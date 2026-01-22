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
# With a 16-byte header and a 237-byte payload cap, a 100-byte payload segment produces
# 116-byte chunks, which stay comfortably under the cap.
SEGMENT_SIZE = 100
# Use mid-range Zstandard compression level to balance CPU cost and compression ratio
_COMPRESSOR = zstd.ZstdCompressor(level=4)
_DECOMPRESSOR = zstd.ZstdDecompressor()
ALIAS_MAP: Dict[str, str] = {}
REVERSE_ALIAS_MAP: Dict[str, str] = {v: k for k, v in ALIAS_MAP.items()}

# Envelope-specific aliases (applied non-recursively to the top-level container)
ENVELOPE_ALIAS_MAP: Dict[str, str] = {
    "command": "cmd",
    "data": "d",
    "id": "i",
    "type": "t",
    "correlation_id": "cid",
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

    def to_dict(self) -> Dict[str, Any]:
        envelope = {
            "id": self.id,
            "type": self.type,
            "command": self.command,
            "priority": self.priority,
            "data": self.data or {},
        }
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
    return _COMPRESSOR.compress(payload)


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


def build_ack_chunk(ack_id: str) -> bytes:
    # Encode ACK ID as UTF-8 for payload
    payload = ack_id.encode("utf-8")
    # Encode and truncate ID prefix for header
    short_id_bytes = ack_id.encode("utf-8")[:8]
    short_id = short_id_bytes.ljust(8, b"\x00")
    header = HEADER_STRUCT.pack(MAGIC, VERSION, FLAG_ACK, short_id, 1, 1)
    return header + payload


def build_nack_chunk(message_prefix: str, missing_seqs: List[int]) -> bytes:
    """Build a compact NACK chunk listing missing sequence numbers."""
    short_id_bytes = message_prefix.encode("utf-8")[:8]
    short_id = short_id_bytes.ljust(8, b"\x00")
    # Limit to 255 entries to keep payload small
    seqs = [min(max(1, int(seq)), 65535) for seq in missing_seqs][:255]
    payload = bytes([len(seqs)]) + b"".join(struct.pack("!H", seq) for seq in seqs)
    header = HEADER_STRUCT.pack(MAGIC, VERSION, FLAG_NACK, short_id, 1, 1)
    return header + payload


def parse_nack_payload(payload: bytes) -> List[int]:
    if not payload:
        return []
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
