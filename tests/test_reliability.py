"""Tests for sliding window reliability."""

import unittest
import sys
import os
import time

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framing.frame import Frame, FrameFlags
from reliability.window import SlidingWindow, PendingFrame


class TestSlidingWindow(unittest.TestCase):
    """Test SlidingWindow class."""
    
    def test_initial_state(self):
        """Test initial window state."""
        window = SlidingWindow(window_size=4)
        
        self.assertEqual(window.window_size, 4)
        self.assertEqual(window.next_seq, 0)
        self.assertEqual(window.next_expected_seq, 0)
        self.assertTrue(window.can_send())
        self.assertEqual(window.pending_count(), 0)
    
    def test_mark_sent_fills_window(self):
        """Test that marking frames as sent fills the window."""
        window = SlidingWindow(window_size=2)
        
        frame1 = Frame(1, 0, 0, FrameFlags.ACK, b"data1")
        frame2 = Frame(1, 1, 0, FrameFlags.ACK, b"data2")
        
        window.mark_sent(frame1)
        self.assertTrue(window.can_send())
        self.assertEqual(window.pending_count(), 1)
        
        window.mark_sent(frame2)
        self.assertFalse(window.can_send())
        self.assertEqual(window.pending_count(), 2)
    
    def test_ack_processing(self):
        """Test ACK processing releases frames from window."""
        window = SlidingWindow(window_size=4)
        
        frames = [
            Frame(1, 0, 0, FrameFlags.ACK, b"0"),
            Frame(1, 1, 0, FrameFlags.ACK, b"1"),
            Frame(1, 2, 0, FrameFlags.ACK, b"2"),
        ]
        
        for f in frames:
            window.mark_sent(f)
        
        self.assertEqual(window.pending_count(), 3)
        
        # ACK up to seq 2 (acknowledges 0 and 1)
        acked = window.process_ack(2)
        
        self.assertEqual(len(acked), 2)
        self.assertEqual(acked[0].seq, 0)
        self.assertEqual(acked[1].seq, 1)
        self.assertEqual(window.pending_count(), 1)
    
    def test_receive_in_order(self):
        """Test receiving frames in order."""
        window = SlidingWindow()
        
        frame0 = Frame(1, 0, 0, FrameFlags.ACK, b"first")
        frame1 = Frame(1, 1, 0, FrameFlags.ACK, b"second")
        
        result0 = window.receive_frame(frame0)
        self.assertIsNotNone(result0)
        self.assertEqual(result0.seq, 0)
        self.assertEqual(window.next_expected_seq, 1)
        
        result1 = window.receive_frame(frame1)
        self.assertIsNotNone(result1)
        self.assertEqual(result1.seq, 1)
        self.assertEqual(window.next_expected_seq, 2)
    
    def test_receive_out_of_order(self):
        """Test receiving frames out of order."""
        window = SlidingWindow()
        
        # Receive frame 2 before 0 and 1
        frame2 = Frame(1, 2, 0, FrameFlags.ACK, b"third")
        result2 = window.receive_frame(frame2)
        
        self.assertIsNone(result2)  # Buffered, not delivered
        self.assertEqual(window.next_expected_seq, 0)
        
        # Now receive frame 0
        frame0 = Frame(1, 0, 0, FrameFlags.ACK, b"first")
        result0 = window.receive_frame(frame0)
        
        self.assertIsNotNone(result0)
        self.assertEqual(window.next_expected_seq, 1)
        
        # Frame 2 is still buffered
        deliverable = window.get_deliverable_frames()
        self.assertEqual(len(deliverable), 0)  # Frame 1 is missing
        
        # Receive frame 1
        frame1 = Frame(1, 1, 0, FrameFlags.ACK, b"second")
        result1 = window.receive_frame(frame1)
        
        self.assertIsNotNone(result1)
        self.assertEqual(window.next_expected_seq, 2)
        
        # Now frame 2 should be deliverable
        deliverable = window.get_deliverable_frames()
        self.assertEqual(len(deliverable), 1)
        self.assertEqual(deliverable[0].seq, 2)
        self.assertEqual(window.next_expected_seq, 3)
    
    def test_receive_duplicate(self):
        """Test that duplicates are rejected."""
        window = SlidingWindow()
        
        frame = Frame(1, 0, 0, FrameFlags.ACK, b"data")
        
        result1 = window.receive_frame(frame)
        self.assertIsNotNone(result1)
        
        result2 = window.receive_frame(frame)
        self.assertIsNone(result2)  # Duplicate
    
    def test_get_missing_seqs(self):
        """Test getting missing sequence numbers."""
        window = SlidingWindow()
        
        # Receive frame 3, skip 0, 1, 2
        frame3 = Frame(1, 3, 0, FrameFlags.ACK, b"data")
        window.receive_frame(frame3)
        
        missing = window.get_missing_seqs()
        self.assertEqual(missing, [0, 1, 2])
    
    def test_retransmit_timeout(self):
        """Test retransmit timeout detection."""
        window = SlidingWindow()
        
        frame = Frame(1, 0, 0, FrameFlags.ACK, b"data")
        window.mark_sent(frame)
        
        # No timeout yet
        to_retransmit, exceeded = window.get_pending_for_retransmit(
            timeout_ms=1000, max_retransmits=3
        )
        self.assertEqual(len(to_retransmit), 0)
        
        # Force timeout by manipulating time (we'll use small timeout)
        time.sleep(0.1)
        to_retransmit, exceeded = window.get_pending_for_retransmit(
            timeout_ms=50, max_retransmits=3
        )
        self.assertEqual(len(to_retransmit), 1)
        self.assertEqual(to_retransmit[0].seq, 0)
    
    def test_max_retransmits_exceeded(self):
        """Test max retransmits exceeded detection."""
        window = SlidingWindow()
        
        frame = Frame(1, 0, 0, FrameFlags.ACK, b"data")
        window.mark_sent(frame)
        
        # Trigger timeout multiple times
        for _ in range(3):
            time.sleep(0.05)
            window.get_pending_for_retransmit(timeout_ms=10, max_retransmits=3)
        
        # One more should exceed max
        time.sleep(0.05)
        to_retransmit, exceeded = window.get_pending_for_retransmit(
            timeout_ms=10, max_retransmits=3
        )
        
        self.assertEqual(len(exceeded), 1)
        self.assertEqual(exceeded[0], 0)
    
    def test_clear(self):
        """Test clearing window state."""
        window = SlidingWindow()
        
        frame = Frame(1, 0, 0, FrameFlags.ACK, b"data")
        window.mark_sent(frame)
        window.receive_frame(Frame(1, 0, 0, FrameFlags.ACK, b"recv"))
        
        window.clear()
        
        self.assertEqual(window.next_seq, 0)
        self.assertEqual(window.next_expected_seq, 0)
        self.assertEqual(window.pending_count(), 0)


class TestSlidingWindowConcurrency(unittest.TestCase):
    """Test SlidingWindow thread safety."""
    
    def test_concurrent_send_ack(self):
        """Test concurrent send and ACK operations."""
        import threading
        
        window = SlidingWindow(window_size=10)
        errors = []
        
        def sender():
            try:
                for i in range(100):
                    if window.can_send():
                        frame = Frame(1, window.allocate_seq(), 0, FrameFlags.ACK, b"x")
                        window.mark_sent(frame)
            except Exception as e:
                errors.append(e)
        
        def acker():
            try:
                for _ in range(100):
                    window.process_ack(window.next_seq)
            except Exception as e:
                errors.append(e)
        
        threads = [
            threading.Thread(target=sender),
            threading.Thread(target=acker),
        ]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        self.assertEqual(len(errors), 0)


if __name__ == "__main__":
    unittest.main()
