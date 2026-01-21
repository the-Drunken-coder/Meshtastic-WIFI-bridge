"""Chunking helpers for Meshtastic payloads."""

from typing import Iterable

from framing.frame import MAX_PAYLOAD_SIZE


def clamp_chunk_size(chunk_size: int) -> int:
    """Clamp chunk size to a safe maximum for Meshtastic frames."""
    if chunk_size <= 0:
        return MAX_PAYLOAD_SIZE
    return min(chunk_size, MAX_PAYLOAD_SIZE)


def iter_chunks(data: bytes, chunk_size: int) -> Iterable[bytes]:
    """Yield data chunks of at most chunk_size bytes."""
    size = clamp_chunk_size(chunk_size)
    for i in range(0, len(data), size):
        yield data[i:i + size]
