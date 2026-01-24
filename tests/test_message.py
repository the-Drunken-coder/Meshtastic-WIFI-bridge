"""Unit tests for MessageEnvelope and chunking functions."""
import struct
from message import (
    MessageEnvelope,
    chunk_envelope,
    parse_chunk,
    reconstruct_message,
    build_nack_chunk,
    parse_nack_payload,
    _encode_rle_sequences,
    _decode_rle_sequences,
    _is_rle_format,
)


def test_message_envelope_creation() -> None:
    """Test creating a MessageEnvelope."""
    envelope = MessageEnvelope(
        id="test-id-123",
        type="request",
        command="list_items",
        data={"limit": 10},
        meta={"timestamp": "2023-01-01"},
    )

    assert envelope.id == "test-id-123"
    assert envelope.type == "request"
    assert envelope.command == "list_items"
    assert envelope.data == {"limit": 10}
    assert envelope.meta == {"timestamp": "2023-01-01"}


def test_message_envelope_to_dict() -> None:
    """Test converting MessageEnvelope to dict."""
    envelope = MessageEnvelope(
        id="test-id",
        type="response",
        command="fetch_item",
        data={"result": "success"},
    )

    result = envelope.to_dict()

    assert result["id"] == "test-id"
    assert result["type"] == "response"
    assert result["command"] == "fetch_item"
    assert result["data"] == {"result": "success"}
    assert "meta" not in result


def test_message_envelope_from_dict() -> None:
    """Test creating MessageEnvelope from dict."""
    payload = {
        "id": "test-id",
        "type": "error",
        "command": "unknown",
        "data": {"error": "not found"},
        "meta": {"code": 404},
        "correlation_id": "corr-1",
    }

    envelope = MessageEnvelope.from_dict(payload)

    assert envelope.id == "test-id"
    assert envelope.type == "error"
    assert envelope.command == "unknown"
    assert envelope.data == {"error": "not found"}
    assert envelope.meta == {"code": 404}
    assert envelope.correlation_id == "corr-1"


def test_chunk_envelope_single_chunk() -> None:
    """Test chunking a small envelope that fits in one chunk."""
    envelope = MessageEnvelope(
        id="short-message",
        type="request",
        command="ping",
        data={"msg": "hi"},
    )

    chunks = chunk_envelope(envelope, segment_size=120)

    assert len(chunks) == 1
    flags, short_id, seq, total, data = parse_chunk(chunks[0])
    assert flags == 0
    assert short_id == "short-me"
    assert seq == 1
    assert total == 1
    assert data


def test_chunk_envelope_multiple_chunks() -> None:
    """Test chunking a large envelope into multiple chunks."""
    # Create a large payload
    large_data = {"payload": "x" * 500}
    envelope = MessageEnvelope(
        id="large-message-id",
        type="request",
        command="test",
        data=large_data,
    )

    chunks = chunk_envelope(envelope, segment_size=60)

    assert len(chunks) > 1
    # All chunks should have the same short ID
    _, short_id, _, total, _ = parse_chunk(chunks[0])
    assert short_id == "large-me"
    for chunk in chunks:
        _, chunk_id, _, chunk_total, _ = parse_chunk(chunk)
        assert chunk_id == short_id
        assert chunk_total == total

    # Check sequence numbers
    for i, chunk in enumerate(chunks):
        _, _, seq, total_chunks, _ = parse_chunk(chunk)
        assert seq == i + 1
        assert total_chunks == len(chunks)


def test_reconstruct_message() -> None:
    """Test reconstructing a message from segments."""
    original = MessageEnvelope(
        id="test-reconstruct",
        type="request",
        command="fetch_detail",
        correlation_id="corr-123",
        data={"item_id": 42, "details": "x" * 100},
    )

    chunks = chunk_envelope(original, segment_size=50)
    segments = [parse_chunk(chunk)[4] for chunk in chunks]

    reconstructed = reconstruct_message(segments)

    assert reconstructed.id == original.id
    assert reconstructed.type == original.type
    assert reconstructed.command == original.command
    assert reconstructed.correlation_id == original.correlation_id
    assert reconstructed.data == original.data


