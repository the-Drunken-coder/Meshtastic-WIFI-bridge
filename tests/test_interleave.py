import os
import shutil
import tempfile
import sys
from pathlib import Path
import pytest

# Add src to path
SRC_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_DIR))

from transport import MeshtasticTransport
from message import MessageEnvelope

class MockRadio:
    def __init__(self):
        self.sent_chunks = []
    
    def send(self, destination, chunk):
        self.sent_chunks.append(chunk)

@pytest.fixture
def transport():
    test_dir = tempfile.mkdtemp()
    spool_path = os.path.join(test_dir, "test_spool.json")
    radio = MockRadio()
    transport = MeshtasticTransport(radio, spool_path=spool_path, enable_spool=True)
    yield transport
    shutil.rmtree(test_dir)

def test_interleave(transport):
    """Verify that a high priority message preempts a low priority one."""
    # 1. Start a large Low Priority message
    payload = {"data": "x" * 500} 
    msg_low = MessageEnvelope(id="low_1", type="test", command="l", priority=20, data=payload)
    
    # 2. Add High Priority message
    msg_high = MessageEnvelope(id="high_1", type="test", command="h", priority=0, data={"tiny": "load"})
    
    # Enqueue Low first
    transport.enqueue(msg_low, "dest")
    
    # Tick once - should send chunk 1 of Low
    transport.tick()
    assert len(transport.radio.sent_chunks) == 1
    
    # Enqueue High
    transport.enqueue(msg_high, "dest")
    
    # Tick 2 - should send High message's ONLY chunk
    transport.tick()
    assert len(transport.radio.sent_chunks) == 2
    
    # Tick 3 - should finish High message send cycle
    transport.tick()
    
    # Verify that the high priority message is no longer "due" (it finished its attempt)
    due_ids = [m[0] for m in transport.spool.due()]
    assert "high_1" not in due_ids
    assert "low_1" in due_ids


def test_enqueue_with_spool_disabled(caplog):
    """Verify that messages are dropped with logging when spool is disabled."""
    import logging
    
    # Create a transport with spool disabled
    radio = MockRadio()
    transport = MeshtasticTransport(radio, spool_path=None, enable_spool=False)
    
    msg = MessageEnvelope(id="test_1", type="request", command="test", priority=10, data={"key": "value"})
    
    with caplog.at_level(logging.WARNING):
        transport.enqueue(msg, "dest")
    
    # Verify warning was logged
    assert any("Dropping message because spool is disabled" in record.message for record in caplog.records)
    
    # Verify message was not sent
    assert len(radio.sent_chunks) == 0
    
    # Tick should not send anything either
    transport.tick()
    assert len(radio.sent_chunks) == 0


def test_enqueue_without_spool_logs_drop(caplog):
    """Verify that messages without spool enabled are logged as dropped."""
    import logging
    
    # Create a transport with spool path but spool disabled
    test_dir = tempfile.mkdtemp()
    try:
        spool_path = os.path.join(test_dir, "test_spool.json")
        radio = MockRadio()
        transport = MeshtasticTransport(radio, spool_path=spool_path, enable_spool=False)
        
        msg = MessageEnvelope(id="test_1", type="request", command="test", priority=10, data={"key": "value"})
        
        with caplog.at_level(logging.WARNING):
            transport.enqueue(msg, "dest")
        
        # Verify warning was logged
        assert any("Dropping message because spool is disabled" in record.message for record in caplog.records)
        
        # Verify message was not sent
        assert len(radio.sent_chunks) == 0
    finally:
        shutil.rmtree(test_dir)

