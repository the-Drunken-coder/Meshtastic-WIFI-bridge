"""Stream abstraction for reliable LoRa transport."""

import threading
import time
from enum import Enum, auto
from typing import Callable, Optional, List
from dataclasses import dataclass, field

from framing.frame import Frame, FrameFlags
from framing.codec import encode_frame
from reliability.ack_methods.base import AckNackMethod
from reliability.ack_methods.smart import SmartAckNack
from reliability.window import SlidingWindow, PendingFrame
from common.logging_setup import get_logger
from common.chunking import clamp_chunk_size, iter_chunks
from common.config import Config

logger = get_logger(__name__)


class StreamState(Enum):
    """Stream lifecycle states."""
    
    CLOSED = auto()      # Initial/final state
    SYN_SENT = auto()    # Client sent SYN, waiting for ACK
    SYN_RECV = auto()    # Server received SYN, sent ACK
    OPEN = auto()        # Stream established
    FIN_SENT = auto()    # Sent FIN, waiting for ACK
    FIN_RECV = auto()    # Received FIN


@dataclass
class StreamStats:
    """Statistics for a stream."""
    
    bytes_sent: int = 0
    bytes_received: int = 0
    frames_sent: int = 0
    frames_received: int = 0
    retransmits: int = 0
    created_at: float = field(default_factory=time.time)
    rtt_count: int = 0
    rtt_sum_ms: float = 0.0
    rtt_max_ms: float = 0.0
    max_pending: int = 0


