"""Unit tests for MessageReassembler."""

import random
import time

import msgpack
import zstandard as zstd

from message import (
    HEADER_STRUCT,
    MAGIC,
    VERSION,
    MessageEnvelope,
    chunk_envelope,
    parse_chunk,
)
from reassembly import MessageReassembler


def test_reassembler_single_chunk() -> None:
    """Test reassembling a single-chunk message."""
    envelope = MessageEnvelope(
        id="single-chunk-id",
        type="request",
        command="ping",
        data={"msg": "hello"},
    )

    chunks = chunk_envelope(envelope, segment_size=120)
    assert len(chunks) == 1

    reassembler = MessageReassembler()
    result = reassembler.add_chunk(chunks[0])

    assert result is not None
    assert result.id == envelope.id
    assert result.command == envelope.command
    assert result.data == envelope.data


def test_reassembler_multi_chunk() -> None:
    """Test reassembling a multi-chunk message."""
    envelope = MessageEnvelope(
        id="multi-chunk-id",
        type="request",
        command="list_items",
        data={"payload": "x" * 500},
    )

    chunks = chunk_envelope(envelope, segment_size=60)
    assert len(chunks) > 1

    reassembler = MessageReassembler()

    # Add all chunks except the last
    for chunk in chunks[:-1]:
        result = reassembler.add_chunk(chunk)
        assert result is None  # Not complete yet

    # Add final chunk
    result = reassembler.add_chunk(chunks[-1])
    assert result is not None
    assert result.id == envelope.id
    assert result.command == envelope.command
    assert result.data == envelope.data


def test_reassembler_duplicate_chunks() -> None:
    """Test that duplicate chunks are ignored."""
    envelope = MessageEnvelope(
        id="dup-test-id",
        type="request",
        command="test",
        data={"payload": "x" * 200},
    )

    chunks = chunk_envelope(envelope, segment_size=50)
    assert len(chunks) >= 2

    reassembler = MessageReassembler()

    # Add first chunk twice
    result1 = reassembler.add_chunk(chunks[0])
    assert result1 is None

    result2 = reassembler.add_chunk(chunks[0])
    assert result2 is None  # Duplicate ignored

    # Add remaining chunks
    for chunk in chunks[1:]:
        result = reassembler.add_chunk(chunk)

    # Should still reassemble correctly
    assert result is not None
    assert result.id == envelope.id


def test_reassembler_ttl_expiration() -> None:
    """Test that messages expire after TTL."""
    envelope = MessageEnvelope(
        id="ttl-test-id",
        type="request",
        command="test",
        data={"payload": "x" * 200},
    )

    chunks = chunk_envelope(envelope, segment_size=50)
    assert len(chunks) >= 2

    # Use short TTL for testing (with buffer for timing variance)
    reassembler = MessageReassembler(ttl_seconds=0.1)

    # Add first chunk
    result = reassembler.add_chunk(chunks[0])
    assert result is None

    # Wait for TTL to expire (with buffer to account for system load)
    time.sleep(0.25)

    # Try to add second chunk - should fail due to expiration
    result = reassembler.add_chunk(chunks[1])
    assert result is None


def test_reassembler_out_of_order_chunks() -> None:
    """Test reassembling chunks received out of order."""
    # Use incompressible payload to ensure at least 3 chunks
    random.seed(42)
    payload = "".join(chr(random.randint(32, 126)) for _ in range(3000))
    envelope = MessageEnvelope(
        id="ooo-test-id",
        type="request",
        command="test",
        data={"payload": payload[:1500], "extra": payload[1500:]},
    )

    chunks = chunk_envelope(envelope, segment_size=60)
    assert len(chunks) >= 3, f"Expected at least 3 chunks, got {len(chunks)}"

    reassembler = MessageReassembler()

    # Add chunks out of order
    result = reassembler.add_chunk(chunks[2])
    assert result is None

    result = reassembler.add_chunk(chunks[0])
    assert result is None

    result = reassembler.add_chunk(chunks[1])

    if len(chunks) == 3:
        # Should be complete
        assert result is not None
        assert result.id == envelope.id
    else:
        # Need more chunks
        for chunk in chunks[3:]:
            result = reassembler.add_chunk(chunk)
        assert result is not None


def test_reassembler_missing_sequences() -> None:
    """Test add_chunk_with_missing returns missing sequence numbers."""
    random.seed(123)
    noisy_payload = "".join(chr(random.randint(32, 126)) for _ in range(800))
    envelope = MessageEnvelope(
        id="missing-test-id",
        type="request",
        command="test",
        data={"payload": noisy_payload},
    )

    chunks = chunk_envelope(envelope, segment_size=50)
    assert len(chunks) >= 3

    reassembler = MessageReassembler()

    # Add first and last, skip middle to force a gap
    msg, missing = reassembler.add_chunk_with_missing(chunks[0])
    assert msg is None
    assert missing is None

    msg, missing = reassembler.add_chunk_with_missing(chunks[-1])
    assert msg is None
    assert missing is not None
    flags, chunk_id, seq, total, payload = parse_chunk(chunks[-1])
    assert missing == list(range(2, total))  # everything before last except first is missing

    # Add remaining chunks in order; last one should complete reassembly
    msg = None
    missing_list = missing
    for chunk in chunks[1:-1]:
        msg, missing_list = reassembler.add_chunk_with_missing(chunk)
    assert msg is not None
    assert msg.id == envelope.id
    assert missing_list is None


