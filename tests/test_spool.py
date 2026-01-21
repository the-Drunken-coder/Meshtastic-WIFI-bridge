import time

from message import MessageEnvelope
from spool import PersistentSpool


def test_spool_add_and_due_returns_copy(tmp_path) -> None:
    path = tmp_path / "spool.json"
    spool = PersistentSpool(str(path), base_delay=1, jitter=0)
    envelope = MessageEnvelope(id="msg-1", type="request", command="ping", data={})

    spool.add(envelope, "dest")
    due_entries = spool.due(now=time.time() + 2)
    assert len(due_entries) == 1
    msg_id, entry = due_entries[0]
    assert msg_id == "msg-1"

    # Mutate returned entry; internal entry should remain unchanged
    entry.attempts = 5
    snapshot = spool.due(now=time.time() + 2)
    _, fresh_entry = snapshot[0]
    assert fresh_entry.attempts == 0


def test_spool_backoff_capped(tmp_path) -> None:
    path = tmp_path / "spool_cap.json"
    spool = PersistentSpool(str(path), base_delay=1, jitter=0)
    envelope = MessageEnvelope(id="msg-2", type="request", command="ping", data={})
    spool.add(envelope, "dest")

    now = time.time()
    spool.mark_attempt("msg-2")
    first_delay = spool._entries["msg-2"].next_retry - now
    assert 0.9 <= first_delay <= 1.1

    for _ in range(10):
        spool.mark_attempt("msg-2")
    capped_delay = spool._entries["msg-2"].next_retry - time.time()
    assert capped_delay <= 16.1  # base_delay * _MAX_BACKOFF_MULTIPLIER


def test_spool_expires_entries(tmp_path) -> None:
    path = tmp_path / "spool_expire.json"
    spool = PersistentSpool(str(path), base_delay=1, jitter=0, expiry_seconds=1)
    envelope = MessageEnvelope(id="msg-3", type="request", command="ping", data={})
    spool.add(envelope, "dest")

    # Age the entry via last_activity to trigger expiry
    spool._entries["msg-3"].last_activity -= 5
    spool._entries["msg-3"].next_retry = time.time() - 1  # ensure eligible if not expired
    due = spool.due(now=time.time())
    assert due == []
    assert spool.has("msg-3") is False


def test_spool_recovers_from_corrupt_file(tmp_path) -> None:
    path = tmp_path / "spool_corrupt.json"
    path.write_text("{not-json")
    spool = PersistentSpool(str(path), base_delay=1, jitter=0)
    assert spool.due() == []
