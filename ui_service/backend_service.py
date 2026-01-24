"""Backend service that monitors attached Meshtastic radios."""

from __future__ import annotations

import threading
import time
import base64
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
import json

import sys

ROOT = Path(__file__).resolve()
while ROOT != ROOT.parent and not (ROOT / "src").exists():
    ROOT = ROOT.parent
SRC = ROOT / "src"
if SRC.exists() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from client import MeshtasticClient
from dedupe import DedupeKeys, RequestDeduper
from gateway import MeshtasticGateway
from message import MessageEnvelope
from radio import build_radio
from transport import MeshtasticTransport


class TransportWrapper:
    """Wrapper for MeshtasticTransport that allows observing received messages."""
    
    def __init__(
        self,
        transport: MeshtasticTransport,
        on_message: Callable[[str, MessageEnvelope, dict[str, int] | None], None] | None = None,
        on_send: Callable[[], None] | None = None,
    ) -> None:
        self._transport = transport
        self._on_message = on_message
        self._on_send = on_send
    
    def receive_message(self, timeout: float = 0.25) -> tuple[str | None, MessageEnvelope | None]:
        """Receive a message and notify observer if provided."""
        sender, envelope = self._transport.receive_message(timeout=timeout)
        if envelope is not None and sender is not None and self._on_message:
            progress = self._transport.last_chunk_progress(envelope.id)
            progress_info = None
            if progress:
                progress_info = {"seq": progress.seq, "total": progress.total}
            self._on_message(sender, envelope, progress_info)
        return sender, envelope
    
    def send_message(self, envelope: MessageEnvelope, destination: str, **kwargs) -> None:
        """Send a message via wrapped transport."""
        if self._on_send:
            self._on_send()
        self._transport.send_message(envelope, destination, **kwargs)
    
    def should_process(self, sender: str, envelope: MessageEnvelope) -> bool:
        """Check if message should be processed."""
        return self._transport.should_process(sender, envelope)
    
    def build_dedupe_keys(self, sender: str, envelope: MessageEnvelope) -> DedupeKeys:
        """Build deduplication keys."""
        return self._transport.build_dedupe_keys(sender, envelope)
    
    @property
    def deduper(self) -> RequestDeduper:
        """Access to deduper."""
        return self._transport.deduper
    
    def __getattr__(self, name: str):
        """Forward all other attributes to wrapped transport."""
        return getattr(self._transport, name)



@dataclass
class BackendState:
    radio_ports: list[str] = field(default_factory=list)
    accessible_ports: list[str] = field(default_factory=list)
    radio_detected: bool = False
    last_error: str | None = None
    mode: str = "idle"
    local_radio_id: str | None = None
    connected_radios: list[str] = field(default_factory=list)
    gateway_traffic: list[str] = field(default_factory=list)
    gateway_error: str | None = None
    client_gateway_id: str = ""
    client_url: str = ""
    client_status: str = "idle"
    client_response: str | None = None
    client_error: str | None = None
    client_send_chunks_sent: int = 0
    client_send_chunks_total: int = 0
    client_send_eta_seconds: float | None = None
    client_recv_chunks_received: int = 0
    client_recv_chunks_total: int = 0
    client_recv_eta_seconds: float | None = None
    client_last_payload: str | None = None
    client_last_payload_raw: str | None = None
    client_last_payload_decoded: str | None = None
    gateway_last_payload: str | None = None
    gateway_last_payload_raw: str | None = None
    gateway_last_payload_decoded: str | None = None
    gateway_last_chunks_total: int = 0
    last_rx_time: float | None = None
    last_tx_time: float | None = None
    spool_depth: int = 0
    client_history: list[str] = field(default_factory=list)


def _normalize_ports(ports: Iterable[object]) -> list[str]:
    normalized: list[str] = []
    for port in ports:
        if isinstance(port, str):
            normalized.append(port)
        elif isinstance(port, dict) and "device" in port:
            normalized.append(str(port["device"]))
        elif hasattr(port, "device"):
            normalized.append(str(port.device))
        else:
            normalized.append(str(port))
    return normalized


def detect_radio_ports() -> tuple[list[str], str | None]:
    meshtastic_err = None
    serial_err = None
    ports: list[str] = []
    try:
        from meshtastic import util as meshtastic_util

        ports = _normalize_ports(meshtastic_util.findPorts())
    except Exception as exc:
        meshtastic_err = str(exc)
    try:
        from serial.tools import list_ports

        serial_ports = [port.device for port in list_ports.comports()]
        for port in serial_ports:
            if port not in ports:
                ports.append(port)
    except Exception as exc:
        serial_err = str(exc)

    if not ports:
        return [], f"{meshtastic_err}; {serial_err}"
    return ports, None


