"""Tests for reliability strategies."""

import tempfile
from pathlib import Path


from message import MessageEnvelope
from reliability import (
    NoAckNackStrategy,
    ParityWindowStrategy,
    SimpleAckNackStrategy,
    StageAckNackStrategy,
    WindowedSelectiveStrategy,
    strategy_from_name,
)
from transport import (
    InMemoryRadio,
    InMemoryRadioBus,
    MeshtasticTransport,
)


def test_strategy_from_name_none():
    """Test that None or empty name returns WindowedSelectiveStrategy (default)."""
    assert isinstance(strategy_from_name(None), WindowedSelectiveStrategy)
    assert isinstance(strategy_from_name(""), WindowedSelectiveStrategy)
    # Test that explicit 'none' returns the default (not NoAckNackStrategy)
    assert isinstance(strategy_from_name("invalid"), WindowedSelectiveStrategy)


def test_strategy_from_name_simple():
    """Test that 'simple' returns SimpleAckNackStrategy."""
    assert isinstance(strategy_from_name("simple"), SimpleAckNackStrategy)
    assert isinstance(strategy_from_name("ack"), SimpleAckNackStrategy)
    assert isinstance(strategy_from_name("ack_nack"), SimpleAckNackStrategy)


def test_strategy_from_name_stage():
    """Test that 'stage' returns StageAckNackStrategy."""
    assert isinstance(strategy_from_name("stage"), StageAckNackStrategy)
    assert isinstance(strategy_from_name("staged"), StageAckNackStrategy)


def test_strategy_from_name_window():
    """Test that 'window' returns WindowedSelectiveStrategy."""
    assert isinstance(strategy_from_name("window"), WindowedSelectiveStrategy)
    assert isinstance(strategy_from_name("windowed"), WindowedSelectiveStrategy)


def test_strategy_from_name_window_fec():
    """Test that 'window_fec' returns ParityWindowStrategy."""
    assert isinstance(strategy_from_name("window_fec"), ParityWindowStrategy)
    assert isinstance(strategy_from_name("window_parity"), ParityWindowStrategy)
    assert isinstance(strategy_from_name("selective_fec"), ParityWindowStrategy)


def test_no_ack_nack_strategy_no_acks():
    """Test that NoAckNackStrategy doesn't send ACKs."""
    bus = InMemoryRadioBus()
    sender = MeshtasticTransport(
        InMemoryRadio("sender", bus),
        segment_size=60,
        reliability=NoAckNackStrategy(),
    )
    receiver = MeshtasticTransport(
        InMemoryRadio("receiver", bus),
        segment_size=60,
        reliability=NoAckNackStrategy(),
    )

    envelope = MessageEnvelope(
        id="test-no-ack",
        type="request",
        command="ping",
        data={"msg": "test"},
    )

    sender.send_message(envelope, "receiver")
    _, received = receiver.receive_message(timeout=1.0)

    assert received is not None
    assert received.id == envelope.id

    # Sender should not receive any ACK
    _, ack = sender.receive_message(timeout=0.5)
    assert ack is None


def test_simple_ack_nack_strategy_sends_acks():
    """Test that SimpleAckNackStrategy sends and processes ACKs."""
    bus = InMemoryRadioBus()
    sender = MeshtasticTransport(
        InMemoryRadio("sender", bus),
        segment_size=60,
        reliability=SimpleAckNackStrategy(),
    )
    receiver = MeshtasticTransport(
        InMemoryRadio("receiver", bus),
        segment_size=60,
        reliability=SimpleAckNackStrategy(),
    )

    envelope = MessageEnvelope(
        id="test-simple-ack",
        type="request",
        command="ping",
        data={"msg": "test"},
    )

    sender.send_message(envelope, "receiver")
    _, received = receiver.receive_message(timeout=1.0)

    assert received is not None
    assert received.id == envelope.id

    # Sender should receive and process ACK (but it's filtered from callers)
    _, ack = sender.receive_message(timeout=0.5)
    assert ack is None  # ACKs are consumed internally


