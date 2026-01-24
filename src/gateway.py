from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Set

from message import MessageEnvelope
from metrics import DEFAULT_LATENCY_BUCKETS, get_metrics_registry
from transport import MeshtasticTransport

__all__ = ["MeshtasticGateway", "Handler", "DEFAULT_HANDLERS"]

LOGGER = logging.getLogger(__name__)


Handler = Callable[[MessageEnvelope, Dict[str, Any]], Any]


def _handle_echo(envelope: MessageEnvelope, data: Dict[str, Any]) -> Dict[str, Any]:
    return {"echo": data, "id": envelope.id}


def _handle_health(_envelope: MessageEnvelope, _data: Dict[str, Any]) -> Dict[str, Any]:
    return {"status": "ok"}


def _handle_payload_digest(_envelope: MessageEnvelope, data: Dict[str, Any]) -> Dict[str, Any]:
    payload = data.get("content_b64") or data.get("payload")
    if payload is None:
        return {"received": 0, "sha256": None}
    raw = str(payload).encode("utf-8")
    try:
        import hashlib

        digest = hashlib.sha256(raw).hexdigest()
    except Exception:
        digest = None
    return {"received": len(raw), "sha256": digest}


def _handle_http_request(_envelope: MessageEnvelope, data: Dict[str, Any]) -> Dict[str, Any]:
    url = data.get("url")
    if not url:
        return {"error": "url is required"}

    method = str(data.get("method", "GET")).upper()
    headers = data.get("headers") or {}
    if not isinstance(headers, dict):
        headers = {}

    timeout = float(data.get("timeout", 20.0))
    body = None
    if data.get("body_b64") is not None:
        try:
            body = base64.b64decode(str(data["body_b64"]))
        except Exception as exc:
            return {"error": f"invalid body_b64: {exc}"}
    elif data.get("body") is not None:
        body_value = data["body"]
        if isinstance(body_value, (dict, list)):
            body = json.dumps(body_value).encode("utf-8")
            headers.setdefault("content-type", "application/json")
        else:
            body = str(body_value).encode("utf-8")

    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    status = None
    response_headers: Dict[str, str] = {}
    content = b""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.getcode()
            response_headers = {k.lower(): v for k, v in response.headers.items()}
            content = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        response_headers = {k.lower(): v for k, v in exc.headers.items()}
        try:
            content = exc.read()
        except Exception:
            content = b""
    except Exception as exc:
        return {"error": str(exc)}

    # Note: content_length can be derived from content_b64 but is kept for backwards compatibility
    return {
        "status": status,
        "content_b64": base64.b64encode(content).decode("ascii"),
        "content_length": len(content),
    }


DEFAULT_HANDLERS: Dict[str, Handler] = {
    "echo": _handle_echo,
    "health": _handle_health,
    "payload_digest": _handle_payload_digest,
    "http_request": _handle_http_request,
}