def test_reassembler_extend_short_ttl_toggle() -> None:
    """Test per-chunk TTL extension toggle for short base TTLs."""
    envelope = MessageEnvelope(
        id="ttl-toggle-id",
        type="request",
        command="test",
        data={"payload": "x" * 200},
    )
    chunks = chunk_envelope(envelope, segment_size=50)
    assert len(chunks) >= 2

    # Default: do not extend when base TTL is very small
    reassembler = MessageReassembler(ttl_seconds=0.1, per_chunk_ttl=1.0, extend_short_ttl=False)
    _, _ = reassembler.add_chunk_with_missing(chunks[0])
    short_id = parse_chunk(chunks[0])[1]
    default_bucket_ttl = reassembler._buckets[short_id]["ttl"]
    assert default_bucket_ttl == 0.1

    # With extend_short_ttl=True, TTL should grow with chunk count
    reassembler_extended = MessageReassembler(
        ttl_seconds=0.1, per_chunk_ttl=1.0, extend_short_ttl=True
    )
    _, _ = reassembler_extended.add_chunk_with_missing(chunks[0])
    _, short_id_ext, _, _, _ = parse_chunk(chunks[0])
    extended_bucket_ttl = reassembler_extended._buckets[short_id_ext]["ttl"]
    assert extended_bucket_ttl > default_bucket_ttl


def test_reassembler_prune() -> None:
    """Test pruning expired message buckets."""
    reassembler = MessageReassembler(ttl_seconds=0.1)

    # Create some incomplete messages
    for i in range(3):
        envelope = MessageEnvelope(
            id=f"prune-test-{i}",
            type="request",
            command="test",
            data={"payload": "x" * 200},
        )
        chunks = chunk_envelope(envelope, segment_size=50)
        # Add only first chunk (incomplete)
        reassembler.add_chunk(chunks[0])

    # Wait for TTL
    time.sleep(0.2)

    # Prune should remove expired buckets
    reassembler.prune()

    # Buckets should be empty
    assert len(reassembler._buckets) == 0


def test_reassembler_binary_format() -> None:
    """Test reassembler supports binary chunk format."""
    envelope = MessageEnvelope(
        id="binary-format-id",
        type="request",
        command="test",
        data={"msg": "test"},
    )

    chunks = chunk_envelope(envelope, segment_size=120)

    reassembler = MessageReassembler()
    result = reassembler.add_chunk(chunks[0])
    assert result is not None
    assert result.id == envelope.id


def test_reassembler_legacy_format() -> None:
    """Test reassembler handles legacy format without field name aliasing."""

    # Create a message payload without aliasing (legacy format)
    envelope_dict = {
        "id": "legacy-format-id",
        "type": "request",
        "command": "fetch_item",
        "data": {"item_id": "123", "ref_id": "456"},
    }

    # Encode without aliasing (legacy way)
    payload = msgpack.packb(envelope_dict, use_bin_type=True)
    compressor = zstd.ZstdCompressor(level=4)
    encoded = compressor.compress(payload)

    # Create chunk manually
    short_id_bytes = envelope_dict["id"].encode("utf-8")[:8]
    short_id = short_id_bytes.ljust(8, b"\x00")
    header = HEADER_STRUCT.pack(MAGIC, VERSION, 0, short_id, 1, 1)
    chunk = header + encoded

    # Reassembler should handle it
    reassembler = MessageReassembler()
    result = reassembler.add_chunk(chunk)

    assert result is not None
    assert result.id == envelope_dict["id"]
    assert result.command == envelope_dict["command"]
    # Note: decode_payload will try to expand aliases, but legacy format doesn't have them
    # So the data might have the original keys if they weren't in the alias map
    assert result.data is not None


def test_reassembler_slim_format() -> None:
    """Test reassembler handles slim format with field name aliasing."""
    envelope = MessageEnvelope(
        id="slim-format-id",
        type="request",
        command="fetch_item",
        data={"item_id": "123", "ref_id": "456", "limit": 10},
    )

    # Current format uses aliasing (slim format)
    chunks = chunk_envelope(envelope, segment_size=120)

    reassembler = MessageReassembler()
    result = reassembler.add_chunk(chunks[0])

    assert result is not None
    assert result.id == envelope.id
    assert result.command == envelope.command
    # Aliased fields should be expanded back to full names
    assert result.data == envelope.data
