import os
import shutil
import tempfile
import sys
from pathlib import Path
import pytest

# Add src to path
SRC_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_DIR))

from spool import PersistentSpool
from message import MessageEnvelope

@pytest.fixture
def spool():
    test_dir = tempfile.mkdtemp()
    spool_path = os.path.join(test_dir, "test_spool.json")
    spool = PersistentSpool(spool_path)
    yield spool
    shutil.rmtree(test_dir)

def test_priority_sorting(spool):
    """Verify that due() returns messages sorted by priority (asc) then time."""
    # Create messages with different priorities
    msg_crit = MessageEnvelope(id="crit_1", type="test", command="c", priority=0)
    msg_high = MessageEnvelope(id="high_1", type="test", command="h", priority=5)
    msg_norm = MessageEnvelope(id="norm_1", type="test", command="n", priority=10)
    msg_low  = MessageEnvelope(id="low_1",  type="test", command="l", priority=20)
    msg_norm2 = MessageEnvelope(id="norm_2", type="test", command="n2", priority=10)

    # Add them in random order
    spool.add(msg_norm, "dest")
    spool.add(msg_low, "dest")
    spool.add(msg_crit, "dest")
    spool.add(msg_high, "dest")
    spool.add(msg_norm2, "dest")

    # Get due messages
    due = spool.due()
    
    assert len(due) == 5
    
    # Verify order
    assert due[0][0] == "crit_1"
    assert due[1][0] == "high_1"
    
    priorities = [d[1].priority for d in due]
    assert priorities == [0, 5, 10, 10, 20]
    
    assert due[4][0] == "low_1"
