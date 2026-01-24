"""Message reassembly for chunked Meshtastic communication."""

from __future__ import annotations

import logging
import time
from typing import Dict, Optional, TypedDict, List, Set, Tuple

from message import MessageEnvelope, parse_chunk, reconstruct_message

logger = logging.getLogger(__name__)


class MessageBucket(TypedDict):
    """Type definition for message reassembly bucket."""
    received: Dict[int, bytes]
    total: int
    created: float
    ttl: float


class MessageReassembler:
    def __init__(
        self,
        ttl_seconds: float = 120.0,
        per_chunk_ttl: float = 2.0,
        max_ttl: float = 600.0,
        nack_max_per_seq: int = 5,
        nack_interval: float = 1.0,
        extend_short_ttl: bool = False,
    ) -> None:
        """Configure message reassembly and expiry behaviour.

        Parameters
        ----------
        ttl_seconds:
            Base time-to-live (TTL) in seconds for a reassembly bucket. This is
            the minimum lifetime that any partially received message will be
            kept before it is considered expired and removed.
        per_chunk_ttl:
            Additional TTL in seconds to add for each chunk beyond the first.
            The effective TTL is clamped between ``ttl_seconds`` and ``max_ttl``
            (see ``_effective_ttl``) so larger messages get proportionally more
            time to arrive.
        max_ttl:
            Upper bound, in seconds, for the effective TTL of any message. The
            effective TTL will never exceed this value.
        nack_max_per_seq:
            Maximum number of NACKs that will be sent for a given missing
            sequence number within a single message, preventing unbounded NACK
            traffic for stubborn gaps.
        nack_interval:
            Minimum time in seconds between sending NACKs for the same missing
            sequence set in a given message.
        extend_short_ttl:
            When ``False`` (default), per-chunk TTL extension is disabled if
            ``ttl_seconds`` is smaller than ``per_chunk_ttl`` to avoid
            unintentionally stretching very short TTLs (e.g., in tests). Set to
            ``True`` to allow per-chunk extension even with very small base
            TTLs.
        """
        self._buckets: Dict[str, MessageBucket] = {}
        self._base_ttl = ttl_seconds
        # Avoid extending TTL when a very small base TTL is requested unless explicitly allowed
        self._per_chunk_ttl = per_chunk_ttl if (extend_short_ttl or ttl_seconds >= per_chunk_ttl) else 0
        self._max_ttl = max(max_ttl, ttl_seconds)
        self._nack_max_per_seq = max(1, nack_max_per_seq)
        self._nack_interval = nack_interval
        # Tracks last NACK sent for a chunk_id: (missing set, timestamp)
        self._nack_state: Dict[str, Tuple[Set[int], float]] = {}
        # Tracks how many times we've NACKed a given seq
        self._nack_counts: Dict[str, Dict[int, int]] = {}

    @property
    def max_ttl(self) -> float:
        return self._max_ttl

    def _effective_ttl(self, total_chunks: int) -> float:
        dynamic_ttl = self._base_ttl + max(0, total_chunks - 1) * self._per_chunk_ttl
        return min(self._max_ttl, max(self._base_ttl, dynamic_ttl))

    def add_chunk(self, chunk: bytes) -> Optional[MessageEnvelope]:
        """Add a chunk and return a completed envelope if available."""
        message, _ = self._add_chunk(chunk)
        return message

    def add_chunk_with_missing(
        self, chunk: bytes
    ) -> Tuple[Optional[MessageEnvelope], Optional[List[int]]]:
        """Add a chunk and return both the message (if complete) and any missing sequences."""
        return self._add_chunk(chunk)

    def _add_chunk(
        self, chunk: bytes
    ) -> Tuple[Optional[MessageEnvelope], Optional[List[int]]]:
        """Internal implementation for adding a chunk to the reassembly state.

        Parameters
        ----------
        chunk:
            Raw Meshtastic chunk bytes containing header and payload data.

        Returns
        -------
        Tuple[Optional[MessageEnvelope], Optional[List[int]]]
            ``message`` is returned when all chunks for the envelope are
            present; otherwise ``None``. ``missing_sequences`` lists sequence
            numbers that should be NACKed, or ``None`` when no NACK should be
            sent (including throttle conditions).

        Notes
        -----
        - Buckets track ``created`` timestamp and a per-bucket ``ttl`` derived
          from :meth:`_effective_ttl`; they are removed when expired.
        - NACK emission is throttled via :meth:`_should_nack` and per-sequence
          caps stored in ``_nack_counts`` to avoid noisy retransmissions.
        """
        now = time.time()
        try:
            _flags, chunk_id, chunk_seq, chunk_total, chunk_data = parse_chunk(chunk)
        except ValueError as exc:
            logger.debug("[REASSEMBLY] Failed to parse chunk: %s", exc)
            return None, None

        bucket = self._buckets.setdefault(
            chunk_id,
            MessageBucket(
                received={},
                total=chunk_total,
                created=now,
                ttl=self._effective_ttl(chunk_total),
            ),
        )
        # Update TTL if total_chunks increases
        effective_ttl = self._effective_ttl(chunk_total)
        if effective_ttl > bucket["ttl"]:
            bucket["ttl"] = effective_ttl
        if chunk_id not in self._nack_counts:
            self._nack_counts[chunk_id] = {}

        # Deduplicate: if we already have this chunk, skip it
        if chunk_seq in bucket["received"]:
            logger.debug(
                "[REASSEMBLY] Duplicate chunk %d/%d for %s (ignored)",
                chunk_seq,
                chunk_total,
                chunk_id[:8],
            )
            return None, None

        bucket["received"][chunk_seq] = chunk_data
        bucket["total"] = chunk_total

        # Log progress
        received_count = len(bucket["received"])
        logger.info(
            "[REASSEMBLY] Chunk %d/%d for %s (%d/%d received)",
            chunk_seq,
            chunk_total,
            chunk_id[:8],
            received_count,
            chunk_total,
        )

        # Check TTL
        if now - bucket["created"] > bucket["ttl"]:
            logger.warning("[REASSEMBLY] Message %s expired (TTL exceeded)", chunk_id[:8])
            del self._buckets[chunk_id]
            return None, None

        # Check if complete
        if received_count == bucket["total"]:
            # Validate that we have a full, consecutive sequence of chunks
            expected_indices = set(range(1, bucket["total"] + 1))
            received_indices = set(bucket["received"].keys())
            if expected_indices != received_indices:
                missing = sorted(expected_indices - received_indices)
                unexpected = sorted(received_indices - expected_indices)
                logger.warning(
                    "[REASSEMBLY] Inconsistent chunk sequence for %s "
                    "(missing=%s, unexpected=%s); discarding message",
                    chunk_id[:8],
                    missing if missing else "[]",
                    unexpected if unexpected else "[]",
                )
                del self._buckets[chunk_id]
                self._nack_counts.pop(chunk_id, None)
                return None, None
            
            segments = [
                bucket["received"][index] for index in range(1, bucket["total"] + 1)
            ]
            logger.info("[REASSEMBLY] Complete: %s (%d chunks)", chunk_id[:8], chunk_total)
            del self._buckets[chunk_id]
            self._nack_counts.pop(chunk_id, None)
            self._nack_state.pop(chunk_id, None)
            message = reconstruct_message(segments)
            return message, None

        expected_indices = set(range(1, bucket["total"] + 1))
        received_indices = set(bucket["received"].keys())
        # Only NACK when there is an observed gap (missing below the highest seen seq)
        highest = max(received_indices) if received_indices else 0
        missing = {seq for seq in expected_indices if seq not in received_indices and seq < highest}
        missing_list: Optional[List[int]] = None
        if missing and self._should_nack(chunk_id, missing, now):
            filtered = []
            counts = self._nack_counts.setdefault(chunk_id, {})
            for seq in sorted(missing):
                attempts = counts.get(seq, 0)
                if attempts < self._nack_max_per_seq:
                    filtered.append(seq)
                    counts[seq] = attempts + 1
            if filtered:
                missing_list = filtered
                self._nack_state[chunk_id] = (set(filtered), now)
        return None, missing_list

    def prune(self) -> None:
        """Remove expired message buckets."""
        now = time.time()
        expired = [
            bucket_id
            for bucket_id, bucket in self._buckets.items()
            if now - bucket["created"] > bucket["ttl"]
        ]
        for bucket_id in expired:
            del self._buckets[bucket_id]
            self._nack_state.pop(bucket_id, None)
            self._nack_counts.pop(bucket_id, None)

    def missing_sequences(self, chunk_id: str, *, force: bool = False) -> Optional[List[int]]:
        """Return missing sequences for a message, optionally including trailing gaps."""
        bucket = self._buckets.get(chunk_id)
        if not bucket:
            return None
        expected_indices = set(range(1, bucket["total"] + 1))
        received_indices = set(bucket["received"].keys())
        highest = max(received_indices) if received_indices else 0
        missing = [
            seq
            for seq in sorted(expected_indices - received_indices)
            if force or seq < highest
        ]
        return missing or []

    def _should_nack(self, chunk_id: str, missing: Set[int], now: float) -> bool:
        last = self._nack_state.get(chunk_id)
        if last is None:
            return True
        last_missing, last_time = last
        if missing != last_missing:
            return True
        return (now - last_time) >= self._nack_interval