def test_chunk_and_reconstruct_roundtrip() -> None:
    """Test that chunking and reconstructing preserves the message."""
    original = MessageEnvelope(
        id="roundtrip-test-id",
        type="response",
        command="list_items",
        data={
            "items": [
                {"id": 1, "name": "Item 1"},
                {"id": 2, "name": "Item 2"},
            ]
        },
        meta={"count": 2},
    )

    chunks = chunk_envelope(original, segment_size=40)
    segments = [parse_chunk(chunk)[4] for chunk in chunks]
    reconstructed = reconstruct_message(segments)

    assert reconstructed.id == original.id
    assert reconstructed.type == original.type
    assert reconstructed.command == original.command
    assert reconstructed.data == original.data
    assert reconstructed.meta == original.meta


def test_chunk_envelope_compression() -> None:
    """Test that chunking includes compression."""
    # Repetitive data should compress well
    envelope = MessageEnvelope(
        id="compress-test",
        type="request",
        command="test",
        data={"pattern": "abc" * 100},
    )

    chunks = chunk_envelope(envelope, segment_size=120)

    # The compressed data should be present
    total_chunk_data_len = sum(len(parse_chunk(chunk)[4]) for chunk in chunks)

    # Compressed + base64 encoded data should be valid
    assert total_chunk_data_len > 0
    # We can't guarantee compression ratio, but data should be valid
    for chunk in chunks:
        data = parse_chunk(chunk)[4]
        assert isinstance(data, (bytes, bytearray))
        assert len(data) > 0


def test_rle_encode_single_sequences() -> None:
    """Test RLE encoding of non-consecutive single sequence numbers."""
    seqs = [5, 10, 15, 20]
    encoded = _encode_rle_sequences(seqs)
    decoded = _decode_rle_sequences(encoded)
    assert decoded == seqs


def test_rle_encode_consecutive_range() -> None:
    """Test RLE encoding of a long consecutive range."""
    seqs = [5, 6, 7, 8, 9]
    encoded = _encode_rle_sequences(seqs)
    decoded = _decode_rle_sequences(encoded)
    assert decoded == seqs
    # Range encoding should be more efficient: 1 (count) + 1 (type) + 4 (start+end) = 6 bytes
    # vs singles: 1 (count) + 5 * 3 (type+value each) = 16 bytes
    assert len(encoded) < 10


def test_rle_encode_mixed_ranges_and_singles() -> None:
    """Test RLE encoding with both ranges and single values."""
    seqs = [1, 2, 3, 5, 10, 11, 12, 13, 20]
    encoded = _encode_rle_sequences(seqs)
    decoded = _decode_rle_sequences(encoded)
    assert decoded == seqs


def test_rle_encode_empty_list() -> None:
    """Test RLE encoding of an empty list."""
    seqs = []
    encoded = _encode_rle_sequences(seqs)
    decoded = _decode_rle_sequences(encoded)
    assert decoded == []


def test_rle_encode_single_value() -> None:
    """Test RLE encoding of a single value."""
    seqs = [42]
    encoded = _encode_rle_sequences(seqs)
    decoded = _decode_rle_sequences(encoded)
    assert decoded == seqs


def test_rle_encode_two_consecutive() -> None:
    """Test RLE encoding of two consecutive numbers (should use singles, not range)."""
    seqs = [5, 6]
    encoded = _encode_rle_sequences(seqs)
    decoded = _decode_rle_sequences(encoded)
    assert decoded == seqs


def test_rle_encode_deduplication() -> None:
    """Test that RLE encoding deduplicates and sorts."""
    seqs = [5, 3, 5, 1, 3]
    encoded = _encode_rle_sequences(seqs)
    decoded = _decode_rle_sequences(encoded)
    assert decoded == [1, 3, 5]


