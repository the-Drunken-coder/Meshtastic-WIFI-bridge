"""Frame data structure for LoRa WiFi Forwarder.

On-Wire Frame Format (byte layout):
====================================
| Offset | Size    | Field         | Description                          |
|--------|---------|---------------|--------------------------------------|
| 0      | 4 bytes | stream_id     | Unique stream identifier (uint32 LE) |
| 4      | 4 bytes | seq           | Sequence number (uint32 LE)          |
| 8      | 4 bytes | ack           | Acknowledgment number (uint32 LE)    |
| 12     | 1 byte  | flags         | Frame flags (bitfield)               |
| 13     | 2 bytes | payload_len   | Payload length (uint16 LE)           |
| 15     | N bytes | payload       | Actual payload data                  |
| 15+N   | 4 bytes | crc32         | CRC32 of entire frame (uint32 LE)    |
====================================

Frame Flags (bitfield):
- Bit 0 (0x01): SYN   - Stream synchronization (open stream)
- Bit 1 (0x02): FIN   - Stream finish (close stream)
- Bit 2 (0x04): RST   - Stream reset (abort stream)
- Bit 3 (0x08): ACK   - Acknowledgment
- Bit 4 (0x10): NACK  - Negative acknowledgment (request retransmit)

Header size: 15 bytes
CRC size: 4 bytes
Total overhead: 19 bytes
"""

from dataclasses import dataclass
from enum import IntFlag


class FrameFlags(IntFlag):
    """Frame control flags."""
    
    NONE = 0x00
    SYN = 0x01   # Stream synchronization (open stream)
    FIN = 0x02   # Stream finish (close stream)
    RST = 0x04   # Stream reset (abort stream)
    ACK = 0x08   # Acknowledgment
    NACK = 0x10  # Negative acknowledgment (request retransmit)


# Header size without payload and CRC
FRAME_HEADER_SIZE = 15

# CRC32 size
FRAME_CRC_SIZE = 4

# Total frame overhead (header + CRC)
FRAME_OVERHEAD = FRAME_HEADER_SIZE + FRAME_CRC_SIZE

# Maximum payload size (conservative for LoRa/Meshtastic)
MAX_PAYLOAD_SIZE = 180


@dataclass
class Frame:
    """
    Represents a single frame in the LoRa transport protocol.
    
    Attributes:
        stream_id: Unique identifier for the stream
        seq: Sequence number for this frame
        ack: Acknowledgment number (last received seq + 1)
        flags: Control flags (SYN, FIN, RST, ACK, NACK)
        payload: Actual data payload (may be empty)
    """
    
    stream_id: int
    seq: int
    ack: int
    flags: FrameFlags
    payload: bytes = b""
    
    def __post_init__(self):
        """Validate frame parameters."""
        if self.stream_id < 0 or self.stream_id > 0xFFFFFFFF:
            raise ValueError(f"stream_id must be 0-{0xFFFFFFFF}, got {self.stream_id}")
        if self.seq < 0 or self.seq > 0xFFFFFFFF:
            raise ValueError(f"seq must be 0-{0xFFFFFFFF}, got {self.seq}")
        if self.ack < 0 or self.ack > 0xFFFFFFFF:
            raise ValueError(f"ack must be 0-{0xFFFFFFFF}, got {self.ack}")
        if len(self.payload) > MAX_PAYLOAD_SIZE:
            raise ValueError(
                f"payload size {len(self.payload)} exceeds max {MAX_PAYLOAD_SIZE}"
            )
    
    def is_syn(self) -> bool:
        """Check if this is a SYN frame."""
        return bool(self.flags & FrameFlags.SYN)
    
    def is_fin(self) -> bool:
        """Check if this is a FIN frame."""
        return bool(self.flags & FrameFlags.FIN)
    
    def is_rst(self) -> bool:
        """Check if this is a RST frame."""
        return bool(self.flags & FrameFlags.RST)
    
    def is_ack(self) -> bool:
        """Check if this frame carries an ACK."""
        return bool(self.flags & FrameFlags.ACK)
    
    def is_nack(self) -> bool:
        """Check if this frame carries a NACK."""
        return bool(self.flags & FrameFlags.NACK)
    
    def __repr__(self) -> str:
        flags_str = "|".join(f.name for f in FrameFlags if f in self.flags and f.name)
        if not flags_str:
            flags_str = "NONE"
        return (
            f"Frame(stream={self.stream_id:#x}, seq={self.seq}, ack={self.ack}, "
            f"flags={flags_str}, payload_len={len(self.payload)})"
        )