class Stream:
    """
    Represents a single reliable stream over LoRa transport.
    
    A stream provides:
    - Ordered, reliable delivery of bytes
    - Flow control via sliding window
    - Automatic retransmission
    """
    
    def __init__(
        self,
        stream_id: int,
        remote_node_id: int,
        config: Optional[Config] = None,
        send_callback: Optional[Callable[[bytes], bool]] = None,
        ack_method: Optional[AckNackMethod] = None,
    ):
        """
        Initialize a stream.
        
        Args:
            stream_id: Unique stream identifier
            remote_node_id: Remote Meshtastic node ID
            config: Configuration settings
            send_callback: Callback to send encoded frames
        """
        self.stream_id = stream_id
        self.remote_node_id = remote_node_id
        self.config = config or Config()
        self._send_callback = send_callback
        self._ack_method = ack_method or SmartAckNack()
        
        self.state = StreamState.CLOSED
        self.window = SlidingWindow(window_size=self.config.window_size)
        self.stats = StreamStats()
        
        # Received data buffer
        self._recv_buffer: bytes = b""
        self._recv_lock = threading.Lock()
        self._recv_event = threading.Event()
        
        # Send queue for data waiting to enter window
        self._send_queue: List[bytes] = []
        self._send_lock = threading.Lock()
        
        # Last activity for timeout
        self.last_activity = time.time()
        
        self._lock = threading.RLock()
    
    def _send_frame(self, frame: Frame) -> bool:
        """Send a frame through the transport."""
        if not self._send_callback:
            logger.error(f"Stream {self.stream_id:#x}: No send callback configured")
            return False
        
        encoded = encode_frame(frame)
        success = self._send_callback(encoded)
        
        if success:
            self.stats.frames_sent += 1
            self.stats.bytes_sent += len(frame.payload)
            self.last_activity = time.time()
            logger.debug(f"Stream {self.stream_id:#x}: Sent {frame}")
        
        return success

    def record_ack_rtts(self, pendings: List[PendingFrame]) -> None:
        """Record RTT stats for acknowledged frames."""
        now = time.time()
        for pending in pendings:
            rtt_ms = max((now - pending.send_time) * 1000.0, 0.0)
            self.stats.rtt_count += 1
            self.stats.rtt_sum_ms += rtt_ms
            if rtt_ms > self.stats.rtt_max_ms:
                self.stats.rtt_max_ms = rtt_ms

    def _update_max_pending(self) -> None:
        pending = self.window.pending_count()
        if pending > self.stats.max_pending:
            self.stats.max_pending = pending
    
    def open(self) -> bool:
        """
        Initiate stream opening (client side).
        
        Sends SYN frame to remote.
        
        Returns:
            True if SYN was sent
        """
        with self._lock:
            if self.state != StreamState.CLOSED:
                logger.warning(
                    f"Stream {self.stream_id:#x}: Cannot open, state is {self.state}"
                )
                return False
            
            # Send SYN frame
            frame = Frame(
                stream_id=self.stream_id,
                seq=self.window.allocate_seq(),
                ack=self.window.next_expected_seq,
                flags=FrameFlags.SYN,
                payload=b"",
            )
            
            if self._send_frame(frame):
                self.window.mark_sent(frame)
                self._update_max_pending()
                self.state = StreamState.SYN_SENT
                logger.info(f"Stream {self.stream_id:#x}: SYN sent, state=SYN_SENT")
                return True
            
            return False
    
    def accept(self) -> bool:
        """
        Accept incoming stream (server side).
        
        Called when SYN is received.
        
        Returns:
            True if ACK was sent
        """
        with self._lock:
            if self.state != StreamState.CLOSED:
                return False
            
            self.state = StreamState.SYN_RECV
            
            # Send SYN-ACK
            frame = Frame(
                stream_id=self.stream_id,
                seq=self.window.allocate_seq(),
                ack=self.window.next_expected_seq,
                flags=FrameFlags.SYN | FrameFlags.ACK,
                payload=b"",
            )
            
            if self._send_frame(frame):
                self.window.mark_sent(frame)
                self._update_max_pending()
                self.state = StreamState.OPEN
                logger.info(f"Stream {self.stream_id:#x}: Accepted, state=OPEN")
                return True
            
            # Sending SYN-ACK failed; revert to CLOSED to keep state consistent.
            logger.warning(
                f"Stream {self.stream_id:#x}: Failed to send SYN-ACK, reverting to CLOSED"
            )
            self.state = StreamState.CLOSED
            return False
    
    def send(self, data: bytes) -> int:
        """
        Queue data for sending.
        
        Data will be chunked and sent as window allows.
        
        Args:
            data: Data to send
            
        Returns:
            Number of bytes queued
        """
        with self._lock:
            if self.state not in (StreamState.OPEN, StreamState.SYN_SENT):
                logger.warning(
                    f"Stream {self.stream_id:#x}: Cannot send, state is {self.state}"
                )
                return 0
        
        chunk_size = clamp_chunk_size(self.config.chunk_payload_size)

        with self._send_lock:
            # Split data into chunks
            for chunk in iter_chunks(data, chunk_size):
                self._send_queue.append(chunk)
        
        # Try to send queued data
        self._process_send_queue()
        
        return len(data)
    
    def _process_send_queue(self) -> int:
        """
        Send queued data as window allows.
        
        Returns:
            Number of frames sent
        """
        sent = 0
        
        with self._send_lock:
            while self._send_queue and self.window.can_send():
                chunk = self._send_queue.pop(0)

                with self._lock:
                    frame = Frame(
                        stream_id=self.stream_id,
                        seq=self.window.allocate_seq(),
                        ack=self.window.next_expected_seq,
                        flags=FrameFlags.ACK,
                        payload=chunk,
                    )

                    for control in self._ack_method.on_send(self, frame):
                        self._send_frame(control)

                    if self._send_frame(frame):
                        self.window.mark_sent(frame)
                        self._update_max_pending()
                        sent += 1
                    else:
                        # Failed to send, put back in queue
                        self._send_queue.insert(0, chunk)
                        break

            if sent:
                for control in self._ack_method.on_chunks_sent(self):
                    self._send_frame(control)

        return sent
    
    def receive_frame(self, frame: Frame) -> None:
        """
        Process a received frame.
        
        Args:
            frame: Received frame for this stream
        """
        with self._lock:
            self.last_activity = time.time()
            self.stats.frames_received += 1
            
            logger.debug(f"Stream {self.stream_id:#x}: Received {frame}")
            
            # Let ACK/NACK method process control info
            for control in self._ack_method.handle_control(self, frame):
                self._send_frame(control)
            
            # Handle control frames
            if frame.is_syn():
                if self.state == StreamState.CLOSED:
                    # New stream request
                    pass  # Caller should call accept()
                return
            
            if frame.is_rst():
                self.state = StreamState.CLOSED
                self.window.clear()
                logger.info(f"Stream {self.stream_id:#x}: RST received, state=CLOSED")
                return
            
            if frame.is_fin():
                self.state = StreamState.FIN_RECV
                # Send ACK for FIN
                ack_frame = Frame(
                    stream_id=self.stream_id,
                    seq=self.window.allocate_seq(),
                    ack=frame.seq + 1,
                    flags=FrameFlags.ACK,
                    payload=b"",
                )
                self._send_frame(ack_frame)
                logger.info(f"Stream {self.stream_id:#x}: FIN received, state=FIN_RECV")
                return
            
            # Process payload data
            if frame.payload:
                delivered = self.window.receive_frame(frame)

                if delivered:
                    with self._recv_lock:
                        self._recv_buffer += frame.payload
                        self.stats.bytes_received += len(frame.payload)
                        self._recv_event.set()

                    # Get any buffered frames now deliverable
                    for buffered in self.window.get_deliverable_frames():
                        with self._recv_lock:
                            self._recv_buffer += buffered.payload
                            self.stats.bytes_received += len(buffered.payload)

                if delivered:
                    for control in self._ack_method.on_complete(self):
                        self._send_frame(control)
                else:
                    missing = self.window.get_missing_seqs()
                    if missing:
                        for control in self._ack_method.on_missing(self, missing):
                            self._send_frame(control)

        # Try to send more queued data. Note: This is called outside the _lock
        # intentionally - _process_send_queue() has its own locking via _send_lock,
        # and holding _lock here could cause deadlocks. The window state is thread-safe.
        self._process_send_queue()
    
    def recv(self, max_bytes: int = 4096, timeout: Optional[float] = None) -> bytes:
        """
        Receive data from the stream.
        
        Args:
            max_bytes: Maximum bytes to return
            timeout: Timeout in seconds, None for blocking
            
        Returns:
            Received data (may be empty on timeout)
        """
        deadline = time.time() + timeout if timeout is not None else None
        
        while True:
            with self._recv_lock:
                if self._recv_buffer:
                    data = self._recv_buffer[:max_bytes]
                    self._recv_buffer = self._recv_buffer[max_bytes:]
                    if not self._recv_buffer:
                        self._recv_event.clear()
                    return data
            
            # Check for closed stream
            if self.state in (StreamState.CLOSED, StreamState.FIN_RECV):
                return b""
            
            # Wait for data
            remaining = deadline - time.time() if deadline is not None else None
            if remaining is not None and remaining <= 0:
                return b""
            
            self._recv_event.wait(timeout=remaining)
    
    def close(self) -> None:
        """
        Close the stream gracefully.
        
        Sends FIN frame.
        """
        with self._lock:
            if self.state == StreamState.CLOSED:
                return
            
            # Send FIN
            frame = Frame(
                stream_id=self.stream_id,
                seq=self.window.allocate_seq(),
                ack=self.window.next_expected_seq,
                flags=FrameFlags.FIN | FrameFlags.ACK,
                payload=b"",
            )
            
            if self._send_frame(frame):
                self.window.mark_sent(frame)
                self._update_max_pending()
                self.state = StreamState.FIN_SENT
                logger.info(f"Stream {self.stream_id:#x}: FIN sent, state=FIN_SENT")
    
    def reset(self) -> None:
        """
        Abort the stream immediately.
        
        Sends RST frame.
        """
        with self._lock:
            # Send RST
            frame = Frame(
                stream_id=self.stream_id,
                seq=0,
                ack=0,
                flags=FrameFlags.RST,
                payload=b"",
            )
            
            self._send_frame(frame)
            self.state = StreamState.CLOSED
            self.window.clear()
            logger.info(f"Stream {self.stream_id:#x}: RST sent, state=CLOSED")
    
    def check_retransmits(self) -> bool:
        """
        Check for frames needing retransmission.
        
        Returns:
            True if stream is still viable, False if should be closed
        """
        to_retransmit, exceeded = self.window.get_pending_for_retransmit(
            self.config.retransmit_timeout_ms,
            self.config.max_retransmits,
        )
        
        for frame in to_retransmit:
            self._send_frame(frame)
            self.stats.retransmits += 1
        
        if exceeded:
            logger.warning(
                f"Stream {self.stream_id:#x}: {len(exceeded)} frames exceeded max retransmits"
            )
            return False
        
        return True
    
    def is_timed_out(self) -> bool:
        """Check if stream has timed out due to inactivity."""
        return time.time() - self.last_activity > self.config.stream_timeout_s
