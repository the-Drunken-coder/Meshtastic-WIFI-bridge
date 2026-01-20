"""Sliding window implementation for reliable transport."""

import threading
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from collections import OrderedDict
import time

from framing.frame import Frame, FrameFlags
from common.logging_setup import get_logger

logger = get_logger(__name__)


@dataclass
class PendingFrame:
    """Frame pending acknowledgment."""
    
    frame: Frame
    send_time: float
    retransmit_count: int = 0


class SlidingWindow:
    """
    Sliding window implementation for flow control and reliability.
    
    Manages:
    - Sending frames within window constraints
    - Tracking pending (unacknowledged) frames
    - Processing incoming ACKs
    - Ordering received frames
    """
    
    def __init__(self, window_size: int = 4):
        """
        Initialize sliding window.
        
        Args:
            window_size: Maximum number of unacknowledged frames
        """
        self.window_size = window_size
        
        # Send state
        self._next_seq: int = 0  # Next sequence number to use
        self._pending: OrderedDict[int, PendingFrame] = OrderedDict()
        
        # Receive state
        self._next_expected_seq: int = 0  # Next expected sequence number
        self._received_buffer: Dict[int, Frame] = {}  # Out-of-order buffer
        
        self._lock = threading.RLock()
    
    @property
    def next_seq(self) -> int:
        """Get the next sequence number to use."""
        return self._next_seq
    
    @property
    def next_expected_seq(self) -> int:
        """Get the next expected sequence number (for ACK)."""
        return self._next_expected_seq
    
    def can_send(self) -> bool:
        """Check if the window allows sending more frames."""
        with self._lock:
            return len(self._pending) < self.window_size
    
    def mark_sent(self, frame: Frame) -> None:
        """
        Mark a frame as sent and pending ACK.
        
        Args:
            frame: The sent frame
        """
        with self._lock:
            self._pending[frame.seq] = PendingFrame(
                frame=frame,
                send_time=time.time(),
            )
            self._next_seq = frame.seq + 1
            logger.debug(
                f"Marked seq={frame.seq} as pending, window={len(self._pending)}/{self.window_size}"
            )
    
    def allocate_seq(self) -> int:
        """
        Allocate the next sequence number.
        
        Returns:
            The next sequence number to use
        """
        with self._lock:
            seq = self._next_seq
            return seq
    
    def process_ack(self, ack_num: int) -> List[Frame]:
        """
        Process an incoming ACK.
        
        ACK number acknowledges all frames with seq < ack_num.
        
        Args:
            ack_num: The acknowledgment number
            
        Returns:
            List of frames that were acknowledged
        """
        with self._lock:
            acked_frames = []
            
            # Find and remove all acknowledged frames
            seqs_to_remove = [seq for seq in self._pending.keys() if seq < ack_num]
            
            for seq in seqs_to_remove:
                pending = self._pending.pop(seq)
                acked_frames.append(pending.frame)
                logger.debug(f"ACK received for seq={seq}")
            
            if acked_frames:
                logger.debug(
                    f"Processed ACK={ack_num}, acked {len(acked_frames)} frames, "
                    f"pending={len(self._pending)}"
                )
            
            return acked_frames
    
    def process_nack(self, nack_seq: int) -> Optional[Frame]:
        """
        Process a NACK requesting retransmission.
        
        Args:
            nack_seq: The sequence number being NACKed
            
        Returns:
            The frame to retransmit, or None if not found
        """
        with self._lock:
            if nack_seq in self._pending:
                pending = self._pending[nack_seq]
                pending.retransmit_count += 1
                pending.send_time = time.time()
                logger.debug(f"NACK received for seq={nack_seq}")
                return pending.frame
            return None
    
    def get_pending_for_retransmit(
        self, timeout_ms: int, max_retransmits: int
    ) -> Tuple[List[Frame], List[int]]:
        """
        Get frames that need retransmission due to timeout.
        
        Args:
            timeout_ms: Timeout in milliseconds
            max_retransmits: Maximum retransmit attempts
            
        Returns:
            Tuple of (frames to retransmit, sequences that exceeded max retries)
        """
        now = time.time()
        timeout_s = timeout_ms / 1000.0
        
        to_retransmit: List[Frame] = []
        exceeded_max: List[int] = []
        
        with self._lock:
            for seq, pending in list(self._pending.items()):
                elapsed = now - pending.send_time
                
                if elapsed >= timeout_s:
                    if pending.retransmit_count >= max_retransmits:
                        exceeded_max.append(seq)
                        logger.warning(
                            f"seq={seq} exceeded max retransmits ({max_retransmits})"
                        )
                    else:
                        pending.retransmit_count += 1
                        pending.send_time = now
                        to_retransmit.append(pending.frame)
                        logger.debug(
                            f"Retransmitting seq={seq} (attempt {pending.retransmit_count})"
                        )
        
        return to_retransmit, exceeded_max
    
    def remove_pending(self, seq: int) -> Optional[Frame]:
        """
        Remove a pending frame without ACK (e.g., on stream reset).
        
        Args:
            seq: Sequence number to remove
            
        Returns:
            The removed frame, or None if not found
        """
        with self._lock:
            pending = self._pending.pop(seq, None)
            return pending.frame if pending else None
    
    def receive_frame(self, frame: Frame) -> Optional[Frame]:
        """
        Process a received frame and return it if in-order.
        
        Args:
            frame: Received frame
            
        Returns:
            The frame if it's the next expected, None if buffered
        """
        with self._lock:
            seq = frame.seq
            
            if seq < self._next_expected_seq:
                # Duplicate, already processed
                logger.debug(f"Duplicate frame seq={seq}, expected {self._next_expected_seq}")
                return None
            
            if seq == self._next_expected_seq:
                # In order, process immediately
                self._next_expected_seq = seq + 1
                logger.debug(f"Received in-order frame seq={seq}")
                
                # Check if we can deliver buffered frames too
                return frame
            
            # Out of order, buffer for later
            self._received_buffer[seq] = frame
            logger.debug(
                f"Buffered out-of-order frame seq={seq}, expected {self._next_expected_seq}"
            )
            return None
    
    def get_deliverable_frames(self) -> List[Frame]:
        """
        Get buffered frames that are now deliverable.
        
        Call after receive_frame() advances the expected seq.
        
        Returns:
            List of frames that can now be delivered in order
        """
        with self._lock:
            deliverable = []
            
            while self._next_expected_seq in self._received_buffer:
                frame = self._received_buffer.pop(self._next_expected_seq)
                deliverable.append(frame)
                self._next_expected_seq += 1
                logger.debug(f"Delivering buffered frame seq={frame.seq}")
            
            return deliverable
    
    def get_missing_seqs(self) -> List[int]:
        """
        Get sequence numbers that are missing (gaps in received).
        
        Useful for generating NACKs.
        
        Returns:
            List of missing sequence numbers
        """
        with self._lock:
            if not self._received_buffer:
                return []
            
            max_buffered = max(self._received_buffer.keys())
            missing = []
            
            for seq in range(self._next_expected_seq, max_buffered):
                if seq not in self._received_buffer:
                    missing.append(seq)
            
            return missing
    
    def clear(self) -> None:
        """Clear all window state."""
        with self._lock:
            self._pending.clear()
            self._received_buffer.clear()
            self._next_seq = 0
            self._next_expected_seq = 0
    
    def pending_count(self) -> int:
        """Get count of pending (unacknowledged) frames."""
        with self._lock:
            return len(self._pending)
