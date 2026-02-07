from __future__ import annotations

import logging
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Protocol, Tuple

from dedupe import RequestDeduper, build_dedupe_keys
from message import (
    FLAG_ACK,
    MessageEnvelope,
    chunk_envelope,
    parse_chunk,
)
from metrics import DEFAULT_LATENCY_BUCKETS, get_metrics_registry
from reassembly import MessageReassembler
from reliability import ReliabilityStrategy, strategy_from_name
from spool import PersistentSpool

__all__ = [
    "RadioInterface",
    "InMemoryRadioBus",
    "InMemoryRadio",
    "ChunkProgress",
    "MeshtasticTransport",
]


class RadioInterface(Protocol):
    def send(self, destination: str, payload: bytes) -> None: ...

    def receive(self, timeout: float) -> Optional[Tuple[str, bytes]]: ...

    def close(self) -> None: ...


@dataclass
class InMemoryRadioBus:
    queues: Dict[str, deque] = field(default_factory=lambda: defaultdict(deque))

    def send(self, source: str, destination: str, payload: bytes) -> None:
        self.queues[destination].append((source, payload))

    def receive(self, node_id: str) -> Optional[Tuple[str, bytes]]:
        queue = self.queues.setdefault(node_id, deque())
        if queue:
            return queue.popleft()
        return None


class InMemoryRadio:
    """Simple radio emulator used for tests and early development."""

    def __init__(self, node_id: str, bus: Optional[InMemoryRadioBus] = None) -> None:
        self.node_id = node_id
        self._bus = bus or InMemoryRadioBus()

    def send(self, destination: str, payload: bytes) -> None:
        self._bus.send(self.node_id, destination, payload)

    def receive(self, timeout: float) -> Optional[Tuple[str, bytes]]:
        return self._bus.receive(self.node_id)

    def close(self) -> None:
        """No-op for in-memory radio."""
        pass


@dataclass
class ChunkProgress:
    """Tracks the most recent chunk or ACK for a given message prefix."""

    message_id: str
    seq: int
    total: int
    timestamp: float
    is_ack: bool = False


RETRY_CHUNK_DELAY = 0.1
MAX_CHUNK_SIZE = 230  # Conservative Meshtastic chunk size limit (bytes)
MIN_SEGMENT_SIZE = 50  # Minimum segment size to avoid over-reduction
SEGMENT_SIZE_REDUCTION = 50  # Bytes to reduce when chunk size exceeds limit
logger = logging.getLogger(__name__)

# Pre-created label dicts for common metrics (avoid dict creation on every call)
_LABELS_CHUNKS_INBOUND = {"direction": "inbound", "command": "unknown"}
_LABELS_CHUNKS_OUTBOUND_NACK = {"direction": "outbound", "command": "nack_resend"}


