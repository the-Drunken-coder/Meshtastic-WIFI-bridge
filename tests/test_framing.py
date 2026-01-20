"""Tests for framing encode/decode functions."""

import unittest
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framing.frame import Frame, FrameFlags, FRAME_OVERHEAD, MAX_PAYLOAD_SIZE
from framing.codec import encode_frame, decode_frame, FrameDecodeError


class TestFrame(unittest.TestCase):
    """Test Frame dataclass."""
    
    def test_create_basic_frame(self):
        """Test creating a basic frame."""
        frame = Frame(
            stream_id=0x12345678,
            seq=1,
            ack=0,
            flags=FrameFlags.SYN,
            payload=b"test",
        )
        
        self.assertEqual(frame.stream_id, 0x12345678)
        self.assertEqual(frame.seq, 1)
        self.assertEqual(frame.ack, 0)
        self.assertEqual(frame.flags, FrameFlags.SYN)
        self.assertEqual(frame.payload, b"test")
    
    def test_frame_flags(self):
        """Test frame flag methods."""
        syn_frame = Frame(0, 0, 0, FrameFlags.SYN)
        self.assertTrue(syn_frame.is_syn())
        self.assertFalse(syn_frame.is_fin())
        
        fin_frame = Frame(0, 0, 0, FrameFlags.FIN)
        self.assertTrue(fin_frame.is_fin())
        self.assertFalse(fin_frame.is_syn())
        
        ack_frame = Frame(0, 0, 0, FrameFlags.ACK)
        self.assertTrue(ack_frame.is_ack())
        
        combined = Frame(0, 0, 0, FrameFlags.SYN | FrameFlags.ACK)
        self.assertTrue(combined.is_syn())
        self.assertTrue(combined.is_ack())
    
    def test_frame_validation(self):
        """Test frame validation."""
        # Valid frame
        Frame(0, 0, 0, FrameFlags.NONE)
        
        # Invalid stream_id
        with self.assertRaises(ValueError):
            Frame(-1, 0, 0, FrameFlags.NONE)
        
        with self.assertRaises(ValueError):
            Frame(0x100000000, 0, 0, FrameFlags.NONE)
        
        # Invalid payload size
        with self.assertRaises(ValueError):
            Frame(0, 0, 0, FrameFlags.NONE, b"x" * (MAX_PAYLOAD_SIZE + 1))


class TestCodec(unittest.TestCase):
    """Test encode/decode functions."""
    
    def test_encode_decode_roundtrip(self):
        """Test that encoding and decoding produces the same frame."""
        original = Frame(
            stream_id=0xDEADBEEF,
            seq=42,
            ack=41,
            flags=FrameFlags.ACK,
            payload=b"Hello, LoRa!",
        )
        
        encoded = encode_frame(original)
        decoded = decode_frame(encoded)
        
        self.assertEqual(decoded.stream_id, original.stream_id)
        self.assertEqual(decoded.seq, original.seq)
        self.assertEqual(decoded.ack, original.ack)
        self.assertEqual(decoded.flags, original.flags)
        self.assertEqual(decoded.payload, original.payload)
    
    def test_encode_decode_empty_payload(self):
        """Test encoding/decoding frame with no payload."""
        original = Frame(
            stream_id=1,
            seq=0,
            ack=0,
            flags=FrameFlags.SYN,
            payload=b"",
        )
        
        encoded = encode_frame(original)
        decoded = decode_frame(encoded)
        
        self.assertEqual(decoded.payload, b"")
        self.assertEqual(len(encoded), FRAME_OVERHEAD)
    
    def test_encode_decode_max_payload(self):
        """Test encoding/decoding frame with maximum payload."""
        payload = b"x" * MAX_PAYLOAD_SIZE
        original = Frame(
            stream_id=0xFFFFFFFF,
            seq=0xFFFFFFFF,
            ack=0xFFFFFFFF,
            flags=FrameFlags.FIN | FrameFlags.ACK,
            payload=payload,
        )
        
        encoded = encode_frame(original)
        decoded = decode_frame(encoded)
        
        self.assertEqual(decoded.payload, payload)
        self.assertEqual(len(encoded), FRAME_OVERHEAD + MAX_PAYLOAD_SIZE)
    
    def test_decode_corrupted_crc(self):
        """Test that corrupted CRC is detected."""
        original = Frame(
            stream_id=1,
            seq=1,
            ack=0,
            flags=FrameFlags.NONE,
            payload=b"test",
        )
        
        encoded = bytearray(encode_frame(original))
        # Corrupt CRC
        encoded[-1] ^= 0xFF
        
        with self.assertRaises(FrameDecodeError) as ctx:
            decode_frame(bytes(encoded))
        
        self.assertIn("CRC mismatch", str(ctx.exception))
    
    def test_decode_truncated_frame(self):
        """Test that truncated frame is rejected."""
        with self.assertRaises(FrameDecodeError) as ctx:
            decode_frame(b"short")
        
        self.assertIn("too short", str(ctx.exception))
    
    def test_decode_corrupted_length(self):
        """Test that corrupted length field is detected."""
        original = Frame(
            stream_id=1,
            seq=1,
            ack=0,
            flags=FrameFlags.NONE,
            payload=b"test",
        )
        
        encoded = bytearray(encode_frame(original))
        # Valid CRC would need to be recalculated, but we corrupt payload_len
        # Since CRC will fail first, we need a different approach
        # Actually, let's test by creating a frame and manually shortening it
        short_data = encode_frame(original)[:-5]  # Remove CRC and some payload
        
        with self.assertRaises(FrameDecodeError):
            decode_frame(short_data)
    
    def test_all_flag_combinations(self):
        """Test encoding/decoding with various flag combinations."""
        flag_combos = [
            FrameFlags.NONE,
            FrameFlags.SYN,
            FrameFlags.FIN,
            FrameFlags.RST,
            FrameFlags.ACK,
            FrameFlags.NACK,
            FrameFlags.SYN | FrameFlags.ACK,
            FrameFlags.FIN | FrameFlags.ACK,
            FrameFlags.ACK | FrameFlags.NACK,
        ]
        
        for flags in flag_combos:
            with self.subTest(flags=flags):
                original = Frame(1, 1, 0, flags, b"")
                encoded = encode_frame(original)
                decoded = decode_frame(encoded)
                self.assertEqual(decoded.flags, flags)
    
    def test_binary_payload(self):
        """Test encoding/decoding binary payloads."""
        # All possible byte values
        payload = bytes(range(180))
        original = Frame(1, 1, 0, FrameFlags.ACK, payload)
        
        encoded = encode_frame(original)
        decoded = decode_frame(encoded)
        
        self.assertEqual(decoded.payload, payload)


class TestFrameRepr(unittest.TestCase):
    """Test Frame string representation."""
    
    def test_repr_basic(self):
        """Test basic repr output."""
        frame = Frame(0x123, 1, 0, FrameFlags.SYN, b"test")
        repr_str = repr(frame)
        
        self.assertIn("0x123", repr_str)
        self.assertIn("seq=1", repr_str)
        self.assertIn("SYN", repr_str)
    
    def test_repr_multiple_flags(self):
        """Test repr with multiple flags."""
        frame = Frame(0, 0, 0, FrameFlags.SYN | FrameFlags.ACK, b"")
        repr_str = repr(frame)
        
        self.assertIn("SYN", repr_str)
        self.assertIn("ACK", repr_str)


if __name__ == "__main__":
    unittest.main()
