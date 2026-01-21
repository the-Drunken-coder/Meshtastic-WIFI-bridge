"""Unit tests for RequestDeduper."""

import time

from dedupe import RequestDeduper


def test_dedupe_first_seen_returns_false() -> None:
    """Test that seeing a key for the first time returns False."""
    deduper = RequestDeduper()
    key = ("sender", "command", "id123")

    assert deduper.seen(key) is False


def test_dedupe_duplicate_returns_true() -> None:
    """Test that seeing the same key twice returns True."""
    deduper = RequestDeduper()
    key = ("sender", "command", "id123")

    deduper.seen(key)
    assert deduper.seen(key) is True


def test_dedupe_lru_eviction() -> None:
    """Test that old entries are evicted when max_entries is exceeded."""
    deduper = RequestDeduper(max_entries=3)

    key1 = ("sender", "cmd1", "id1")
    key2 = ("sender", "cmd2", "id2")
    key3 = ("sender", "cmd3", "id3")
    key4 = ("sender", "cmd4", "id4")

    # Add first 3 keys
    assert deduper.seen(key1) is False  # First time
    assert deduper.seen(key2) is False  # First time
    assert deduper.seen(key3) is False  # First time

    # Verify they're all seen now (doesn't change order since checking in order)
    assert deduper.seen(key1) is True
    assert deduper.seen(key2) is True
    assert deduper.seen(key3) is True

    # Now order is: key1, key2, key3 (key1 least recently accessed)
    # Add 4th key, should evict key1 (oldest)
    assert deduper.seen(key4) is False  # First time

    # Now we have: key2, key3, key4
    # Check that key1 was evicted - it should return False (and re-add it, evicting key2)
    assert deduper.seen(key1) is False  # Was evicted, so first time again


def test_dedupe_move_to_end() -> None:
    """Test that accessing a key moves it to the end (most recently used)."""
    deduper = RequestDeduper(max_entries=2)

    key1 = ("sender", "cmd1", "id1")
    key2 = ("sender", "cmd2", "id2")
    key3 = ("sender", "cmd3", "id3")

    # Add two keys
    assert deduper.seen(key1) is False  # First time
    assert deduper.seen(key2) is False  # First time

    # Access key1 again (moves to end)
    assert deduper.seen(key1) is True

    # Add key3 - should evict key2 (least recently used)
    # key1 was just accessed, key2 is oldest
    assert deduper.seen(key3) is False  # First time, evicts key2

    # key1 and key3 should still be there
    assert deduper.seen(key1) is True
    assert deduper.seen(key3) is True
    # key2 should be evicted
    assert deduper.seen(key2) is False  # Evicted, so first time again


def test_dedupe_lease_expiration() -> None:
    """Entries should expire after the lease duration."""
    deduper = RequestDeduper(max_entries=4, lease_seconds=0.1)
    key = ("sender", "cmd", "id-lease")

    assert deduper.seen(key) is False
    assert deduper.seen(key) is True

    # After the lease window, the key should be treated as new again
    time.sleep(0.15)
    assert deduper.seen(key) is False


def test_dedupe_check_keys_atomic() -> None:
    """check_keys should treat multiple keys atomically."""
    deduper = RequestDeduper(max_entries=4)
    keys = [("sender", "cmd", "id1"), ("semantic", "job", "123")]

    assert deduper.check_keys(keys) is False
    assert deduper.check_keys(keys) is True


def test_dedupe_in_progress_leases() -> None:
    """In-progress leases block duplicates until released."""
    deduper = RequestDeduper(max_entries=4)
    key = ("job", "start", "123")

    assert deduper.acquire_lease(key) is True
    assert deduper.acquire_lease(key) is False

    deduper.release_lease(key)

    # After release, the completion is remembered and a fresh lease can be taken again
    assert deduper.seen(key) is True
    assert deduper.acquire_lease(key) is True
