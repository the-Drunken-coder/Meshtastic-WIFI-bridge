from __future__ import annotations

from collections import OrderedDict
import threading
import time
from typing import Hashable, Iterable, NamedTuple, Optional

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from message import MessageEnvelope


class DedupeKeys(NamedTuple):
    message: Hashable
    correlation: Optional[Hashable]
    semantic: Optional[Hashable]


class RequestDeduper:
    def __init__(self, max_entries: int = 256, lease_seconds: float = 300.0) -> None:
        self._seen: "OrderedDict[Hashable, float]" = OrderedDict()
        self._in_progress: "OrderedDict[Hashable, float]" = OrderedDict()
        self._max = max_entries
        self._lease = lease_seconds
        self._lock = threading.Lock()

    @property
    def lease_seconds(self) -> float:
        return self._lease

    def _now(self) -> float:
        return time.monotonic()

    def _purge_expired(self, now: float) -> None:
        expired_seen = [key for key, expiry in list(self._seen.items()) if expiry <= now]
        for key in expired_seen:
            self._seen.pop(key, None)

        expired_progress = [key for key, expiry in list(self._in_progress.items()) if expiry <= now]
        for key in expired_progress:
            self._in_progress.pop(key, None)

    def _enforce_limit(self, target: "OrderedDict[Hashable, float]") -> None:
        while len(target) > self._max:
            target.popitem(last=False)

    def _mark_seen(self, keys: Iterable[Hashable], expires_at: float, enforce_limit: bool = True) -> None:
        for key in keys:
            # Refresh position to keep most recently used semantics
            self._seen.pop(key, None)
            self._seen[key] = expires_at
        if enforce_limit:
            self._enforce_limit(self._seen)

    def check_keys(self, keys: Iterable[Hashable], lease_seconds: Optional[float] = None) -> bool:
        """Check multiple keys atomically, applying a lease if they are new."""
        lease = lease_seconds or self._lease
        now = self._now()
        with self._lock:
            self._purge_expired(now)

            for key in keys:
                if key in self._in_progress:
                    return True
                if key in self._seen:
                    self._seen.move_to_end(key)
                    return True

            self._mark_seen(keys, now + lease)
            return False

    def seen(self, key: Hashable, lease_seconds: Optional[float] = None) -> bool:
        """Backwards-compatible wrapper for single-key dedupe checks."""
        return self.check_keys([key], lease_seconds=lease_seconds)

    def acquire_lease(self, key: Hashable, lease_seconds: Optional[float] = None) -> bool:
        """Acquire an in-progress lease for a key. Returns False if already leased."""
        lease = lease_seconds or self._lease
        now = self._now()
        with self._lock:
            self._purge_expired(now)
            if key in self._in_progress:
                return False
            self._in_progress[key] = now + lease
            self._in_progress.move_to_end(key)
            self._enforce_limit(self._in_progress)
            return True

    def release_lease(
        self, key: Hashable, lease_seconds: Optional[float] = None, remember: bool = True
    ) -> None:
        """Release an in-progress lease and optionally mark the key as seen."""
        lease = lease_seconds or self._lease
        now = self._now()
        with self._lock:
            self._in_progress.pop(key, None)
            self._purge_expired(now)
            if remember:
                # Avoid immediate LRU eviction when finishing an operation; defer size enforcement.
                self._mark_seen([key], now + lease, enforce_limit=False)
                if len(self._seen) > self._max * 2:
                    self._enforce_limit(self._seen)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "seen": len(self._seen),
                "in_progress": len(self._in_progress),
                "max_entries": self._max,
            }


def build_dedupe_keys(sender: str, envelope: "MessageEnvelope") -> DedupeKeys:
    """Generate message, correlation, and semantic keys for deduplication."""
    data = envelope.data or {}
    correlation_id = getattr(envelope, "correlation_id", None)

    message_key: Hashable = (sender, envelope.command, envelope.id)
    correlation_key: Optional[Hashable] = None
    semantic_key: Optional[Hashable] = None

    if correlation_id:
        correlation_key = (sender, envelope.command, "corr", correlation_id)

    meta = getattr(envelope, "meta", None) or {}
    semantic_hint = meta.get("semantic_key") or meta.get("dedupe_key") or data.get("dedupe_key")
    if semantic_hint is not None:
        semantic_key = (sender, envelope.command, "semantic", str(semantic_hint))

    return DedupeKeys(message=message_key, correlation=correlation_key, semantic=semantic_key)
