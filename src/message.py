"""Binary message envelope and chunking for Meshtastic.

Optimizations:
- Binary chunk header (fixed 16 bytes)
- MessagePack payloads + Zstandard compression
- Short message ID prefixes in chunks
"""

from __future__ import annotations

import math
import re
import struct
from typing import Iterable, List, Tuple

import msgpack
import zstandard as zstd
from dataclasses import dataclass, field
from typing import Any, Dict

MAGIC = b"MB"
VERSION = 1
FLAG_ACK = 0x01
FLAG_NACK = 0x02
HEADER_STRUCT = struct.Struct("!2sBB8sHH")
HEADER_SIZE = HEADER_STRUCT.size

# Optimized segment payload size - balance between fewer chunks and staying under payload cap.
# LoRa frames max out at 256 bytes; Meshtastic adds a 16-byte LoRa header outside the
# payload. Our 16-byte chunk header is inside the payload, so keep chunks well under
# the ~240-byte usable payload limit for safety.
SEGMENT_SIZE = 100

# Adaptive compression: use different levels based on payload size
# Small payloads: fast compression (overhead may exceed savings)
# Medium payloads: balanced compression
# Large payloads: better compression (worth the CPU cost)
_COMPRESSOR_FAST = zstd.ZstdCompressor(level=1)  # For payloads < 200 bytes
_COMPRESSOR_DEFAULT = zstd.ZstdCompressor(level=3)  # For payloads 200-1000 bytes
_COMPRESSOR_THOROUGH = zstd.ZstdCompressor(level=5)  # For payloads > 1000 bytes
_DECOMPRESSOR = zstd.ZstdDecompressor()

# Thresholds for adaptive compression
_COMPRESSION_THRESHOLD_FAST = 200  # bytes
_COMPRESSION_THRESHOLD_THOROUGH = 1000  # bytes
ALIAS_MAP: Dict[str, str] = {}
REVERSE_ALIAS_MAP: Dict[str, str] = {v: k for k, v in ALIAS_MAP.items()}

# Envelope-specific aliases (applied non-recursively to the top-level container)
ENVELOPE_ALIAS_MAP: Dict[str, str] = {
    "command": "cmd",
    "data": "d",
    "id": "i",
    "type": "t",
    "correlation_id": "cid",
    "priority": "p",
    "meta": "m",
}
REVERSE_ENVELOPE_MAP: Dict[str, str] = {v: k for k, v in ENVELOPE_ALIAS_MAP.items()}

_TS_RE = re.compile(r"^(?P<prefix>.+T\d{2}:\d{2}:\d{2})(?:\.\d+)?(?P<suffix>Z|[+-]\d{2}:\d{2})?$")


@dataclass
class MessageEnvelope:
    id: str
    type: str
    command: str
    priority: int = 10  # Lower is higher priority (0=Critical, 10=Normal)
    correlation_id: str | None = None
    data: Dict[str, Any] | None = None
    meta: Dict[str, Any] = field(default_factory=dict)

    DEFAULT_PRIORITY = 10  # Class constant for default priority

    def to_dict(self) -> Dict[str, Any]:
        envelope: Dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "command": self.command,
        }
        # Only include priority if non-default (saves 2-3 bytes)
        if self.priority != self.DEFAULT_PRIORITY:
            envelope["priority"] = self.priority
        # Only include data if non-empty (saves 2-3 bytes)
        if self.data:
            envelope["data"] = self.data
        if self.meta:
            envelope["meta"] = self.meta
        if self.correlation_id is not None:
            envelope["correlation_id"] = self.correlation_id
        return envelope

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "MessageEnvelope":
        return cls(
            id=payload["id"],
            type=payload["type"],
            command=payload["command"],
            priority=payload.get("priority", 10),
            correlation_id=payload.get("correlation_id"),
            data=payload.get("data") or {},
            meta=payload.get("meta") or {},
        )


def _normalize_value(key: str, value: Any) -> Any:
    """Normalize values to reduce payload size."""
    if isinstance(value, str) and key in {"created_at", "updated_at", "ca", "ua"}:
        match = _TS_RE.match(value)
        if match:
            return f"{match.group('prefix')}{match.group('suffix') or ''}"
    return value


def _alias_payload(value: Any, encode: bool = True) -> Any:
    if isinstance(value, dict):
        mapped: Dict[str, Any] = {}
        for key, val in value.items():
            new_key = ALIAS_MAP.get(key, key) if encode else REVERSE_ALIAS_MAP.get(key, key)
            normalized_val = _normalize_value(key, val) if encode else val
            mapped[new_key] = _alias_payload(normalized_val, encode=encode)
        return mapped
    if isinstance(value, list):
        return [_alias_payload(item, encode=encode) for item in value]
    return value


def shorten_payload(payload: Any) -> Any:
    """Public helper to apply aliasing/normalization to an arbitrary payload."""
    return _alias_payload(payload, encode=True)


def expand_payload(payload: Any) -> Any:
    """Reverse aliasing/normalization on a payload."""
    return _alias_payload(payload, encode=False)


