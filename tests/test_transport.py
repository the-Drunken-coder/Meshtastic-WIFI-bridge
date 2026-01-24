"""Unit tests for MeshtasticTransport."""

from dedupe import RequestDeduper
from message import MessageEnvelope
from transport import (
    InMemoryRadio,
    InMemoryRadioBus,
    MeshtasticTransport,
)


def test_in_memory_radio_bus() -> None:
    """Test the InMemoryRadioBus message queue."""
    bus = InMemoryRadioBus()

    # Send message
    bus.send("node1", "node2", b"hello")

    # Receive on correct node
    sender, message = bus.receive("node2")
    assert sender == "node1"
    assert message == b"hello"

    # Nothing left
    result = bus.receive("node2")
    assert result is None


def test_in_memory_radio() -> None:
    """Test InMemoryRadio communication."""
    bus = InMemoryRadioBus()
    radio1 = InMemoryRadio("node1", bus)
    radio2 = InMemoryRadio("node2", bus)

    # Send from radio1 to radio2
    radio1.send("node2", b"test message")

    # Receive on radio2
    sender, message = radio2.receive(timeout=0.1)
    assert sender == "node1"
    assert message == b"test message"

    # Close (no-op for in-memory)
    radio1.close()
    radio2.close()


def test_transport_initialization() -> None:
    """Test MeshtasticTransport initialization."""
    radio = InMemoryRadio("test-node")
    transport = MeshtasticTransport(
        radio=radio,
        segment_size=100,
        chunk_ttl=60.0,
    )

    assert transport.radio == radio
    assert transport.segment_size == 100
    assert transport.reassembler is not None
    assert transport.deduper is not None


def test_transport_initialization_with_custom_deduper() -> None:
    """Test MeshtasticTransport with custom deduper."""
    radio = InMemoryRadio("test-node")
    deduper = RequestDeduper(max_entries=512)
    transport = MeshtasticTransport(radio=radio, deduper=deduper)

    assert transport.deduper == deduper


def test_transport_send_message() -> None:
    """Test sending a message through transport."""
    bus = InMemoryRadioBus()
    sender_radio = InMemoryRadio("sender", bus)
    transport = MeshtasticTransport(sender_radio, segment_size=60)

    envelope = MessageEnvelope(
        id="test-send-id",
        type="request",
        command="ping",
        data={"msg": "hello"},
    )

    # Send message
    transport.send_message(envelope, "receiver")

    # Check message was sent (at least one chunk)
    result = bus.receive("receiver")
    assert result is not None


def test_transport_send_large_message() -> None:
    """Test sending a large message that requires multiple chunks."""
    bus = InMemoryRadioBus()
    sender_radio = InMemoryRadio("sender", bus)
    transport = MeshtasticTransport(sender_radio, segment_size=40)

    envelope = MessageEnvelope(
        id="large-msg-id",
        type="request",
        command="bulk_payload",
        data={"payload": "x" * 500},
    )

    # Send message
    transport.send_message(envelope, "receiver")

    # Should have multiple chunks
    chunks_received = 0
    while True:
        result = bus.receive("receiver")
        if result is None:
            break
        chunks_received += 1

    assert chunks_received > 1


def test_transport_receive_message() -> None:
    """Test receiving and reassembling a message."""
    bus = InMemoryRadioBus()
    sender_transport = MeshtasticTransport(InMemoryRadio("sender", bus), segment_size=60)
    receiver_transport = MeshtasticTransport(InMemoryRadio("receiver", bus), segment_size=60)

    envelope = MessageEnvelope(
        id="recv-test-id",
        type="request",
        command="fetch",
        data={"id": "123"},
    )

    # Send message
    sender_transport.send_message(envelope, "receiver")

    # Receive message
    sender, received_envelope = receiver_transport.receive_message(timeout=1.0)

    assert sender == "sender"
    assert received_envelope is not None
    assert received_envelope.id == envelope.id
    assert received_envelope.command == envelope.command
    assert received_envelope.data == envelope.data


def test_transport_receive_timeout() -> None:
    """Test that receive_message returns None on timeout."""
    radio = InMemoryRadio("node")
    transport = MeshtasticTransport(radio, segment_size=60)

    # No messages available
    sender, envelope = transport.receive_message(timeout=0.1)

    assert sender is None
    assert envelope is None


def test_transport_should_process_deduplication() -> None:
    """Test should_process method for request deduplication."""
    radio = InMemoryRadio("node")
    transport = MeshtasticTransport(radio, segment_size=60)

    envelope = MessageEnvelope(
        id="dedup-test",
        type="request",
        command="test_cmd",
        data={},
    )

    sender = "!sender123"

    # First time should process
    assert transport.should_process(sender, envelope) is True

    # Second time should not process (duplicate)
    assert transport.should_process(sender, envelope) is False


def test_transport_should_process_different_senders() -> None:
    """Test that different senders can send the same request."""
    radio = InMemoryRadio("node")
    transport = MeshtasticTransport(radio, segment_size=60)

    envelope = MessageEnvelope(
        id="same-id",
        type="request",
        command="test_cmd",
        data={},
    )

    # Different senders
    assert transport.should_process("!sender1", envelope) is True
    assert transport.should_process("!sender2", envelope) is True