class BackendService:
    def __init__(self, poll_interval: float = 1.0) -> None:
        self._poll_interval = poll_interval
        self._state = BackendState()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="meshtastic-backend",
        )
        self._gateway_thread: threading.Thread | None = None
        self._gateway_stop_event = threading.Event()
        self._client_thread: threading.Thread | None = None
        self._gateway_log = deque(maxlen=30)
        self._connected_radios: set[str] = set()
        self._radio: object | None = None
        self._transport: MeshtasticTransport | None = None
        self._radio_port: str | None = None
        self._radio_error: str | None = None
        self._last_connect_attempt = 0.0
        self._preferred_port: str | None = None
        self._mode_name: str = "general"
        self._mode_config: dict = _load_mode_config(self._mode_name)
        # Cache for port accessibility probing
        self._last_ports: list[str] = []
        self._last_accessible: list[str] = []
        self._last_probe_time: float = 0.0
        self._probe_ttl_seconds: float = 30.0

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=2.0)
        self.stop_gateway()
        if self._client_thread and self._client_thread.is_alive():
            self._client_thread.join(timeout=2.0)
        self._close_radio()

    def snapshot(self) -> BackendState:
        with self._lock:
            return BackendState(
                radio_ports=list(self._state.radio_ports),
                accessible_ports=list(self._state.accessible_ports),
                radio_detected=self._state.radio_detected,
                last_error=self._state.last_error,
                mode=self._state.mode,
                local_radio_id=self._state.local_radio_id,
                connected_radios=list(self._state.connected_radios),
                gateway_traffic=list(self._state.gateway_traffic),
                gateway_error=self._state.gateway_error,
                client_gateway_id=self._state.client_gateway_id,
                client_url=self._state.client_url,
                client_status=self._state.client_status,
                client_response=self._state.client_response,
                client_error=self._state.client_error,
                client_send_chunks_sent=self._state.client_send_chunks_sent,
                client_send_chunks_total=self._state.client_send_chunks_total,
                client_send_eta_seconds=self._state.client_send_eta_seconds,
                client_recv_chunks_received=self._state.client_recv_chunks_received,
                client_recv_chunks_total=self._state.client_recv_chunks_total,
                client_recv_eta_seconds=self._state.client_recv_eta_seconds,
                client_last_payload=self._state.client_last_payload,
                client_last_payload_raw=self._state.client_last_payload_raw,
                client_last_payload_decoded=self._state.client_last_payload_decoded,
                gateway_last_payload=self._state.gateway_last_payload,
                gateway_last_payload_raw=self._state.gateway_last_payload_raw,
                gateway_last_payload_decoded=self._state.gateway_last_payload_decoded,
                gateway_last_chunks_total=self._state.gateway_last_chunks_total,
                last_rx_time=self._state.last_rx_time,
                last_tx_time=self._state.last_tx_time,
                spool_depth=self._state.spool_depth,
                client_history=list(self._state.client_history),
            )

    def _run(self) -> None:
        while not self._stop_event.is_set():
            ports, error = detect_radio_ports()
            self._ensure_radio_connection(ports, error)
            
            # Cache for port accessibility probing to avoid expensive I/O on every poll.
            # Re-probe ports only if the list of ports has changed or the cache is stale.
            now = time.time()
            if ports != self._last_ports or (now - self._last_probe_time) >= self._probe_ttl_seconds:
                accessible: list[str] = []
                for port in ports:
                    if self._radio_port and port == self._radio_port:
                        accessible.append(port)
                        continue
                    ok, _ = _probe_port_accessibility(port)
                    if ok:
                        accessible.append(port)
                self._last_ports = list(ports)
                self._last_accessible = list(accessible)
                self._last_probe_time = now
            else:
                # Use cached accessibility results when ports are unchanged and cache is fresh.
                accessible = list(self._last_accessible)
            
            # Always include the current radio port if connected
            if self._radio_port and self._radio_port not in accessible:
                accessible.append(self._radio_port)
            with self._lock:
                if self._radio:
                    self._state.radio_ports = [self._radio_port] if self._radio_port else []
                    self._state.radio_detected = True
                    self._state.last_error = None
                else:
                    self._state.radio_ports = []
                    self._state.radio_detected = False
                    self._state.last_error = self._radio_error or error
                self._state.accessible_ports = accessible
            self._stop_event.wait(self._poll_interval)

    def start_gateway(self) -> None:
        with self._lock:
            self._state.mode = "gateway"
            self._state.gateway_error = None
            self._state.local_radio_id = None
            self._connected_radios.clear()
            self._gateway_log.clear()
            self._state.connected_radios = []
            self._state.gateway_traffic = []
        if self._gateway_thread and self._gateway_thread.is_alive():
            return
        self._gateway_stop_event.clear()
        self._gateway_thread = threading.Thread(
            target=self._run_gateway,
            daemon=True,
            name="meshtastic-gateway",
        )
        self._gateway_thread.start()

    def stop_gateway(self) -> None:
        self._gateway_stop_event.set()
        if self._gateway_thread and self._gateway_thread.is_alive():
            self._gateway_thread.join(timeout=2.0)
        with self._lock:
            if self._state.mode == "gateway":
                self._state.mode = "idle"

    def send_http_request(self, gateway_id: str, url: str) -> None:
        with self._lock:
            self._state.mode = "client"
            self._state.client_gateway_id = gateway_id
            self._state.client_url = url
            self._state.client_status = "sending"
            self._state.client_response = None
            self._state.client_error = None
            self._state.client_send_chunks_sent = 0
            self._state.client_send_chunks_total = 0
            self._state.client_send_eta_seconds = None
            self._state.client_recv_chunks_received = 0
            self._state.client_recv_chunks_total = 0
            self._state.client_recv_eta_seconds = None
            self._state.client_last_payload = None
            self._state.client_history = list(self._state.client_history)
        if self._client_thread and self._client_thread.is_alive():
            return
        self._client_thread = threading.Thread(
            target=self._run_client_request,
            args=(gateway_id, url),
            daemon=True,
            name="meshtastic-client",
        )
        self._client_thread.start()

    def send_health_request(self, gateway_id: str) -> None:
        with self._lock:
            self._state.mode = "client"
            self._state.client_gateway_id = gateway_id
            self._state.client_status = "sending"
            self._state.client_response = None
            self._state.client_error = None
        if self._client_thread and self._client_thread.is_alive():
            return
        self._client_thread = threading.Thread(
            target=self._run_health_request,
            args=(gateway_id,),
            daemon=True,
            name="meshtastic-client-health",
        )
        self._client_thread.start()

    def set_mode(self, mode_name: str) -> None:
        with self._lock:
            if mode_name == self._mode_name:
                return
            config = _load_mode_config(mode_name)
            self._mode_name = mode_name
            self._mode_config = config
        # Rebuild transport for new settings
        self.stop_gateway()
        self._rebuild_transport()

    def set_radio_port(self, port: str | None) -> None:
        # Stop gateway and close current radio; next loop will reconnect using preferred port
        self.stop_gateway()
        self._close_radio()
        self._preferred_port = port

    def list_accessible_ports(self) -> list[str]:
        snapshot = self.snapshot()
        accessible = list(snapshot.accessible_ports or [])
        # Always include current connected port
        if self._radio_port and self._radio_port not in accessible:
            accessible.append(self._radio_port)
        if accessible:
            return accessible
        # Fallback probe if nothing cached
        ports, _ = detect_radio_ports()
        for port in ports:
            ok, _ = _probe_port_accessibility(port)
            if ok:
                accessible.append(port)
        return accessible

    def _run_gateway(self) -> None:
        try:
            transport = self._transport
            if transport is None:
                with self._lock:
                    self._state.gateway_error = self._radio_error or "no accessible radios"
                return
            
            # Wrap transport to observe messages
            wrapped_transport = TransportWrapper(
                transport,
                on_message=self._record_gateway_event,
                on_send=self._record_tx_event,
            )
            gateway = MeshtasticGateway(wrapped_transport)
            
            local_id = _resolve_local_radio_id(self._radio)
            with self._lock:
                self._state.local_radio_id = local_id
                if self._radio_port:
                    self._state.radio_ports = [self._radio_port]
            
            while not self._gateway_stop_event.is_set():
                gateway.run_once(timeout=0.25)
        except Exception as exc:
            with self._lock:
                self._state.gateway_error = str(exc)

    def _run_client_request(self, gateway_id: str, url: str) -> None:
        try:
            transport = self._transport
            if transport is None:
                with self._lock:
                    self._state.client_status = "error"
                    self._state.client_error = self._radio_error or "no accessible radios"
                return
            wrapped_transport = TransportWrapper(
                transport,
                on_send=self._record_tx_event,
            )
            client = MeshtasticClient(wrapped_transport, gateway_id)
            response = client.http_request(
                url=url,
                progress_callback=self._record_client_progress,
            )
            summary = _summarize_response(response)
            with self._lock:
                self._state.client_status = "done"
                self._state.client_response = summary
                self._state.client_last_payload = _format_payload(response.data)
                self._state.client_last_payload_raw = _stringify_payload(response.data)
                self._state.client_last_payload_decoded = _decode_content(response.data)
                self._state.client_history = _append_history(
                    self._state.client_history,
                    f"{_timestamp()} http_request {summary}",
                )
        except Exception as exc:
            with self._lock:
                self._state.client_status = "error"
                self._state.client_error = str(exc)

    def _run_health_request(self, gateway_id: str) -> None:
        try:
            transport = self._transport
            if transport is None:
                with self._lock:
                    self._state.client_status = "error"
                    self._state.client_error = self._radio_error or "no accessible radios"
                return
            wrapped_transport = TransportWrapper(
                transport,
                on_send=self._record_tx_event,
            )
            client = MeshtasticClient(wrapped_transport, gateway_id)
            start = time.time()
            response = client.send_request("health")
            latency = (time.time() - start) * 1000.0
            summary = _summarize_response(response)
            with self._lock:
                self._state.client_status = "done"
                self._state.client_response = f"{summary} ({latency:.0f} ms)"
                self._state.client_last_payload = _format_payload(response.data)
                self._state.client_last_payload_raw = _stringify_payload(response.data)
                self._state.client_last_payload_decoded = _decode_content(response.data)
                self._state.client_history = _append_history(
                    self._state.client_history,
                    f"{_timestamp()} health {summary} ({latency:.0f} ms)",
                )
        except Exception as exc:
            with self._lock:
                self._state.client_status = "error"
                self._state.client_error = str(exc)
    def _record_gateway_event(
        self,
        sender: str,
        envelope: MessageEnvelope,
        progress: dict[str, int] | None,
    ) -> None:
        timestamp = _timestamp()
        command = envelope.command or envelope.type or "message"
        message = f"{timestamp} {sender} {command}"
        if envelope.command == "http_request":
            url = ""
            if isinstance(envelope.data, dict):
                url = str(envelope.data.get("url") or "")
            if url:
                message = f"{timestamp} {sender} {command} {url}"
        with self._lock:
            self._connected_radios.add(sender)
            self._gateway_log.appendleft(message)
            self._state.connected_radios = sorted(self._connected_radios)
            self._state.gateway_traffic = list(self._gateway_log)
            self._state.gateway_last_payload = _format_payload(envelope.data)
            self._state.gateway_last_payload_raw = _stringify_payload(envelope.data)
            self._state.gateway_last_payload_decoded = _decode_content(envelope.data)
            if progress and progress.get("total"):
                self._state.gateway_last_chunks_total = int(progress["total"])
            self._state.last_rx_time = time.time()
            self._state.spool_depth = _get_spool_depth(self._transport)

    def _record_client_progress(self, update: dict[str, object]) -> None:
        phase = str(update.get("phase", ""))
        with self._lock:
            if phase == "send":
                self._state.client_send_chunks_sent = int(update.get("sent_chunks", 0))
                self._state.client_send_chunks_total = int(update.get("total_chunks", 0))
                self._state.client_send_eta_seconds = _coerce_seconds(update.get("eta_seconds"))
            elif phase == "receive":
                self._state.client_recv_chunks_received = int(
                    update.get("received_chunks", 0)
                )
                self._state.client_recv_chunks_total = int(update.get("total_chunks", 0))
                self._state.client_recv_eta_seconds = _coerce_seconds(update.get("eta_seconds"))
                self._state.last_rx_time = time.time()
            self._state.spool_depth = _get_spool_depth(self._transport)

    def _record_tx_event(self) -> None:
        with self._lock:
            self._state.last_tx_time = time.time()
            self._state.spool_depth = _get_spool_depth(self._transport)

    def _ensure_radio_connection(self, ports: list[str], error: str | None) -> None:
        if self._radio:
            return
        now = time.time()
        if now - self._last_connect_attempt < 2.0:
            return
        self._last_connect_attempt = now
        # Respect preferred port ordering
        if self._preferred_port:
            ordered = [self._preferred_port] + [p for p in ports if p != self._preferred_port]
            ports = ordered
        if not ports:
            self._radio_error = error or "no radio ports detected"
            return
        try:
            radio, used_port = _open_radio_from_ports(ports, "ui")
            self._radio = radio
            self._transport = self._build_transport(radio)
            self._radio_port = used_port
            self._radio_error = None
            with self._lock:
                self._state.local_radio_id = _resolve_local_radio_id(radio)
        except Exception as exc:
            self._radio_error = str(exc)

    def _close_radio(self) -> None:
        radio = self._radio
        self._radio = None
        self._transport = None
        self._radio_port = None
        if radio and hasattr(radio, "close"):
            radio.close()
        with self._lock:
            self._state.local_radio_id = None

    def _rebuild_transport(self) -> None:
        if not self._radio:
            return
        self._transport = self._build_transport(self._radio)

    def _build_transport(self, radio: object) -> MeshtasticTransport:
        cfg = self._mode_config or {}
        transport_kwargs = {
            "segment_size": int(cfg.get("transport", {}).get("segment_size", 200)),
            "chunk_ttl_per_chunk": float(cfg.get("transport", {}).get("chunk_ttl_per_chunk", 2.0)),
            "chunk_ttl_max": float(cfg.get("transport", {}).get("chunk_ttl_max", 600.0)),
            "chunk_delay_threshold": cfg.get("transport", {}).get("chunk_delay_threshold", None),
            "chunk_delay_seconds": float(cfg.get("transport", {}).get("chunk_delay_seconds", 0.0)),
            "nack_max_per_seq": int(cfg.get("transport", {}).get("nack_max_per_seq", 5)),
            "nack_interval": float(cfg.get("transport", {}).get("nack_interval", 1.0)),
        }
        reliability_method = cfg.get("reliability_method")
        return MeshtasticTransport(
            radio,
            reliability=reliability_method,
            **transport_kwargs,
        )