def _select_compressor(payload_size: int) -> zstd.ZstdCompressor:
    """Select compression level based on payload size for optimal speed/ratio tradeoff."""
    if payload_size < _COMPRESSION_THRESHOLD_FAST:
        return _COMPRESSOR_FAST
    elif payload_size > _COMPRESSION_THRESHOLD_THOROUGH:
        return _COMPRESSOR_THOROUGH
    return _COMPRESSOR_DEFAULT


def _encode_payload(envelope: MessageEnvelope) -> bytes:
    """Encode envelope as compressed binary payload with scoped aliasing."""
    # 1. Start with raw dict
    raw = envelope.to_dict()
    
    # 2. Recursively alias the inner 'data' payload
    if "data" in raw:
        raw["data"] = _alias_payload(raw["data"], encode=True)
        
    # 3. Alias the top-level envelope keys
    aliased = {}
    for k, v in raw.items():
        aliased[ENVELOPE_ALIAS_MAP.get(k, k)] = v
        
    payload = msgpack.packb(aliased, use_bin_type=True)
    # Use adaptive compression based on payload size
    compressor = _select_compressor(len(payload))
    return compressor.compress(payload)


def _decode_payload(encoded: bytes) -> Dict[str, Any]:
    """Decode compressed binary payload back to dict with scoped aliasing."""
    decompressed = _DECOMPRESSOR.decompress(encoded)
    unpacked = msgpack.unpackb(decompressed, raw=False)
    
    # 1. Un-alias top-level envelope keys
    envelope_dict = {}
    for k, v in unpacked.items():
        envelope_dict[REVERSE_ENVELOPE_MAP.get(k, k)] = v
        
    # 2. Recursively un-alias inner 'data' payload
    if "data" in envelope_dict:
        envelope_dict["data"] = _alias_payload(envelope_dict["data"], encode=False)
        
    return envelope_dict


def estimate_chunk_count(envelope: MessageEnvelope, segment_size: int = SEGMENT_SIZE) -> int:
    """Estimate the number of chunks without building them (faster for progress display)."""
    encoded = _encode_payload(envelope)
    if not encoded:
        return 0
    return math.ceil(len(encoded) / segment_size)


def chunk_envelope(
    envelope: MessageEnvelope, segment_size: int = SEGMENT_SIZE
) -> List[bytes]:
    """Split envelope into binary chunks for transmission."""
    encoded = _encode_payload(envelope)
    if not encoded:
        return []

    count = math.ceil(len(encoded) / segment_size)
    # Encode message ID as UTF-8 and truncate to 8 bytes
    short_id_bytes = envelope.id.encode("utf-8")[:8]
    short_id = short_id_bytes.ljust(8, b"\x00")

    chunks: List[bytes] = []
    for index in range(count):
        segment = encoded[index * segment_size : (index + 1) * segment_size]
        header = HEADER_STRUCT.pack(MAGIC, VERSION, 0, short_id, index + 1, count)
        chunks.append(header + segment)
    return chunks


def build_ack_chunk(ack_id: str, include_payload: bool = True) -> bytes:
    """Build an ACK chunk.
    
    Args:
        ack_id: The message ID or control message (e.g., "bitmap_req|msg_id")
        include_payload: If False, omit payload for simple ACKs (saves ~12 bytes).
                        Control messages (containing "|") always include payload.
    """
    # Control messages (with |) must include payload for parsing
    is_control = "|" in ack_id
    # Encode and truncate ID suffix for header (use last segment after |, e.g. message/chunk ID)
    id_for_header = ack_id.split("|")[-1] if is_control else ack_id
    short_id_bytes = id_for_header.encode("utf-8")[:8]
    short_id = short_id_bytes.ljust(8, b"\x00")
    header = HEADER_STRUCT.pack(MAGIC, VERSION, FLAG_ACK, short_id, 1, 1)
    
    # Include payload for control messages or when explicitly requested
    if is_control or include_payload:
        payload = ack_id.encode("utf-8")
        return header + payload
    # Simple ACK: empty payload (header already has 8-byte ID prefix)
    return header


def _encode_rle_sequences(seqs: List[int]) -> bytes:
    """Encode sequences using run-length encoding for consecutive runs.
    
    Format: [count][entries...]
    Each entry is either:
    - Single: 0x00 + uint16 (3 bytes) - single sequence number
    - Range:  0x01 + uint16 start + uint16 end (5 bytes) - inclusive range
    
    For runs of 3+ consecutive numbers, range encoding saves bytes.
    Example: [5,6,7,8,9] as range = 5 bytes vs individual = 15 bytes
    """
    if not seqs:
        return bytes([0])
    
    seqs = sorted(set(seqs))  # Dedupe and sort
    entries: List[bytes] = []
    i = 0
    
    while i < len(seqs):
        # Find consecutive run starting at i
        run_start = seqs[i]
        run_end = run_start
        j = i + 1
        while j < len(seqs) and seqs[j] == run_end + 1:
            run_end = seqs[j]
            j += 1
        
        run_length = run_end - run_start + 1
        
        if run_length >= 3:
            # Range encoding: 5 bytes for any run of 3+
            entries.append(b'\x01' + struct.pack("!HH", run_start, run_end))
            i = j
        else:
            # Single encoding: 3 bytes each
            entries.append(b'\x00' + struct.pack("!H", run_start))
            i += 1
    
    # Limit total entries to fit in payload
    entry_count = min(len(entries), 127)  # 7-bit count
    return bytes([entry_count]) + b"".join(entries[:entry_count])


