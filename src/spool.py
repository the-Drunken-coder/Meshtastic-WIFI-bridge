from __future__ import annotations

import json
import logging
import math
import os
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from message import MessageEnvelope


@dataclass
class SpoolEntry:
    envelope: Dict[str, object]
    destination: str
    attempts: int = 0
    next_retry: float = field(default_factory=time.time)
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    priority: int = 10


class PersistentSpool:
    """Simple JSON-file backed spool for pending Meshtastic messages."""

    _MAX_BACKOFF_MULTIPLIER = 16.0

    def __init__(
        self,
        path: str,
        max_attempts: int = 5,
        base_delay: float = 2.0,
        jitter: float = 0.5,
        expiry_seconds: float = 86400.0,
    ) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._jitter = jitter
        self._expiry = expiry_seconds
        self._entries: Dict[str, SpoolEntry] = {}
        self._load()

    # Persistence helpers -------------------------------------------------
    def _load(self) -> None:
        if not os.path.exists(self._path):
            self._entries = {}
            return
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            entries = raw.get("entries", {})
            hydrated: Dict[str, SpoolEntry] = {}
            for msg_id, entry in entries.items():
                if "last_activity" not in entry:
                    entry["last_activity"] = entry.get("created_at", time.time())
                hydrated[msg_id] = SpoolEntry(**entry)
            self._entries = hydrated
        except (json.JSONDecodeError, OSError, PermissionError) as exc:
            # Corrupt or unreadable spool; start clean but do not raise
            logging.getLogger(__name__).warning(
                "Failed to load spool file %s: %s (starting clean)", self._path, exc
            )
            self._entries = {}

    def _flush(self) -> None:
        target_dir = os.path.dirname(self._path) or "."
        try:
            os.makedirs(target_dir, exist_ok=True)
            payload = {
                "entries": {msg_id: entry.__dict__ for msg_id, entry in self._entries.items()}
            }
            with open(self._path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
        except (OSError, PermissionError) as exc:
            logging.getLogger(__name__).error("Failed to flush spool file %s: %s", self._path, exc)

    # Public API ----------------------------------------------------------
    def add(self, envelope: MessageEnvelope, destination: str) -> None:
        with self._lock:
            if envelope.id not in self._entries:
                self._entries[envelope.id] = SpoolEntry(
                    envelope=envelope.to_dict(),
                    destination=destination,
                    priority=envelope.priority,
                )
                self._flush()

    def mark_attempt(self, message_id: str) -> None:
        with self._lock:
            entry = self._entries.get(message_id)
            if entry is None:
                return
            entry.attempts += 1
            max_power = math.log2(self._MAX_BACKOFF_MULTIPLIER)
            power = min(entry.attempts - 1, max_power)
            delay = self._base_delay * (2**power)
            delay += random.uniform(0, self._jitter)
            now = time.time()
            entry.next_retry = now + delay
            entry.last_activity = now
            self._flush()

    def ack(self, message_id: str) -> None:
        """Acknowledge and remove a message from the spool.
        
        Supports both exact match and prefix match (for 8-byte truncated IDs from ACKs).
        """
        with self._lock:
            # Try exact match first
            if message_id in self._entries:
                del self._entries[message_id]
                self._flush()
                return
            
            # Try prefix match (for truncated ACK IDs)
            # This handles the case where ACK contains only 8-byte prefix
            if len(message_id) == 8:
                matching = [
                    full_id for full_id in self._entries 
                    if full_id.startswith(message_id)
                ]
                if len(matching) == 1:
                    # Unique prefix match - safe to remove
                    del self._entries[matching[0]]
                    self._flush()

    def touch(self, message_id: str) -> None:
        """Refresh last_activity without changing retry state.

        This is intended for recording partial progress (for example, when chunks of a message
        are being received) without affecting the retry schedule. To reduce disk I/O, this
        method only updates in-memory state and does not trigger an immediate flush of the
        spool to disk. As a result, if the process crashes before another operation that calls
        _flush(), the updated last_activity timestamp may be lost and will be reconstructed
        from the last persisted state on restart.
        """
        with self._lock:
            entry = self._entries.get(message_id)
            if entry:
                entry.last_activity = time.time()
                # Don't flush for touch-only updates to reduce disk I/O; persistence is deferred

    def delay_retry(self, message_id: str, delay_seconds: float) -> None:
        """Push back the next retry time while a message is actively progressing.

        This is intended for extending the retry window when chunks are actively being
        received, without immediately persisting the updated schedule. To reduce disk I/O,
        this method only updates in-memory state and does not trigger an immediate flush of
        the spool to disk. If the process crashes before a subsequent operation that calls
        _flush(), the adjusted next_retry and last_activity values may be lost and will be
        reconstructed from the last persisted state on restart.
        """
        with self._lock:
            entry = self._entries.get(message_id)
            if entry:
                now = time.time()
                entry.last_activity = now
                entry.next_retry = max(entry.next_retry, now + delay_seconds)
                # Don't flush for delay-only updates to reduce disk I/O; persistence is deferred

    def due(self, now: float | None = None) -> List[Tuple[str, SpoolEntry]]:
        now = now or time.time()
        with self._lock:
            # Drop expired entries
            expired = [
                msg_id
                for msg_id, entry in self._entries.items()
                if (now - entry.last_activity) > self._expiry
            ]
            for msg_id in expired:
                del self._entries[msg_id]
            if expired:
                self._flush()

            ready: List[Tuple[str, SpoolEntry]] = []
            for msg_id, entry in self._entries.items():
                if entry.attempts >= self._max_attempts:
                    continue
                if entry.next_retry <= now:
                    ready_entry = SpoolEntry(
                        envelope=dict(entry.envelope),
                        destination=entry.destination,
                        attempts=entry.attempts,
                        next_retry=entry.next_retry,
                        created_at=entry.created_at,
                        last_activity=entry.last_activity,
                        priority=entry.priority,
                    )
                    ready.append((msg_id, ready_entry))
            
            # Sort by priority (asc) then next_retry (asc)
            # Lower priority value = higher importance (0=Critical, 10=Normal)
            ready.sort(key=lambda x: (x[1].priority, x[1].next_retry))
            return ready

    def has(self, message_id: str) -> bool:
        with self._lock:
            return message_id in self._entries

    def depth(self) -> int:
        with self._lock:
            return len(self._entries)
