"""Tests for ACK/NACK methods."""

import unittest
import sys
import os
from unittest.mock import Mock, MagicMock

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framing.frame import Frame, FrameFlags
from reliability.ack_methods.basic import BasicAckNack
from reliability.stream import StreamState
from reliability.window import SlidingWindow


class TestBasicAckNack(unittest.TestCase):
    """Test BasicAckNack ACK/NACK strategy."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.ack_nack = BasicAckNack()
        self.stream = Mock()
        self.stream.stream_id = 12345
        self.stream.window = SlidingWindow(window_size=4)
        self.stream.stats = Mock()
        self.stream.stats.retransmits = 0
    
    def test_handle_control_ack_in_syn_sent_state(self):
        """Test that ACK in SYN_SENT state transitions to OPEN."""
        # Setup stream in SYN_SENT state
        self.stream.state = StreamState.SYN_SENT
        
        # Create a frame to ack
        sent_frame = Frame(12345, 0, 0, FrameFlags.SYN, b"data")
        self.stream.window.mark_sent(sent_frame)
        
        # Create ACK frame that acknowledges seq 0
        ack_frame = Frame(12345, 1, 1, FrameFlags.ACK, b"")
        
        # Handle the ACK
        result = self.ack_nack.handle_control(self.stream, ack_frame)
        
        # Verify state transition
        self.assertEqual(self.stream.state, StreamState.OPEN)
        self.assertEqual(result, [])
    
    def test_handle_control_ack_in_open_state(self):
        """Test that ACK in OPEN state does not change state."""
        # Setup stream in OPEN state
        self.stream.state = StreamState.OPEN
        
        # Create a frame to ack
        sent_frame = Frame(12345, 0, 0, FrameFlags.ACK, b"data")
        self.stream.window.mark_sent(sent_frame)
        
        # Create ACK frame
        ack_frame = Frame(12345, 1, 1, FrameFlags.ACK, b"")
        
        # Handle the ACK
        result = self.ack_nack.handle_control(self.stream, ack_frame)
        
        # Verify state unchanged
        self.assertEqual(self.stream.state, StreamState.OPEN)
        self.assertEqual(result, [])
    
    def test_handle_control_ack_with_no_frames_acked(self):
        """Test ACK when no frames are actually acknowledged."""
        self.stream.state = StreamState.SYN_SENT
        
        # Create ACK frame that doesn't acknowledge any pending frames
        ack_frame = Frame(12345, 0, 0, FrameFlags.ACK, b"")
        
        # Handle the ACK
        result = self.ack_nack.handle_control(self.stream, ack_frame)
        
        # State should remain SYN_SENT since nothing was acked
        self.assertEqual(self.stream.state, StreamState.SYN_SENT)
        self.assertEqual(result, [])
    
    def test_handle_control_nack_with_pending_frame(self):
        """Test NACK handling when frame is pending."""
        self.stream.state = StreamState.OPEN
        
        # Create a frame to NACK
        sent_frame = Frame(12345, 5, 0, FrameFlags.ACK, b"data")
        self.stream.window.mark_sent(sent_frame)
        
        # Create NACK frame
        nack_frame = Frame(12345, 1, 5, FrameFlags.NACK, b"")
        
        # Handle the NACK
        result = self.ack_nack.handle_control(self.stream, nack_frame)
        
        # Verify retransmit is requested
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].seq, 5)
        self.assertEqual(result[0].payload, b"data")
        self.assertEqual(self.stream.stats.retransmits, 1)
    
    def test_handle_control_nack_without_pending_frame(self):
        """Test NACK handling when frame is not pending."""
        self.stream.state = StreamState.OPEN
        
        # Create NACK frame for non-existent seq
        nack_frame = Frame(12345, 1, 99, FrameFlags.NACK, b"")
        
        # Handle the NACK
        result = self.ack_nack.handle_control(self.stream, nack_frame)
        
        # No retransmit should be requested
        self.assertEqual(result, [])
        self.assertEqual(self.stream.stats.retransmits, 0)
    
    def test_handle_control_combined_ack_and_nack(self):
        """Test handling frame with both ACK and NACK flags."""
        self.stream.state = StreamState.SYN_SENT
        
        # Create frames
        sent_frame1 = Frame(12345, 0, 0, FrameFlags.SYN, b"data1")
        sent_frame2 = Frame(12345, 1, 0, FrameFlags.ACK, b"data2")
        self.stream.window.mark_sent(sent_frame1)
        self.stream.window.mark_sent(sent_frame2)
        
        # Create frame with both ACK and NACK
        ack_nack_frame = Frame(12345, 2, 1, FrameFlags.ACK | FrameFlags.NACK, b"")
        
        # Handle the frame
        result = self.ack_nack.handle_control(self.stream, ack_nack_frame)
        
        # ACK should be processed (state transition) and NACK should request retransmit
        self.assertEqual(self.stream.state, StreamState.OPEN)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].seq, 1)
    
    def test_on_missing_with_missing_sequences(self):
        """Test on_missing generates NACK for first missing sequence."""
        self.stream.state = StreamState.OPEN
        self.stream.window._next_seq = 10
        
        missing_seqs = [3, 4, 5]
        
        result = self.ack_nack.on_missing(self.stream, missing_seqs)
        
        # Should generate NACK for first missing seq
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].stream_id, 12345)
        self.assertEqual(result[0].ack, 3)
        self.assertTrue(result[0].flags & FrameFlags.NACK)
        self.assertEqual(result[0].payload, b"")
    
    def test_on_missing_with_empty_list(self):
        """Test on_missing with no missing sequences."""
        result = self.ack_nack.on_missing(self.stream, [])
        
        # Should return empty list
        self.assertEqual(result, [])
    
    def test_on_complete_generates_ack(self):
        """Test on_complete generates ACK frame."""
        self.stream.state = StreamState.OPEN
        self.stream.window._next_seq = 10
        self.stream.window._next_expected_seq = 5
        
        result = self.ack_nack.on_complete(self.stream)
        
        # Should generate ACK frame
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].stream_id, 12345)
        self.assertEqual(result[0].ack, 5)
        self.assertTrue(result[0].flags & FrameFlags.ACK)
        self.assertEqual(result[0].payload, b"")
    
    def test_on_complete_allocates_sequence_number(self):
        """Test on_complete properly allocates sequence numbers."""
        self.stream.state = StreamState.OPEN
        self.stream.window._next_seq = 10
        self.stream.window._next_expected_seq = 5
        
        # Call on_complete twice
        result1 = self.ack_nack.on_complete(self.stream)
        result2 = self.ack_nack.on_complete(self.stream)
        
        # Each should allocate the current sequence number
        self.assertEqual(result1[0].seq, 10)
        self.assertEqual(result2[0].seq, 10)  # allocate_seq doesn't increment


if __name__ == '__main__':
    unittest.main()