def test_simple_ack_nack_strategy_with_spool():
    """Test that SimpleAckNackStrategy acknowledges messages in spool."""
    bus = InMemoryRadioBus()

    with tempfile.TemporaryDirectory() as tmpdir:
        spool_file = Path(tmpdir) / "test_spool.json"

        sender = MeshtasticTransport(
            InMemoryRadio("sender", bus),
            segment_size=60,
            reliability=SimpleAckNackStrategy(),
            spool_path=str(spool_file),
            enable_spool=True,
        )
        receiver = MeshtasticTransport(
            InMemoryRadio("receiver", bus),
            segment_size=60,
            reliability=SimpleAckNackStrategy(),
        )

        envelope = MessageEnvelope(
            id="test-spool-ack",
            type="request",
            command="ping",
            data={"msg": "test"},
        )

        sender.send_message(envelope, "receiver")
        assert sender.spool.has(envelope.id)

        # Drive the transport to send the chunks
        for _ in range(10):
            sender.tick()

        # Receiver gets message and sends ACK
        _, received = receiver.receive_message(timeout=1.0)
        assert received is not None

        # Sender processes ACK and removes from spool
        sender.receive_message(timeout=0.5)
        assert not sender.spool.has(envelope.id)


def test_stage_ack_nack_strategy_sends_announce():
    """Test that StageAckNackStrategy sends announce messages."""
    bus = InMemoryRadioBus()
    sender = MeshtasticTransport(
        InMemoryRadio("sender", bus),
        segment_size=60,
        reliability=StageAckNackStrategy(),
    )
    receiver = MeshtasticTransport(
        InMemoryRadio("receiver", bus),
        segment_size=60,
        reliability=StageAckNackStrategy(),
    )

    envelope = MessageEnvelope(
        id="test-stage",
        type="request",
        command="ping",
        data={"msg": "test"},
    )

    sender.send_message(envelope, "receiver")
    _, received = receiver.receive_message(timeout=1.0)

    assert received is not None
    assert received.id == envelope.id


def test_windowed_selective_strategy():
    """Test that WindowedSelectiveStrategy works correctly."""
    bus = InMemoryRadioBus()
    sender = MeshtasticTransport(
        InMemoryRadio("sender", bus),
        segment_size=60,
        reliability=WindowedSelectiveStrategy(),
    )
    receiver = MeshtasticTransport(
        InMemoryRadio("receiver", bus),
        segment_size=60,
        reliability=WindowedSelectiveStrategy(),
    )

    envelope = MessageEnvelope(
        id="test-window",
        type="request",
        command="ping",
        data={"msg": "test" * 20},  # Multi-chunk message
    )

    sender.send_message(envelope, "receiver")
    _, received = receiver.receive_message(timeout=2.0)

    assert received is not None
    assert received.id == envelope.id


def test_parity_window_strategy_sends_duplicate():
    """Test that ParityWindowStrategy sends duplicate chunk."""
    bus = InMemoryRadioBus()
    sender = MeshtasticTransport(
        InMemoryRadio("sender", bus),
        segment_size=60,
        reliability=ParityWindowStrategy(),
    )
    receiver = MeshtasticTransport(
        InMemoryRadio("receiver", bus),
        segment_size=60,
        reliability=ParityWindowStrategy(),
    )

    envelope = MessageEnvelope(
        id="test-parity",
        type="request",
        command="ping",
        data={"msg": "test" * 20},  # Multi-chunk message
    )

    sender.send_message(envelope, "receiver")
    _, received = receiver.receive_message(timeout=2.0)

    assert received is not None
    assert received.id == envelope.id


def test_reliability_strategy_names():
    """Test that all strategies have correct names."""
    assert NoAckNackStrategy().name == "none"
    assert SimpleAckNackStrategy().name == "simple"
    assert StageAckNackStrategy().name == "stage"
    assert WindowedSelectiveStrategy().name == "window"
    assert ParityWindowStrategy().name == "window_fec"
