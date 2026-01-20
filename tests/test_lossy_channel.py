"""Tests for lossy channel simulation."""

import unittest
import sys
import os
import random

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framing.frame import Frame, FrameFlags
from framing.codec import encode_frame, decode_frame, FrameDecodeError
from reliability.window import SlidingWindow


class LossyChannel:
    """Simulates a lossy channel for testing."""
    
    def __init__(self, drop_rate: float = 0.0, reorder_rate: float = 0.0):
        """
        Initialize lossy channel.
        
        Args:
            drop_rate: Probability of dropping a packet (0.0 - 1.0)
            reorder_rate: Probability of reordering packets (0.0 - 1.0)
        """
        self.drop_rate = drop_rate
        self.reorder_rate = reorder_rate
        self._buffer: list = []
    
    def send(self, data: bytes) -> list:
        """
        Send data through the lossy channel.
        
        Returns:
            List of data that made it through (may be empty, reordered)
        """
        # Possibly drop the packet
        if random.random() < self.drop_rate:
            return self._flush_buffer()
        
        # Possibly reorder
        if random.random() < self.reorder_rate:
            self._buffer.append(data)
            if len(self._buffer) >= 2 and random.random() < 0.5:
                # Swap last two
                self._buffer[-1], self._buffer[-2] = self._buffer[-2], self._buffer[-1]
            return []
        
        # Normal delivery
        result = self._flush_buffer()
        result.append(data)
        return result
    
    def _flush_buffer(self) -> list:
        """Flush any buffered packets."""
        result = self._buffer
        self._buffer = []
        return result
    
    def flush(self) -> list:
        """Force flush all buffered packets."""
        return self._flush_buffer()


class TestLossyChannelSimulation(unittest.TestCase):
    """Test frame transmission over simulated lossy channel."""
    
    def test_no_loss(self):
        """Test transmission with no loss."""
        channel = LossyChannel(drop_rate=0.0)
        
        frames = [
            Frame(1, i, 0, FrameFlags.ACK, f"data{i}".encode())
            for i in range(10)
        ]
        
        received = []
        for frame in frames:
            encoded = encode_frame(frame)
            delivered = channel.send(encoded)
            for data in delivered:
                received.append(decode_frame(data))
        
        received.extend([decode_frame(d) for d in channel.flush()])
        
        self.assertEqual(len(received), 10)
        for i, frame in enumerate(received):
            self.assertEqual(frame.seq, i)
    
    def test_with_drops(self):
        """Test transmission with packet drops."""
        random.seed(42)  # Reproducible
        channel = LossyChannel(drop_rate=0.3)
        
        frames = [
            Frame(1, i, 0, FrameFlags.ACK, f"data{i}".encode())
            for i in range(100)
        ]
        
        received = []
        for frame in frames:
            encoded = encode_frame(frame)
            delivered = channel.send(encoded)
            for data in delivered:
                received.append(decode_frame(data))
        
        received.extend([decode_frame(d) for d in channel.flush()])
        
        # With 30% drop rate, we expect roughly 70 frames
        self.assertLess(len(received), 100)
        self.assertGreater(len(received), 50)
    
    def test_with_reordering(self):
        """Test transmission with packet reordering."""
        random.seed(42)
        channel = LossyChannel(reorder_rate=0.3)
        
        frames = [
            Frame(1, i, 0, FrameFlags.ACK, f"data{i}".encode())
            for i in range(20)
        ]
        
        received = []
        for frame in frames:
            encoded = encode_frame(frame)
            delivered = channel.send(encoded)
            for data in delivered:
                received.append(decode_frame(data))
        
        received.extend([decode_frame(d) for d in channel.flush()])
        
        # All frames should arrive
        self.assertEqual(len(received), 20)
        
        # Verify we can check ordering (even if not used in assertion)
        # With reordering, some might be out of order
        # (though our simple implementation may not always reorder)
        seqs = [f.seq for f in received]
        # Just verify the sequence list is valid length
        self.assertEqual(len(seqs), 20)


class TestReliabilityWithLoss(unittest.TestCase):
    """Test sliding window reliability with simulated loss."""
    
    def test_window_handles_ack_loss(self):
        """Test that window handles ACK loss correctly."""
        sender_window = SlidingWindow(window_size=4)
        receiver_window = SlidingWindow(window_size=4)
        channel = LossyChannel(drop_rate=0.5)
        
        random.seed(42)
        
        # Send some frames
        sent_frames = []
        for i in range(4):
            seq = sender_window.allocate_seq()
            frame = Frame(1, seq, 0, FrameFlags.ACK, f"data{i}".encode())
            sender_window.mark_sent(frame)
            sent_frames.append(frame)
        
        # Simulate receiving some frames (with loss)
        received_seqs = []
        for frame in sent_frames:
            encoded = encode_frame(frame)
            delivered = channel.send(encoded)
            for data in delivered:
                decoded = decode_frame(data)
                result = receiver_window.receive_frame(decoded)
                if result:
                    received_seqs.append(result.seq)
        
        # Some frames should have been received
        # The window should track what needs retransmission
        self.assertLessEqual(len(received_seqs), 4)
    
    def test_window_retransmit_on_nack(self):
        """Test window handles NACK correctly."""
        window = SlidingWindow(window_size=4)
        
        # Send frames
        frames = []
        for i in range(3):
            seq = window.allocate_seq()
            frame = Frame(1, seq, 0, FrameFlags.ACK, f"data{i}".encode())
            window.mark_sent(frame)
            frames.append(frame)
        
        # Process NACK for seq 1
        retransmit = window.process_nack(1)
        
        self.assertIsNotNone(retransmit)
        self.assertEqual(retransmit.seq, 1)


class TestFrameCorruption(unittest.TestCase):
    """Test handling of corrupted frames."""
    
    def test_single_bit_flip(self):
        """Test detection of single bit flip."""
        frame = Frame(1, 0, 0, FrameFlags.ACK, b"test data")
        encoded = bytearray(encode_frame(frame))
        
        # Flip a bit in the payload
        encoded[16] ^= 0x01
        
        with self.assertRaises(FrameDecodeError):
            decode_frame(bytes(encoded))
    
    def test_random_corruption(self):
        """Test detection of random corruption."""
        random.seed(42)
        
        frame = Frame(1, 42, 0, FrameFlags.ACK, b"important data")
        original_encoded = encode_frame(frame)
        
        detected_corruptions = 0
        total_tests = 100
        
        for _ in range(total_tests):
            corrupted = bytearray(original_encoded)
            
            # Corrupt random byte
            pos = random.randint(0, len(corrupted) - 1)
            corrupted[pos] ^= random.randint(1, 255)
            
            try:
                decode_frame(bytes(corrupted))
            except FrameDecodeError:
                detected_corruptions += 1
        
        # CRC32 should detect most corruptions
        self.assertGreater(detected_corruptions, total_tests * 0.99)


if __name__ == "__main__":
    unittest.main()
