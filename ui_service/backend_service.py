"""Backend service that monitors attached Meshtastic radios."""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import sys

ROOT = Path(__file__).resolve()
while ROOT != ROOT.parent and not (ROOT / "src").exists():
    ROOT = ROOT.parent
SRC = ROOT / "src"
if SRC.exists() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from client import MeshtasticClient
from dedupe import DedupeKeys
from gateway import MeshtasticGateway
from message import MessageEnvelope
from radio import build_radio
from transport import MeshtasticTransport


class TransportWrapper:
    """Wrapper for MeshtasticTransport that allows observing received messages."""
    
    def __init__(
        self,
        transport: MeshtasticTransport,
        on_message: Callable[[str, MessageEnvelope], None] | None = None,
    ) -> None:
        self._transport = transport
        self._on_message = on_message
    
    def receive_message(self, timeout: float = 0.25) -> tuple[str | None, MessageEnvelope | None]:
        """Receive a message and notify observer if provided."""
        sender, envelope = self._transport.receive_message(timeout=timeout)
        if envelope is not None and sender is not None and self._on_message:
            self._on_message(sender, envelope)
        return sender, envelope
    
    def send_message(self, envelope: MessageEnvelope, destination: str) -> None:
        """Send a message via wrapped transport."""
        self._transport.send_message(envelope, destination)
    
    def should_process(self, sender: str, envelope: MessageEnvelope) -> bool:
        """Check if message should be processed."""
        return self._transport.should_process(sender, envelope)
    
    def build_dedupe_keys(self, sender: str, envelope: MessageEnvelope) -> DedupeKeys:
        """Build deduplication keys."""
        return self._transport.build_dedupe_keys(sender, envelope)
    
    @property
    def deduper(self):
        """Access to deduper."""
        return self._transport.deduper
    
    def __getattr__(self, name: str):
        """Forward all other attributes to wrapped transport."""
        return getattr(self._transport, name)



@dataclass
class BackendState:
    radio_ports: list[str] = field(default_factory=list)
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
    try:
        from meshtastic import util as meshtastic_util

        ports = meshtastic_util.findPorts()
        return _normalize_ports(ports), None
    except Exception as exc:
        meshtastic_err = str(exc)
    try:
        from serial.tools import list_ports

        ports = [port.device for port in list_ports.comports()]
        return ports, None
    except Exception as exc:
        serial_err = str(exc)
    return [], f"{meshtastic_err}; {serial_err}"


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

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=2.0)
        self.stop_gateway()
        if self._client_thread and self._client_thread.is_alive():
            self._client_thread.join(timeout=2.0)

    def snapshot(self) -> BackendState:
        with self._lock:
            return BackendState(
                radio_ports=list(self._state.radio_ports),
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
            )

    def _run(self) -> None:
        while not self._stop_event.is_set():
            ports, error = detect_radio_ports()
            with self._lock:
                self._state.radio_ports = ports
                self._state.radio_detected = bool(ports)
                self._state.last_error = error
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
        if self._client_thread and self._client_thread.is_alive():
            return
        self._client_thread = threading.Thread(
            target=self._run_client_request,
            args=(gateway_id, url),
            daemon=True,
            name="meshtastic-client",
        )
        self._client_thread.start()

    def _run_gateway(self) -> None:
        radio = None
        try:
            ports, _ = detect_radio_ports()
            if not ports:
                with self._lock:
                    self._state.gateway_error = "no radio ports detected"
                return
            port = ports[0]
            radio = build_radio(False, port, "gateway")
            transport = MeshtasticTransport(radio)
            
            # Wrap transport to observe messages
            wrapped_transport = TransportWrapper(
                transport,
                on_message=self._record_gateway_event,
            )
            gateway = MeshtasticGateway(wrapped_transport)
            
            local_id = _resolve_local_radio_id(radio)
            with self._lock:
                self._state.local_radio_id = local_id
            
            while not self._gateway_stop_event.is_set():
                gateway.run_once(timeout=0.25)
        except Exception as exc:
            with self._lock:
                self._state.gateway_error = str(exc)
        finally:
            if radio and hasattr(radio, "close"):
                radio.close()

    def _run_client_request(self, gateway_id: str, url: str) -> None:
        radio = None
        try:
            ports, _ = detect_radio_ports()
            if not ports:
                with self._lock:
                    self._state.client_status = "error"
                    self._state.client_error = "no radio ports detected"
                return
            port = ports[0]
            radio = build_radio(False, port, "client")
            transport = MeshtasticTransport(radio)
            client = MeshtasticClient(transport, gateway_id)
            response = client.http_request(url=url)
            summary = _summarize_response(response)
            with self._lock:
                self._state.client_status = "done"
                self._state.client_response = summary
        except Exception as exc:
            with self._lock:
                self._state.client_status = "error"
                self._state.client_error = str(exc)
        finally:
            if radio and hasattr(radio, "close"):
                radio.close()

    def _record_gateway_event(self, sender: str, envelope: MessageEnvelope) -> None:
        timestamp = time.strftime("%H:%M:%S")
        command = envelope.command or envelope.type or "message"
        message = f"{timestamp} {sender} {command}"
        with self._lock:
            self._connected_radios.add(sender)
            self._gateway_log.appendleft(message)
            self._state.connected_radios = sorted(self._connected_radios)
            self._state.gateway_traffic = list(self._gateway_log)


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
