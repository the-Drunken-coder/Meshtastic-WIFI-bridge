"""Encode and decode functions for frames.

This module handles serialization and deserialization of Frame objects
to and from bytes for transmission over LoRa.
"""

import struct
import zlib

from framing.frame import Frame, FrameFlags, FRAME_HEADER_SIZE, FRAME_CRC_SIZE


class FrameDecodeError(Exception):
    """Raised when frame decoding fails."""
    pass


def encode_frame(frame: Frame) -> bytes:
    """
    Encode a Frame object into bytes for transmission.
    
    Frame format:
    - stream_id: 4 bytes (uint32 LE)
    - seq: 4 bytes (uint32 LE)
    - ack: 4 bytes (uint32 LE)
    - flags: 1 byte
    - payload_len: 2 bytes (uint16 LE)
    - payload: N bytes
    - crc32: 4 bytes (uint32 LE)
    
    Args:
        frame: Frame object to encode
        
    Returns:
        Encoded bytes ready for transmission
    """
    # Pack header
    header = struct.pack(
        "<IIIBH",
        frame.stream_id,
        frame.seq,
        frame.ack,
        int(frame.flags),
        len(frame.payload),
    )
    
    # Combine header and payload
    data = header + frame.payload
    
    # Calculate CRC32 of the entire frame (header + payload)
    crc = zlib.crc32(data) & 0xFFFFFFFF
    
    # Append CRC
    return data + struct.pack("<I", crc)


def decode_frame(data: bytes) -> Frame:
    """
    Decode bytes into a Frame object.
    
    Args:
        data: Raw bytes received from transmission
        
    Returns:
        Decoded Frame object
        
    Raises:
        FrameDecodeError: If decoding fails (too short, CRC mismatch, etc.)
    """
    min_size = FRAME_HEADER_SIZE + FRAME_CRC_SIZE
    
    if len(data) < min_size:
        raise FrameDecodeError(
            f"Frame too short: {len(data)} bytes, minimum {min_size}"
        )
    
    # Extract CRC from the end
    received_crc = struct.unpack("<I", data[-FRAME_CRC_SIZE:])[0]
    
    # Calculate CRC of the frame without the CRC field
    frame_data = data[:-FRAME_CRC_SIZE]
    calculated_crc = zlib.crc32(frame_data) & 0xFFFFFFFF
    
    if received_crc != calculated_crc:
        raise FrameDecodeError(
            f"CRC mismatch: received {received_crc:#x}, calculated {calculated_crc:#x}"
        )
    
    # Unpack header
    stream_id, seq, ack, flags_byte, payload_len = struct.unpack(
        "<IIIBH", frame_data[:FRAME_HEADER_SIZE]
    )
    
    # Validate payload length
    expected_len = FRAME_HEADER_SIZE + payload_len
    if len(frame_data) != expected_len:
        raise FrameDecodeError(
            f"Frame length mismatch: got {len(frame_data)}, expected {expected_len}"
        )
    
    # Extract payload
    payload = frame_data[FRAME_HEADER_SIZE:]
    
    # Create Frame object
    flags = FrameFlags(flags_byte)
    
    return Frame(
        stream_id=stream_id,
        seq=seq,
        ack=ack,
        flags=flags,
        payload=payload,
    )