def _resolve_local_radio_id(radio: object) -> str | None:
    if hasattr(radio, "node_id"):
        return str(getattr(radio, "node_id"))
    interface = getattr(radio, "_interface", None)
    if interface and hasattr(interface, "getMyNodeInfo"):
        info = interface.getMyNodeInfo()
        if isinstance(info, dict):
            user = info.get("user", {})
            if isinstance(user, dict) and user.get("id"):
                return str(user.get("id"))
    return None


def _probe_port_accessibility(port: str) -> tuple[bool, str | None]:
    """Try opening a radio on the given port to verify accessibility."""
    radio = None
    try:
        radio = build_radio(False, port, "probe")
        return True, None
    except Exception as exc:
        return False, str(exc)
    finally:
        if radio and hasattr(radio, "close"):
            try:
                radio.close()
            except Exception:
                pass


def _open_radio_from_ports(
    ports: list[str],
    node_id: str,
) -> tuple[object, str | None]:
    errors: list[str] = []
    for port in ports:
        try:
            return build_radio(False, port, node_id), port
        except Exception as exc:
            errors.append(f"{port}: {exc}")
    raise RuntimeError("no available radios: " + "; ".join(errors))


def _summarize_response(response: MessageEnvelope) -> str:
    if response.type == "error":
        if isinstance(response.data, dict) and response.data.get("error"):
            return str(response.data["error"])
        return "gateway returned an error"
    if not isinstance(response.data, dict):
        return "response received"
    result = response.data.get("result")
    if isinstance(result, dict):
        status = result.get("status")
        length = result.get("content_length")
        if status is not None and length is not None:
            return f"status={status} bytes={length}"
        if status is not None:
            return f"status={status}"
    return "response received"


