from __future__ import annotations

import logging
import random
import time
import uuid
from typing import Any, Callable, Dict, Optional

from message import MessageEnvelope, estimate_chunk_count
from metrics import DEFAULT_LATENCY_BUCKETS, get_metrics_registry
from transport import MeshtasticTransport

__all__ = ["MeshtasticClient"]

LOGGER = logging.getLogger(__name__)

# Default values (used when mode config is not provided)
_DEFAULT_BACKOFF_BASE_SECONDS = 0.5
_DEFAULT_BACKOFF_JITTER_FACTOR = 0.2
_DEFAULT_BACKOFF_MAX_SECONDS = 30.0
_DEFAULT_TIMEOUT = 30.0
_DEFAULT_RETRIES = 2


class MeshtasticClient:
    def __init__(
        self,
        transport: MeshtasticTransport,
        gateway_node_id: str,
        mode_config: Dict[str, Any] | None = None,
    ) -> None:
        self.transport = transport
        self.gateway_node_id = gateway_node_id
        self._metrics = get_metrics_registry()

        # Load client config from mode profile
        self._mode_config = mode_config or {}
        client_cfg = self._mode_config.get("client", {})
        self._backoff_base = float(client_cfg.get("backoff_base_seconds", _DEFAULT_BACKOFF_BASE_SECONDS))
        self._backoff_jitter = float(client_cfg.get("backoff_jitter_factor", _DEFAULT_BACKOFF_JITTER_FACTOR))
        self._backoff_max = float(client_cfg.get("backoff_max_seconds", _DEFAULT_BACKOFF_MAX_SECONDS))
        self._default_timeout = float(self._mode_config.get("timeout", _DEFAULT_TIMEOUT))
        self._default_retries = int(self._mode_config.get("retries", _DEFAULT_RETRIES))

    def echo(
        self,
        message: Any = "ping",
        *,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
    ) -> MessageEnvelope:
        return self._send_typed("echo", {"message": message}, timeout, max_retries, None)

    def payload_digest(
        self,
        *,
        content_b64: str | None = None,
        payload: Any | None = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
    ) -> MessageEnvelope:
        data: Dict[str, Any] = {}
        if content_b64 is not None:
            data["content_b64"] = content_b64
        if payload is not None:
            data["payload"] = payload
        return self._send_typed("payload_digest", data, timeout, max_retries, None)

    def http_request(
        self,
        *,
        url: str,
        method: str | None = None,
        headers: Dict[str, Any] | None = None,
        body: Any | None = None,
        body_b64: str | None = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        progress_callback: Callable[[Dict[str, Any]], None] | None = None,
    ) -> MessageEnvelope:
        data: Dict[str, Any] = {"url": url}
        if method:
            data["method"] = method
        if headers:
            data["headers"] = headers
        if body is not None:
            data["body"] = body
        if body_b64 is not None:
            data["body_b64"] = body_b64
        if timeout is not None:
            data["timeout"] = timeout
        return self._send_typed("http_request", data, timeout, max_retries, progress_callback)

    def _send_typed(
        self,
        command: str,
        data: Dict[str, Any],
        timeout: Optional[float],
        max_retries: Optional[int],
        progress_callback: Callable[[Dict[str, Any]], None] | None,
    ) -> MessageEnvelope:
        kwargs: Dict[str, Any] = {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        if max_retries is not None:
            kwargs["max_retries"] = max_retries
        return self.send_request(command=command, data=data, progress_callback=progress_callback, **kwargs)

    def send_request(
        self,
        command: str,
        data: Dict[str, Any] | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        progress_callback: Callable[[Dict[str, Any]], None] | None = None,
    ) -> MessageEnvelope:
        # Use mode config defaults if not explicitly provided
        if timeout is None:
            timeout = self._default_timeout
        if max_retries is None:
            max_retries = self._default_retries
        request_start = time.time()
        envelope = MessageEnvelope(
            id=uuid.uuid4().hex[:20],
            type="request",
            command=command,
            data=data or {},
        )

        data_size = len(str(data or {}).encode("utf-8"))
        try:
            # Use estimate_chunk_count instead of building all chunks (faster)
            total_chunks = estimate_chunk_count(envelope, self.transport.segment_size)
        except Exception:
            total_chunks = 0
        LOGGER.info(
            "[CLIENT] Sending request %s: command=%s, data_size=%d bytes, timeout=%.1fs, max_retries=%d",
            envelope.id[:8],
            command,
            data_size,
            timeout,
            max_retries,
        )

        last_exception = None
        original_id = envelope.id  # Keep original ID for response matching

        self._metrics.inc(
            "client_requests_total",
            labels={"command": command, "status": "started"},
        )

        if progress_callback:
            progress_callback(
                {
                    "phase": "send",
                    "message_id": envelope.id,
                    "sent_chunks": 0,
                    "total_chunks": total_chunks,
                    "eta_seconds": None,
                    "progress": 0.0,
                }
            )

        for attempt in range(max_retries + 1):
            if attempt > 0:
                # Adaptive exponential backoff with jitter
                backoff = self._backoff_base * (2 ** (attempt - 1))
                backoff += random.uniform(0, backoff * self._backoff_jitter)
                backoff = min(backoff, self._backoff_max)
                LOGGER.info(
                    "[CLIENT] Retry attempt %d/%d for request %s (backoff %.2fs)",
                    attempt,
                    max_retries,
                    original_id[:8],
                    backoff,
                )
                self._metrics.inc(
                    "client_retries_total",
                    labels={"command": command, "attempt": str(attempt)},
                )
                time.sleep(backoff)
                # Keep the same envelope ID for retries - responses may be delayed
                # The gateway may already have sent a response that's still in transit

            # Opportunistically flush any pending spool entries before sending
            if hasattr(self.transport, "tick"):
                self.transport.tick()
            elif hasattr(self.transport, "process_outbox"):
                self.transport.process_outbox()

            send_start = time.time()
            sent_chunks = 0

            def on_chunk_sent(seq: int, total: int) -> None:
                nonlocal sent_chunks
                sent_chunks = seq
                if not progress_callback:
                    return
                elapsed = max(0.001, time.time() - send_start)
                eta = (elapsed / max(seq, 1)) * max(total - seq, 0)
                progress_callback(
                    {
                        "phase": "send",
                        "message_id": envelope.id,
                        "sent_chunks": seq,
                        "total_chunks": total,
                        "eta_seconds": eta,
                        "progress": (seq / total) if total else 0.0,
                    }
                )

            self.transport.send_message(envelope, self.gateway_node_id, on_chunk_sent=on_chunk_sent)
            send_time = time.time() - send_start
            self._metrics.observe(
                "client_send_seconds",
                send_time,
                labels={"command": command},
                buckets=DEFAULT_LATENCY_BUCKETS,
            )
            LOGGER.info(
                "[CLIENT] Request %s sent in %.3fs, waiting for response (timeout %.1fs)...",
                envelope.id[:8],
                send_time,
                timeout,
            )

            attempt_start = time.time()
            last_progress = attempt_start
            observed_chunk_total = 1  # Updated when we see chunk headers/ACKs
            poll_count = 0
            # Use time-since-progress as the primary timeout; cap with a generous overall limit.
            overall_deadline = attempt_start + (timeout + 60.0)

            while True:
                now = time.time()
                inactivity_deadline = last_progress + timeout

                if now >= inactivity_deadline:
                    elapsed = time.time() - attempt_start
                    LOGGER.warning(
                        "[CLIENT] Inactivity timeout waiting for %s after %.3fs (attempt %d/%d)",
                        envelope.id[:8],
                        elapsed,
                        attempt + 1,
                        max_retries + 1,
                    )
                    last_exception = TimeoutError(f"No response for {command} ({envelope.id})")
                    self._metrics.inc(
                        "client_requests_total",
                        labels={"command": command, "status": "timeout"},
                    )
                    break  # Break inner loop to retry

                if now >= overall_deadline:
                    elapsed = time.time() - attempt_start
                    LOGGER.warning(
                        "[CLIENT] Overall timeout waiting for %s after %.3fs (attempt %d/%d)",
                        envelope.id[:8],
                        elapsed,
                        attempt + 1,
                        max_retries + 1,
                    )
                    last_exception = TimeoutError(f"No response for {command} ({envelope.id})")
                    self._metrics.inc(
                        "client_requests_total",
                        labels={"command": command, "status": "timeout"},
                    )
                    break  # Break inner loop to retry

                remaining = min(inactivity_deadline, overall_deadline) - now
                wait_timeout = max(0.05, min(0.5, remaining))

                poll_count += 1
                if poll_count % 10 == 0:  # Log every 10 polls (~5 seconds)
                    elapsed = time.time() - attempt_start
                    LOGGER.debug(
                        "[CLIENT] Still waiting for response to %s (%.1fs elapsed, %.1fs since last progress, %.1fs remaining)",
                        envelope.id[:8],
                        elapsed,
                        elapsed - (last_progress - attempt_start),
                        remaining,
                    )

                # Drive transport (send pending chunks)
                if hasattr(self.transport, "tick"):
                    self.transport.tick()

                sender, response = self.transport.receive_message(timeout=wait_timeout)

                # Refresh progress if we saw chunks/ACKs for this message
                progress = self.transport.last_chunk_progress(original_id)
                if progress and progress.timestamp > last_progress:
                    last_progress = progress.timestamp
                    if progress.total:
                        observed_chunk_total = max(observed_chunk_total, progress.total)
                    LOGGER.debug(
                        "[CLIENT] Progress on %s: chunk %d/%d (ack=%s) at +%.2fs",
                        original_id[:8],
                        progress.seq,
                        progress.total,
                        progress.is_ack,
                        last_progress - attempt_start,
                    )
                    if progress_callback and progress.total:
                        elapsed = max(0.001, last_progress - attempt_start)
                        eta = (elapsed / max(progress.seq, 1)) * max(progress.total - progress.seq, 0)
                        progress_callback(
                            {
                                "phase": "receive",
                                "message_id": original_id,
                                "received_chunks": progress.seq,
                                "total_chunks": progress.total,
                                "eta_seconds": eta,
                                "progress": progress.seq / progress.total,
                            }
                        )

                if response is None:
                    continue

                receive_time = time.time() - request_start
                LOGGER.debug(
                    "[CLIENT] Received message: sender=%s, response_id=%s, response_type=%s, expected_id=%s (after %.3fs)",
                    sender,
                    response.id[:8] if response else None,
                    response.type if response else None,
                    envelope.id[:8],
                    receive_time,
                )

                # Only accept responses with matching request ID (use original ID)
                if response.id != original_id:
                    LOGGER.debug(
                        "[CLIENT] Response ID mismatch: got %s, expected %s (ignoring)",
                        response.id[:8],
                        original_id[:8],
                    )
                    continue
                # Only accept response or error types, not requests
                if response.type not in ("response", "error"):
                    LOGGER.debug(
                        "[CLIENT] Response type mismatch: got %s, expected response or error (ignoring)",
                        response.type,
                    )
                    continue
                # In point-to-point communication, accept any response with matching ID
                # (The request ID matching provides sufficient security)
                total_time = time.time() - request_start
                LOGGER.info(
                    "[CLIENT] Accepted response %s: type=%s, total time=%.3fs (attempt %d/%d)",
                    response.id[:8],
                    response.type,
                    total_time,
                    attempt + 1,
                    max_retries + 1,
                )
                self._metrics.observe(
                    "client_total_seconds",
                    total_time,
                    labels={"command": command, "status": response.type},
                    buckets=DEFAULT_LATENCY_BUCKETS,
                )
                self._metrics.inc(
                    "client_requests_total",
                    labels={
                        "command": command,
                        "status": "success" if response.type == "response" else "error",
                    },
                )
                return response

            # If we get here, we timed out - retry if we have retries left
            if attempt < max_retries:
                continue

        # All retries exhausted
        elapsed = time.time() - request_start
        LOGGER.error(
            "[CLIENT] All retries exhausted for request %s after %.3fs", envelope.id[:8], elapsed
        )
        self._metrics.inc(
            "client_requests_total",
            labels={"command": command, "status": "failure"},
        )
        if last_exception:
            raise last_exception
        raise TimeoutError(f"No response for {command} ({envelope.id})")