class MeshtasticGateway:
    _DEFAULT_OPERATION_TIMEOUT = 30.0
    _DEFAULT_NUMERIC_SENDER_DELAY = 0.5  # Reduced from 1.5s, configurable

    def __init__(
        self,
        transport: MeshtasticTransport,
        handlers: Dict[str, Handler] | None = None,
        numeric_sender_delay: float | None = None,
    ) -> None:
        self.transport = transport
        self.handlers = handlers or DEFAULT_HANDLERS
        self._running = False
        self._metrics = get_metrics_registry()
        self._numeric_senders_seen: Set[str] = set()
        # Configurable delay for first contact from numeric sender IDs
        # Set to 0 to disable, or adjust based on network conditions
        self._numeric_sender_delay = (
            numeric_sender_delay if numeric_sender_delay is not None 
            else self._DEFAULT_NUMERIC_SENDER_DELAY
        )

    def run_once(self, timeout: float = 1.0) -> None:
        outbox_handler = getattr(self.transport, "process_outbox", None)
        if callable(outbox_handler):
            outbox_handler()
        receive_start = time.time()
        sender, envelope = self.transport.receive_message(timeout=timeout)
        receive_time = time.time() - receive_start

        if envelope is None or sender is None:
            if receive_time > timeout * 0.9:  # Only log if we waited most of the timeout
                LOGGER.debug("[GATEWAY] No message received after %.3fs", receive_time)
            return

        if envelope.type != "request":
            LOGGER.debug(
                "[GATEWAY] Ignoring non-request message: type=%s, id=%s",
                envelope.type,
                envelope.id[:8],
            )
            self._metrics.inc(
                "gateway_ignored_messages_total",
                labels={"reason": "non-request", "type": envelope.type or "unknown"},
            )
            return

        if not self.transport.should_process(sender, envelope):
            LOGGER.debug(
                "[GATEWAY] Duplicate request %s from %s (ignored)", envelope.id[:8], sender
            )
            self._metrics.inc(
                "gateway_duplicate_requests_total",
                labels={"command": envelope.command or "unknown"},
            )
            return

        lease_seconds = (envelope.meta or {}).get("lease_seconds")
        dedupe_keys = self.transport.build_dedupe_keys(sender, envelope)
        in_progress_key = dedupe_keys.semantic or dedupe_keys.correlation or dedupe_keys.message
        lease_duration = lease_seconds or self.transport.deduper.lease_seconds

        if not self.transport.deduper.acquire_lease(
            in_progress_key, lease_seconds=lease_duration
        ):
            LOGGER.debug(
                "[GATEWAY] Duplicate request %s for key %s already in progress",
                envelope.id[:8],
                in_progress_key,
            )
            return

        request_start = time.time()
        self._metrics.gauge("gateway_inflight_requests").inc(1)
        self._metrics.inc(
            "gateway_requests_total",
            labels={"command": envelope.command or "unknown", "status": "received"},
        )
        LOGGER.info(
            "[GATEWAY] Processing request %s from %s (received after %.3fs)",
            envelope.id[:8],
            sender,
            receive_time,
        )

        # Allow time for node discovery to complete if this is first contact
        # Delay is configurable (default 0.5s, set to 0 to disable)
        if (
            self._numeric_sender_delay > 0
            and sender 
            and sender.isdigit() 
            and not sender.startswith("!") 
            and sender not in self._numeric_senders_seen
        ):
            LOGGER.info(
                "[GATEWAY] Sender %s is numeric ID - waiting %.1fs for node discovery", 
                sender, 
                self._numeric_sender_delay,
            )
            time.sleep(self._numeric_sender_delay)
            self._numeric_senders_seen.add(sender)

        try:
            handle_start = time.time()
            try:
                response = self._handle_request(envelope)
            except Exception as exc:
                LOGGER.exception("[GATEWAY] Unhandled error while processing %s", envelope.id[:8])
                self._metrics.inc(
                    "gateway_requests_total",
                    labels={"command": envelope.command or "unknown", "status": "error"},
                )
                response = MessageEnvelope(
                    id=envelope.id,
                    type="error",
                    command=envelope.command,
                    correlation_id=envelope.correlation_id,
                    data={"error": str(exc)},
                )
            handle_time = time.time() - handle_start
            LOGGER.info("[GATEWAY] Handled request %s in %.3fs", envelope.id[:8], handle_time)
            self._metrics.observe(
                "gateway_handle_seconds",
                handle_time,
                labels={"command": envelope.command or "unknown"},
                buckets=DEFAULT_LATENCY_BUCKETS,
            )

            send_start = time.time()
            LOGGER.info("[GATEWAY] Sending response %s to %s", response.id[:8], sender)
            try:
                self.transport.send_message(response, sender)
            except Exception as exc:
                LOGGER.warning(
                    "[GATEWAY] Failed to send response %s to %s: %s",
                    response.id[:8],
                    sender,
                    exc,
                    exc_info=True,
                )
                self._metrics.inc(
                    "gateway_requests_total",
                    labels={"command": response.command or "unknown", "status": "send_failed"},
                )
                return
            send_time = time.time() - send_start
            self._metrics.observe(
                "gateway_send_seconds",
                send_time,
                labels={"command": envelope.command or "unknown"},
                buckets=DEFAULT_LATENCY_BUCKETS,
            )

            total_time = time.time() - request_start
            LOGGER.info(
                "[GATEWAY] Completed request %s: total %.3fs (handle: %.3fs, send: %.3fs)",
                envelope.id[:8],
                total_time,
                handle_time,
                send_time,
            )
            self._metrics.observe(
                "gateway_total_seconds",
                total_time,
                labels={"command": envelope.command or "unknown"},
                buckets=DEFAULT_LATENCY_BUCKETS,
            )
            self._metrics.inc(
                "gateway_requests_total",
                labels={"command": envelope.command or "unknown", "status": "success"},
            )
        finally:
            self.transport.deduper.release_lease(
                in_progress_key, lease_seconds=lease_duration, remember=True
            )
            self._metrics.gauge("gateway_inflight_requests").dec(1)

    def run_forever(self, poll_interval: float = 0.1) -> None:
        self._running = True
        while self._running:
            self.run_once(timeout=poll_interval)

    def stop(self) -> None:
        self._running = False

    def _handle_request(self, envelope: MessageEnvelope) -> MessageEnvelope:
        try:
            handler = self.handlers.get(envelope.command)
            if not handler:
                raise ValueError(f"Unknown command: {envelope.command}")
            result = handler(envelope, envelope.data or {})
            if asyncio.iscoroutine(result):
                result = asyncio.run(result)
            compacted = self._compact_payload({"result": result})
            return MessageEnvelope(
                id=envelope.id,
                type="response",
                command=envelope.command,
                correlation_id=envelope.correlation_id,
                data=compacted,
            )
        except Exception as exc:
            LOGGER.exception("Failed to run %s", envelope.command)
            return MessageEnvelope(
                id=envelope.id,
                type="error",
                command=envelope.command,
                correlation_id=envelope.correlation_id,
                data={"error": str(exc)},
            )

    def _compact_payload(self, payload: Any) -> Any:
        if isinstance(payload, dict):
            compacted: Dict[str, Any] = {}
            for key, value in payload.items():
                compact_value = self._compact_payload(value)
                if compact_value is None:
                    continue
                if compact_value is False:
                    continue
                if compact_value == "":
                    continue
                if compact_value == {}:
                    continue
                if compact_value == []:
                    continue
                compacted[key] = compact_value
            return compacted
        if isinstance(payload, list):
            return [self._compact_payload(item) for item in payload]
        return payload