def _format_payload(payload: object, limit: int = 160) -> str | None:
    if payload is None:
        return None
    text = _stringify_payload(payload)
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def _stringify_payload(payload: object) -> str:
    try:
        return json.dumps(payload, ensure_ascii=True)
    except Exception:
        return str(payload)


def _decode_content(payload: object) -> str | None:
    try:
        if isinstance(payload, dict):
            b64 = payload.get("content_b64")
            if not b64 and isinstance(payload.get("result"), dict):
                b64 = payload["result"].get("content_b64")
            if isinstance(b64, str):
                raw = base64.b64decode(b64)
                try:
                    return raw.decode("utf-8")
                except UnicodeDecodeError:
                    return raw.hex()
    except Exception:
        return None
    return None


def _coerce_seconds(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _append_history(history: list[str], entry: str, limit: int = 5) -> list[str]:
    updated = [entry] + history
    return updated[:limit]


def _timestamp() -> str:
    return time.strftime("%H:%M:%S")


def _get_spool_depth(transport: MeshtasticTransport | None) -> int:
    if transport and transport.spool:
        try:
            return int(transport.spool.depth())
        except Exception:
            return 0
    return 0


def _load_mode_config(mode_name: str) -> dict:
    root = Path(__file__).resolve().parent.parent
    mode_path = root / "modes" / f"{mode_name}.json"
    try:
        with open(mode_path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return {}