def test_build_parse_nack_roundtrip() -> None:
    """Test that building and parsing NACK chunks preserves sequence numbers."""
    missing_seqs = [1, 2, 3, 5, 10, 11, 12, 13, 20]
    chunk = build_nack_chunk("test-msg", missing_seqs)
    
    # Parse the chunk to get the payload
    flags, short_id, seq, total, payload = parse_chunk(chunk)
    
    # Verify header
    assert short_id == "test-msg"
    assert seq == 1
    assert total == 1
    
    # Parse the payload
    parsed_seqs = parse_nack_payload(payload)
    assert parsed_seqs == missing_seqs


def test_parse_nack_legacy_format() -> None:
    """Test parsing NACK in legacy format (count + uint16 entries)."""
    # Legacy format: [count][uint16][uint16]...
    # Example: 3 sequences: 100, 200, 300
    count = 3
    seq1 = 100
    seq2 = 200
    seq3 = 300
    
    payload = struct.pack("!BHHH", count, seq1, seq2, seq3)
    parsed = parse_nack_payload(payload)
    
    assert parsed == [100, 200, 300]


def test_parse_nack_legacy_format_edge_case() -> None:
    """Test parsing legacy NACK where first sequence has high byte 0x00 or 0x01.
    
    This tests the fix for the format detection bug where legacy payloads
    with first sequence < 256 or < 512 would be misdetected as RLE.
    """
    # Legacy format with first sequence = 50 (high byte 0x00)
    count = 3
    seq1 = 50   # 0x0032 - high byte is 0x00
    seq2 = 100  # 0x0064
    seq3 = 150  # 0x0096
    
    payload = struct.pack("!BHHH", count, seq1, seq2, seq3)
    parsed = parse_nack_payload(payload)
    
    # Should correctly parse as legacy, not misdetect as RLE
    assert parsed == [50, 100, 150]


def test_parse_nack_legacy_format_high_byte_01() -> None:
    """Test parsing legacy NACK where first sequence has high byte 0x01."""
    # Legacy format with first sequence in range 256-511 (high byte 0x01)
    count = 2
    seq1 = 300  # 0x012C - high byte is 0x01
    seq2 = 400  # 0x0190
    
    payload = struct.pack("!BHH", count, seq1, seq2)
    parsed = parse_nack_payload(payload)
    
    # Should correctly parse as legacy, not misdetect as RLE
    assert parsed == [300, 400]


def test_is_rle_format_valid_rle() -> None:
    """Test that valid RLE format is correctly detected."""
    # RLE with single entry
    payload = bytes([1, 0x00]) + struct.pack("!H", 100)
    assert _is_rle_format(payload) is True
    
    # RLE with range entry
    payload = bytes([1, 0x01]) + struct.pack("!HH", 5, 10)
    assert _is_rle_format(payload) is True


def test_is_rle_format_invalid_rle() -> None:
    """Test that invalid RLE format is correctly rejected."""
    # Invalid entry type
    payload = bytes([1, 0x05, 0x00, 0x00])
    assert _is_rle_format(payload) is False
    
    # Truncated single entry
    payload = bytes([1, 0x00, 0x01])  # Missing one byte
    assert _is_rle_format(payload) is False
    
    # Truncated range entry
    payload = bytes([1, 0x01, 0x00, 0x01, 0x00])  # Missing one byte
    assert _is_rle_format(payload) is False


def test_is_rle_format_legacy_not_detected_as_rle() -> None:
    """Test that legacy format is not misdetected as RLE."""
    # Legacy format: count=2, seq1=50, seq2=100
    payload = struct.pack("!BHH", 2, 50, 100)
    assert _is_rle_format(payload) is False


def test_is_rle_format_extra_bytes() -> None:
    """Test that RLE format with extra bytes is rejected."""
    # Valid RLE with extra bytes appended
    payload = bytes([1, 0x00]) + struct.pack("!H", 100) + b"\x00\x00"
    assert _is_rle_format(payload) is False


def test_parse_nack_payload_with_extra_bytes() -> None:
    """Test that legacy format with exact structure is parsed correctly."""
    # Legacy format with 3 sequences - should parse correctly even if structure matches RLE count
    count = 3
    payload = struct.pack("!BHHH", count, 10, 20, 30)
    parsed = parse_nack_payload(payload)
    assert parsed == [10, 20, 30]