class MeshtasticTransport:
    """Optimized transport layer for Meshtastic communication with application-level ACKs.

    Args:
        segment_size: Chunk payload size in bytes.
        chunk_ttl: TTL (seconds) for reassembly buckets.
        spool_path: Optional JSON file path for durable outgoing message spool.
        spool_max_attempts: Maximum resend attempts per message before expiring.
        spool_base_delay: Base delay (seconds) for exponential backoff.
        spool_jitter: Random jitter (seconds) added to retry delays.
        spool_expiry: Expiration (seconds) before pending spool entries are discarded.

    Application-level ACK envelopes are emitted on receipt of every message. When
    a spool path is provided, outgoing messages are persisted, retried with
    exponential backoff + jitter, and cleared only after an ACK is observed.
    """

    def __init__(
        self,
        radio: RadioInterface,
        deduper: RequestDeduper | None = None,
        disable_dedupe: bool = False,
        dedupe_lease_seconds: float | None = None,
        segment_size: int = 200,  # Push toward upper envelope (header+payload ~216 bytes)
        chunk_ttl: float = 120,  # Base TTL for message reassembly
        chunk_ttl_per_chunk: float = 2.0,  # Additional TTL per chunk for large messages
        chunk_ttl_max: float = 600.0,  # Cap TTL to avoid unbounded retention
        chunk_delay_threshold: int | None = None,  # Enable pacing only for large messages
        chunk_delay_seconds: float = 0.0,  # Delay between chunks when threshold is met
        nack_max_per_seq: int = 5,
        nack_interval: float = 1.0,
        spool_path: str | None = None,
        spool_max_attempts: int = 5,
        spool_base_delay: float = 2.0,
        spool_jitter: float = 0.5,
        spool_expiry: float = 86400.0,
        reliability: ReliabilityStrategy | str | None = None,
        enable_spool: bool = False,  # Enable spooling when spool_path is provided
    ) -> None:
        self.radio = radio
        self.reassembler = MessageReassembler(
            ttl_seconds=chunk_ttl,
            per_chunk_ttl=chunk_ttl_per_chunk,
            max_ttl=chunk_ttl_max,
            nack_max_per_seq=nack_max_per_seq,
            nack_interval=nack_interval,
        )
        if deduper is None:
            lease = dedupe_lease_seconds if dedupe_lease_seconds is not None else 300.0
            self.deduper = RequestDeduper(lease_seconds=lease)
        else:
            self.deduper = deduper
        self.segment_size = segment_size
        self._progress_ttl = max(chunk_ttl_max, chunk_ttl, 1.0)
        self._chunk_delay_threshold = chunk_delay_threshold
        self._chunk_delay_value = max(0.0, chunk_delay_seconds)
        # Cache sent chunks for targeted resends on NACK
        self._chunk_cache: Dict[str, Dict[int, bytes]] = {}
        self._chunk_cache_expiry: Dict[str, float] = {}
        self.spool = (
            PersistentSpool(
                spool_path,
                max_attempts=spool_max_attempts,
                base_delay=spool_base_delay,
                jitter=spool_jitter,
                expiry_seconds=spool_expiry,
            )
            if spool_path
            else None
        )
        self._metrics = get_metrics_registry()
        # Track the last observed chunk/ack per message ID prefix (8 chars)
        self._last_progress: Dict[str, ChunkProgress] = {}
        self._sent_chunk_counts: Dict[str, int] = {}
        if isinstance(reliability, str) or reliability is None:
            env_choice = os.getenv("MESHTASTIC_RELIABILITY_METHOD")
            self.reliability: ReliabilityStrategy = strategy_from_name(reliability or env_choice)
        else:
            self.reliability = reliability
        self._enable_spool = enable_spool
        self._disable_dedupe = disable_dedupe
        
        # Internal state for non-blocking transport
        self._active_chunks: Dict[str, List[bytes]] = {}
        self._active_progress: Dict[str, int] = {}
        
        # Periodic cleanup state (avoid O(n) scan on every operation)
        self._last_cleanup_time: float = 0.0
        self._cleanup_interval: float = 10.0  # Cleanup every 10 seconds max
        self._cleanup_counter: int = 0
        self._cleanup_every_n: int = 50  # Or every 50 operations
        
        self._record_spool_depth()

    def _record_spool_depth(self) -> None:
        if not self.spool:
            return
        try:
            depth = float(self.spool.depth())
        except (AttributeError, TypeError, ValueError):
            return
        self._metrics.set_gauge(
            "transport_spool_depth",
            depth,
            description="Number of pending messages in spool",
        )

    def _message_prefix(self, message_id: str) -> str:
        return message_id[:8]

    def _inc_sent_chunks(self, message_id: str, amount: int = 1) -> None:
        prefix = self._message_prefix(message_id)
        self._sent_chunk_counts[prefix] = self._sent_chunk_counts.get(prefix, 0) + amount

    def get_sent_chunk_count(self, message_id: str) -> int:
        prefix = self._message_prefix(message_id)
        return self._sent_chunk_counts.get(prefix, 0)

    def enqueue(self, envelope: MessageEnvelope, destination: str) -> None:
        """Enqueue a message for transmission (non-blocking)."""
        # Track messages in the spool if enabled, excluding ack and response types
        track_spool = (
            self._enable_spool
            and self.spool is not None
            and envelope.type not in {"ack", "response"}
        )
        if track_spool:
            self.spool.add(envelope, destination)
            self._record_spool_depth()
            self._metrics.inc(
                "transport_messages_enqueued",
                labels={
                    "type": envelope.type,
                    "command": envelope.command or "unknown",
                    "priority": str(envelope.priority),
                },
            )
        else:
            # Spool is disabled or unavailable: the message will not be persisted or sent
            # by the normal transmit loop. Make this drop explicit via logging and metrics
            # so that it is not silently lost.
            logger.warning(
                "Dropping message because spool is disabled or unavailable: "
                "type=%s command=%s priority=%s destination=%s",
                envelope.type,
                envelope.command or "unknown",
                str(envelope.priority),
                destination,
            )
            self._metrics.inc(
                "transport_messages_dropped",
                labels={
                    "type": envelope.type,
                    "command": envelope.command or "unknown",
                    "priority": str(envelope.priority),
                    "reason": "no_spool",
                },
            )

    def tick(self) -> None:
        """Process one step of the transport state machine (send one chunk)."""
        # 1. Process Receive (non-blocking)
        # This is handled by external calls to receive_message(), or we could call it here.
        # But commonly the main loop calls both.
        
        # 2. Process Transmit
        self._tick_transmit()

    def _tick_transmit(self) -> None:
        """Check spool and send one chunk of the highest priority message."""
        if not self.spool:
            return

        # Get highest priority message
        # We need to maintain state of "currently sending message" to avoid 
        # re-chunking it every tick if we want to be efficient, OR we just 
        # re-chunk on demand (simpler, slightly more CPU). 
        # Given 200-byte messages, re-chunking is cheap.
        
        due = self.spool.due()
        if not due:
            return

        # Pick the first one (highest priority)
        msg_id, entry = due[0]
        
        try:
            envelope = MessageEnvelope.from_dict(entry.envelope)
        except (KeyError, TypeError):
            logger.warning("[TRANSPORT] Skipping corrupted spool entry %s", msg_id)
            self.spool.ack(msg_id)
            return

        # 1. Chunk it
        # Optimization: cache chunks for the *active* message ID to avoid re-encoding
        # every 100ms.
        chunks = self._get_or_create_chunks(msg_id, envelope)
        
        # 2. Determine which chunk to send next
        # We rely on an in-memory "progress pointer" for this message ID.
        # If we crashed, we start from 0 (harmless duplicate).
        next_seq = self._get_next_seq(msg_id)
        
        # Call on_send hook when starting a new message (seq == 1)
        if next_seq == 1 and self.reliability:
            try:
                self.reliability.on_send(self, envelope, entry.destination, len(chunks))
            except Exception as e:
                logger.warning("[TRANSPORT] Reliability on_send hook failed for %s: %s", msg_id, e)
        
        if next_seq > len(chunks):
            # Done sending all chunks - call on_chunks_sent hook
            if self.reliability:
                try:
                    self.reliability.on_chunks_sent(self, envelope, entry.destination, len(chunks))
                except Exception as e:
                    logger.warning("[TRANSPORT] Reliability on_chunks_sent hook failed for %s: %s", msg_id, e)
            
            self._metrics.inc(
                "transport_messages_total",
                labels={
                    "direction": "outbound",
                    "type": envelope.type,
                    "command": envelope.command or "unknown",
                },
            )
            # Mark attempt in spool, set next retry time
            self.spool.mark_attempt(msg_id)
            self._clear_progress(msg_id)
            logger.info("[TRANSPORT] Finished sending %s", msg_id)
            return

        # 3. Send the chunk
        chunk = chunks[next_seq - 1]  # seq is 1-based
        destination = entry.destination
        
        try:
            self.radio.send(destination, chunk)
            logger.debug("[TRANSPORT] Sent chunk %d/%d for %s", next_seq, len(chunks), msg_id)
            self._inc_sent_chunks(msg_id)
            
            self._metrics.inc(
                "transport_chunks_total",
                labels={"direction": "outbound", "command": envelope.command or "unknown"},
            )
            
            # Update progress
            self._advance_progress(msg_id)
            
            # "Touch" the spool entry so it doesn't expire while we are actively sending
            self.spool.touch(msg_id)
            
        except Exception as e:
            logger.error("[TRANSPORT] Failed to send chunk %d for %s: %s", next_seq, msg_id, e)
            # Mark the failed attempt and clear in-memory state to avoid rapid retry loops
            if self.spool:
                self.spool.mark_attempt(msg_id)
            self._clear_progress(msg_id)



    def _get_or_create_chunks(self, msg_id: str, envelope: MessageEnvelope) -> List[bytes]:
        """Create chunks for a message, validating size constraints."""
        if msg_id not in self._active_chunks:
            chunks = list(chunk_envelope(envelope, self.segment_size))
            # Validate chunk sizes against limit
            oversized = [i for i, chunk in enumerate(chunks, 1) if len(chunk) > MAX_CHUNK_SIZE]
            if oversized:
                logger.error(
                    "[TRANSPORT] Chunks %s for message %s exceed %d bytes with segment_size=%d. "
                    "Reduce segment_size to avoid transmission failures.",
                    oversized,
                    msg_id,
                    MAX_CHUNK_SIZE,
                    self.segment_size,
                )
                # Auto-reduce segment size and retry
                reduced_size = max(MIN_SEGMENT_SIZE, self.segment_size - SEGMENT_SIZE_REDUCTION)
                logger.warning(
                    "[TRANSPORT] Auto-reducing segment_size from %d to %d for %s",
                    self.segment_size,
                    reduced_size,
                    msg_id,
                )
                chunks = list(chunk_envelope(envelope, reduced_size))
            self._active_chunks[msg_id] = chunks
            if chunks:
                self._cache_chunks(self._message_prefix(msg_id), chunks)
        return self._active_chunks[msg_id]

    def _get_next_seq(self, msg_id: str) -> int:
        return self._active_progress.get(msg_id, 1)

    def _advance_progress(self, msg_id: str) -> None:
        current = self._active_progress.get(msg_id, 1)
        self._active_progress[msg_id] = current + 1

    def _clear_progress(self, msg_id: str) -> None:
        self._active_chunks.pop(msg_id, None)
        self._active_progress.pop(msg_id, None)

    def _resolve_chunk_delay(self, total_chunks: int, chunk_delay: float | None) -> float:
        """Resolve effective inter-chunk delay for a send operation."""
        if chunk_delay is not None:
            return max(0.0, float(chunk_delay))

        threshold = self._chunk_delay_threshold
        if threshold is None or self._chunk_delay_value <= 0:
            return 0.0

        if total_chunks >= max(1, int(threshold)):
            return self._chunk_delay_value
        return 0.0

    def send_message(
        self,
        envelope: MessageEnvelope,
        destination: str,
        chunk_delay: float | None = None,
        on_chunk_sent: Callable[[int, int], None] | None = None,
    ) -> None:
        """Send a message immediately (blocking) or enqueue for async sending.
        
        When spool is enabled, messages are enqueued for async sending via tick().
        When spool is disabled, messages are sent immediately for backward compatibility.
        """
        if self._enable_spool and self.spool is not None:
            logger.warning("[TRANSPORT] send_message is deprecated; use enqueue() for non-blocking behavior")
            self.enqueue(envelope, destination)
        else:
            # Direct send for backward compatibility when spool is disabled
            chunks = list(chunk_envelope(envelope, self.segment_size))
            # Validate chunk sizes against limit (mirror _get_or_create_chunks)
            oversized = [i for i, chunk in enumerate(chunks, 1) if len(chunk) > MAX_CHUNK_SIZE]
            if oversized:
                logger.error(
                    "[TRANSPORT] Chunks %s for message %s exceed %d bytes with segment_size=%d. "
                    "Reduce segment_size to avoid transmission failures.",
                    oversized,
                    envelope.id,
                    MAX_CHUNK_SIZE,
                    self.segment_size,
                )
                reduced_size = max(MIN_SEGMENT_SIZE, self.segment_size - SEGMENT_SIZE_REDUCTION)
                logger.warning(
                    "[TRANSPORT] Auto-reducing segment_size from %d to %d for %s",
                    self.segment_size,
                    reduced_size,
                    envelope.id,
                )
                chunks = list(chunk_envelope(envelope, reduced_size))
            total_chunks = len(chunks)
            if not chunks:
                return
            self._cache_chunks(self._message_prefix(envelope.id), chunks)
            effective_chunk_delay = self._resolve_chunk_delay(total_chunks, chunk_delay)
            if self.reliability:
                try:
                    self.reliability.on_send(self, envelope, destination, total_chunks)
                except Exception as e:
                    logger.warning(
                        "[TRANSPORT] Reliability on_send hook failed for %s: %s",
                        envelope.id,
                        e,
                    )
            for idx, chunk in enumerate(chunks, start=1):
                self.radio.send(destination, chunk)
                self._inc_sent_chunks(envelope.id)
                if on_chunk_sent:
                    on_chunk_sent(idx, total_chunks)
                self._metrics.inc(
                    "transport_chunks_total",
                    labels={"direction": "outbound", "command": envelope.command or "unknown"},
                )
                if effective_chunk_delay > 0 and idx < total_chunks:
                    time.sleep(effective_chunk_delay)
            self._metrics.inc(
                "transport_messages_total",
                labels={
                    "direction": "outbound",
                    "type": envelope.type,
                    "command": envelope.command or "unknown",
                },
            )
            if self.reliability:
                try:
                    self.reliability.on_chunks_sent(self, envelope, destination, total_chunks)
                except Exception as e:
                    logger.warning(
                        "[TRANSPORT] Reliability on_chunks_sent hook failed for %s: %s",
                        envelope.id,
                        e,
                    )

    def _record_progress(
        self, chunk_id: str, chunk_seq: int, chunk_total: int, is_ack: bool
    ) -> None:
        now = time.time()
        self._last_progress[chunk_id] = ChunkProgress(
            message_id=chunk_id,
            seq=chunk_seq,
            total=chunk_total,
            timestamp=now,
            is_ack=is_ack,
        )
        # Periodic cleanup instead of every call (O(n) -> amortized O(1))
        self._maybe_cleanup_progress(now)

    def _maybe_cleanup_progress(self, now: float) -> None:
        """Cleanup stale progress entries periodically, not on every call."""
        self._cleanup_counter += 1
        time_since_cleanup = now - self._last_cleanup_time
        
        # Cleanup if enough time passed OR enough operations occurred
        if time_since_cleanup < self._cleanup_interval and self._cleanup_counter < self._cleanup_every_n:
            return
        
        self._cleanup_counter = 0
        self._last_cleanup_time = now
        cutoff = now - self._progress_ttl
        
        # Cleanup progress dict
        stale_ids = [
            key for key, progress in self._last_progress.items() if progress.timestamp < cutoff
        ]
        for key in stale_ids:
            del self._last_progress[key]
        
        # Also cleanup chunk cache while we're at it
        self._prune_chunk_cache(now)

    def receive_message(
        self, timeout: float = 0.5
    ) -> Tuple[Optional[str], Optional[MessageEnvelope]]:
        """Receive and reassemble chunked messages."""

        receive_start = time.time()
        deadline = receive_start + timeout
        chunks_received = 0

        while time.time() <= deadline:
            remaining = deadline - time.time()
            if remaining <= 0:
                break

            receive_timeout = max(0.05, min(remaining, 0.5))
            received = self.radio.receive(receive_timeout)
            if received is None:
                # Reduced sleep: 2ms when actively receiving, longer otherwise
                # This balances latency vs CPU usage
                sleep_time = 0.002 if chunks_received > 0 else 0.005
                time.sleep(sleep_time)
                continue

            chunks_received += 1
            sender, chunk_bytes = received

            try:
                flags, chunk_id, chunk_seq, chunk_total, chunk_payload = parse_chunk(chunk_bytes)
                self._record_progress(
                    chunk_id=chunk_id,
                    chunk_seq=chunk_seq,
                    chunk_total=chunk_total,
                    is_ack=bool(flags & FLAG_ACK),
                )
                if self.reliability.handle_control(flags, chunk_id, chunk_payload, sender, self):
                    continue

                logger.info(
                    "[TRANSPORT] Received chunk %s/%s (ID: %s) from %s",
                    chunk_seq,
                    chunk_total,
                    chunk_id,
                    sender,
                )
                self._metrics.inc(
                    "transport_chunks_total",
                    labels=_LABELS_CHUNKS_INBOUND,
                )
            except ValueError as e:
                logger.warning("[TRANSPORT] Failed to parse chunk: %s", e)
                continue

            message, missing = self.reassembler.add_chunk_with_missing(chunk_bytes)
            if missing:
                self.reliability.on_missing(sender, chunk_id, missing, self)

            if message:
                total_time = time.time() - receive_start
                # Optional application-level ACK/NACK behaviour
                self.reliability.on_complete(sender, message, self)
                logger.info(
                    "[TRANSPORT] Reassembled message %s (%d chunks) in %.3fs from %s",
                    message.id[:8],
                    chunks_received,
                    total_time,
                    sender,
                )
                self._metrics.inc(
                    "transport_messages_total",
                    labels={
                        "direction": "inbound",
                        "type": message.type,
                        "command": message.command or "unknown",
                    },
                )
                self._metrics.observe(
                    "transport_reassembly_seconds",
                    total_time,
                    labels={"command": message.command or "unknown"},
                    buckets=DEFAULT_LATENCY_BUCKETS,
                )
                return sender, message

        return None, None

    def process_outbox(self) -> None:
        """Public shim for internal outbox processing; intended for gateway/client usage."""
        self._process_outbox()

    def _process_outbox(self) -> None:
        """Resend any pending messages that have not been acknowledged."""
        if not self.spool:
            return
        # Use tick-based sending - process all due messages
        due_messages = list(self.spool.due())
        for _ in due_messages:
            self.tick()

    def _cache_chunks(self, message_prefix: str, chunks: List[bytes]) -> None:
        """Persist chunks in memory for targeted resends."""
        now = time.time()
        self._chunk_cache[message_prefix] = {idx: chunk for idx, chunk in enumerate(chunks, 1)}
        self._chunk_cache_expiry[message_prefix] = now + self._progress_ttl
        self._prune_chunk_cache(now)

    def _drop_chunk_cache(self, message_prefix: str) -> None:
        self._chunk_cache.pop(message_prefix, None)
        self._chunk_cache_expiry.pop(message_prefix, None)

    def _prune_chunk_cache(self, now: float | None = None) -> None:
        now = now or time.time()
        expired = [prefix for prefix, expiry in self._chunk_cache_expiry.items() if expiry < now]
        for prefix in expired:
            self._drop_chunk_cache(prefix)

    def _handle_nack(self, sender: str, message_prefix: str, missing: List[int]) -> None:
        """Resend only the requested chunks when a NACK is received."""
        # Note: chunk cache is now pruned periodically via _maybe_cleanup_progress
        cache = self._chunk_cache.get(message_prefix)
        if not cache:
            logger.debug(
                "[TRANSPORT] No cached chunks for %s; ignoring NACK %s", message_prefix, missing
            )
            return
        logger.info(
            "[TRANSPORT] Resending %d chunks for %s to %s (missing: %s)",
            len(missing),
            message_prefix,
            sender,
            missing,
        )
        for seq in missing:
            chunk = cache.get(seq)
            if not chunk:
                continue
            try:
                self.radio.send(sender, chunk)
                self._inc_sent_chunks(message_prefix)
                self._metrics.inc(
                    "transport_chunks_total",
                    labels=_LABELS_CHUNKS_OUTBOUND_NACK,
                )
                time.sleep(RETRY_CHUNK_DELAY)
            except Exception:
                logger.debug(
                    "[TRANSPORT] Failed to resend chunk %s/%s to %s",
                    seq,
                    message_prefix,
                    sender,
                    exc_info=True,
                )
        # Refresh expiry after resends
        self._chunk_cache_expiry[message_prefix] = time.time() + self._progress_ttl

    def _lease_for(self, envelope: MessageEnvelope) -> Optional[float]:
        """Optional per-message lease override."""
        return (envelope.meta or {}).get("lease_seconds")

    def last_chunk_progress(self, message_id: str | None = None) -> Optional[ChunkProgress]:
        """Expose the last observed chunk/ack for a specific message prefix."""
        if not message_id:
            return None
        prefix = message_id[:8]
        return self._last_progress.get(prefix)

    def build_dedupe_keys(self, sender: str, envelope: MessageEnvelope):
        return build_dedupe_keys(sender, envelope)

    def should_process(self, sender: str, envelope: MessageEnvelope) -> bool:
        if self._disable_dedupe:
            logger.debug(
                "[TRANSPORT] Dedupe disabled; processing request %s from %s",
                envelope.id[:8],
                sender,
            )
            return True
        keys = self.build_dedupe_keys(sender, envelope)
        lease_seconds = self._lease_for(envelope)
        key_list = [keys.message]
        if keys.semantic:
            key_list.append(keys.semantic)
        elif keys.correlation:
            key_list.append(keys.correlation)
        elif keys.correlation is None:
            # no semantic, no correlation; nothing extra
            pass
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "[TRANSPORT] Dedupe keys for %s from %s: %s (lease=%.1fs)",
                envelope.id[:8],
                sender,
                key_list,
                lease_seconds or 0.0,
            )
        allowed = not self.deduper.check_keys(key_list, lease_seconds=lease_seconds)
        if not allowed:
            logger.info(
                "[TRANSPORT] Dedupe blocked request %s from %s (keys=%s)",
                envelope.id[:8],
                sender,
                key_list,
            )
        return allowed