def _decode_rle_sequences(payload: bytes) -> List[int]:
    """Decode RLE-encoded sequences back to a list."""
    if not payload or payload[0] == 0:
        return []
    
    count = payload[0] & 0x7F
    seqs: List[int] = []
    offset = 1
    
    for _ in range(count):
        if offset >= len(payload):
            break
        
        entry_type = payload[offset]
        offset += 1
        
        if entry_type == 0x00:
            # Single sequence
            if offset + 2 > len(payload):
                break
            seq = struct.unpack("!H", payload[offset:offset+2])[0]
            seqs.append(seq)
            offset += 2
        elif entry_type == 0x01:
            # Range
            if offset + 4 > len(payload):
                break
            start, end = struct.unpack("!HH", payload[offset:offset+4])
            seqs.extend(range(start, end + 1))
            offset += 4
        else:
            # Unknown entry type, skip
            break
    
    return seqs


def build_nack_chunk(message_prefix: str, missing_seqs: List[int]) -> bytes:
    """Build a compact NACK chunk listing missing sequence numbers.
    
    Uses run-length encoding for consecutive sequences to reduce payload size.
    Example: [5,6,7,8,9,15,16] encodes as 1 range + 2 singles = 11 bytes
             vs 7 singles = 21 bytes (traditional encoding)
    """
    short_id_bytes = message_prefix.encode("utf-8")[:8]
    short_id = short_id_bytes.ljust(8, b"\x00")
    
    # Clamp sequences to valid range
    seqs = [min(max(1, int(seq)), 65535) for seq in missing_seqs]
    
    # Use RLE encoding for better efficiency with consecutive sequences
    payload = _encode_rle_sequences(seqs)
    header = HEADER_STRUCT.pack(MAGIC, VERSION, FLAG_NACK, short_id, 1, 1)
    return header + payload


def _is_rle_format(payload: bytes) -> bool:
    """Check if payload is in RLE format by validating its structure.
    
    RLE format: [count][entries...] where each entry is:
    - 0x00 + uint16 (single) or 0x01 + uint16 + uint16 (range)
    
    Returns True if the payload structure is consistent with RLE format.
    """
    if len(payload) < 2:
        return False
    
    count = payload[0] & 0x7F
    if count == 0:
        return True  # Empty RLE is valid
    
    offset = 1
    entries_found = 0
    
    # Try to parse as RLE and see if it's structurally consistent
    while offset < len(payload) and entries_found < count:
        if offset >= len(payload):
            return False  # Truncated
        
        entry_type = payload[offset]
        
        if entry_type == 0x00:
            # Single: needs 2 more bytes
            if offset + 3 > len(payload):
                return False
            offset += 3
            entries_found += 1
        elif entry_type == 0x01:
            # Range: needs 4 more bytes
            if offset + 5 > len(payload):
                return False
            offset += 5
            entries_found += 1
        else:
            # Invalid entry type for RLE
            return False
    
    # Valid RLE should consume all bytes and find exactly 'count' entries
    return entries_found == count


def parse_nack_payload(payload: bytes) -> List[int]:
    """Parse NACK payload, supporting both legacy and RLE formats."""
    if not payload:
        return []
    
    # Use structural validation to detect RLE format
    if _is_rle_format(payload):
        return _decode_rle_sequences(payload)
    
    # Legacy format: count + uint16 entries
    count = payload[0]
    seqs: List[int] = []
    for idx in range(count):
        start = 1 + idx * 2
        end = start + 2
        if end > len(payload):
            break
        seqs.append(struct.unpack("!H", payload[start:end])[0])
    return seqs


def parse_chunk(chunk: bytes) -> Tuple[int, str, int, int, bytes]:
    if len(chunk) < HEADER_SIZE:
        raise ValueError("Chunk too small to parse header")
    magic, version, flags, short_id, seq, total = HEADER_STRUCT.unpack(
        chunk[:HEADER_SIZE]
    )
    if magic != MAGIC or version != VERSION:
        raise ValueError("Unsupported chunk header")
    # Decode UTF-8 short ID, replacing invalid sequences with replacement character
    short_id_str = short_id.rstrip(b"\x00").decode("utf-8", errors="replace")
    return flags, short_id_str, seq, total, chunk[HEADER_SIZE:]


def reconstruct_message(segments: Iterable[bytes]) -> MessageEnvelope:
    """Reconstruct message from payload segments."""
    combined = b"".join(segments)
    payload = _decode_payload(combined)
    return MessageEnvelope.from_dict(payload)