def test_transport_correlation_id_deduplication() -> None:
    """Messages sharing a correlation id should deduplicate even with new IDs."""
    radio = InMemoryRadio("node")
    transport = MeshtasticTransport(radio, segment_size=60)

    envelope1 = MessageEnvelope(
        id="msg-1",
        type="request",
        command="test_cmd",
        correlation_id="corr-abc",
        data={},
    )
    envelope2 = MessageEnvelope(
        id="msg-2",
        type="request",
        command="test_cmd",
        correlation_id="corr-abc",
        data={},
    )

    assert transport.should_process("!sender", envelope1) is True
    assert transport.should_process("!sender", envelope2) is False

    # Original message key should also be remembered
    assert transport.should_process("!sender", envelope1) is False


def test_transport_semantic_deduplication_with_meta_key() -> None:
    """Messages with the same semantic key should be deduped."""
    radio = InMemoryRadio("node")
    transport = MeshtasticTransport(radio, segment_size=60)

    envelope1 = MessageEnvelope(
        id="semantic-1",
        type="request",
        command="process",
        data={},
        meta={"dedupe_key": "job-1"},
    )
    envelope2 = MessageEnvelope(
        id="semantic-2",
        type="request",
        command="process",
        data={},
        meta={"dedupe_key": "job-1"},
    )

    assert transport.should_process("!sender", envelope1) is True
    assert transport.should_process("!sender", envelope2) is False


def test_transport_roundtrip() -> None:
    """Test full send and receive roundtrip with multi-chunk message."""
    bus = InMemoryRadioBus()
    sender_transport = MeshtasticTransport(InMemoryRadio("sender", bus), segment_size=50)
    receiver_transport = MeshtasticTransport(InMemoryRadio("receiver", bus), segment_size=50)

    # Large message requiring multiple chunks
    envelope = MessageEnvelope(
        id="roundtrip-test",
        type="response",
        command="bulk_response",
        data={
            "items": [{"id": i, "name": f"Item {i}", "description": "x" * 50} for i in range(10)]
        },
    )

    # Send
    sender_transport.send_message(envelope, "receiver")

    # Receive
    sender, received = receiver_transport.receive_message(timeout=2.0)

    assert sender == "sender"
    assert received is not None
    assert received.id == envelope.id
    assert received.command == envelope.command
    assert received.data == envelope.data


def test_transport_burst_mode_configuration() -> None:
    """Test that burst mode parameters are correctly set."""
    radio = InMemoryRadio("test-node")
    transport = MeshtasticTransport(
        radio=radio,
        segment_size=100,
        burst_size=10,
        burst_delay=0.1,
    )

    assert transport._burst_size == 10
    assert transport._burst_delay == 0.1


def test_transport_burst_mode_default_values() -> None:
    """Test that burst mode has sensible defaults."""
    radio = InMemoryRadio("test-node")
    transport = MeshtasticTransport(radio=radio)

    # Check default values are set
    assert transport._burst_size == 5  # Default burst size
    assert transport._burst_delay == 0.05  # Default burst delay


def test_transport_burst_mode_sends_multiple_chunks() -> None:
    """Test that burst mode sends multiple chunks per send_message call."""
    bus = InMemoryRadioBus()
    sender_radio = InMemoryRadio("sender", bus)
    receiver_radio = InMemoryRadio("receiver", bus)
    
    # Configure transport with burst mode
    sender_transport = MeshtasticTransport(
        sender_radio, 
        segment_size=40,
        burst_size=3,  # Send 3 chunks at a time
        burst_delay=0.0,  # No delay for testing
    )
    receiver_transport = MeshtasticTransport(receiver_radio, segment_size=40)

    # Create message that requires multiple chunks
    envelope = MessageEnvelope(
        id="burst-test-id",
        type="request",
        command="bulk_payload",
        data={"payload": "x" * 500},
    )

    # Send message using burst mode
    sender_transport.send_message(envelope, "receiver")

    # Receive and reassemble the message
    sender, received = receiver_transport.receive_message(timeout=2.0)
    
    assert sender == "sender"
    assert received is not None
    assert received.id == envelope.id
    assert received.data == envelope.data


def test_transport_burst_mode_caches_chunks_for_nack() -> None:
    """Test that burst mode caches chunks for NACK-based recovery."""
    bus = InMemoryRadioBus()
    sender_radio = InMemoryRadio("sender", bus)
    
    sender_transport = MeshtasticTransport(
        sender_radio, 
        segment_size=40,
        burst_size=5,
    )

    envelope = MessageEnvelope(
        id="cache-test-id",
        type="request",
        command="test",
        data={"payload": "x" * 200},
    )

    # Send message
    sender_transport.send_message(envelope, "receiver")

    # Check that chunks are cached for NACK recovery
    message_prefix = envelope.id[:8]
    assert message_prefix in sender_transport._chunk_cache
    assert len(sender_transport._chunk_cache[message_prefix]) > 0

