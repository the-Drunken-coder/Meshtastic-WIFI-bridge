"""Client-side stream manager for LoRa WiFi Forwarder."""

import threading
import random
from typing import Dict, Optional, Callable

from framing.frame import Frame, FrameFlags
from framing.codec import decode_frame, encode_frame, FrameDecodeError
from reliability.stream import Stream, StreamState
from common.config import Config
from common.logging_setup import get_logger

logger = get_logger(__name__)


class ClientStreamManager:
    """
    Manages streams on the client side.
    
    Responsibilities:
    - Create new streams for proxy connections
    - Route incoming frames to appropriate streams
    - Handle stream lifecycle
    """
    
    def __init__(
        self,
        gateway_node_id: int,
        config: Config,
        send_callback: Callable[[int, bytes], bool],
    ):
        """
        Initialize client stream manager.
        
        Args:
            gateway_node_id: Node ID of the gateway
            config: Configuration settings
            send_callback: Function to send data (node_id, data) -> success
        """
        self.gateway_node_id = gateway_node_id
        self.config = config
        self._send_callback = send_callback
        
        self._streams: Dict[int, Stream] = {}
        self._stream_id_counter = random.randint(1, 0x7FFFFFFF)
        self._lock = threading.RLock()
    
    def _allocate_stream_id(self) -> int:
        """Allocate a unique stream ID."""
        with self._lock:
            stream_id = self._stream_id_counter
            self._stream_id_counter = (self._stream_id_counter + 1) & 0xFFFFFFFF
            if self._stream_id_counter == 0:
                self._stream_id_counter = 1
            return stream_id
    
    def _create_stream_send_callback(self) -> Callable[[bytes], bool]:
        """Create a send callback for a stream."""
        def send(data: bytes) -> bool:
            return self._send_callback(self.gateway_node_id, data)
        return send
    
    def create_stream(self, host: str, port: int) -> Optional[Stream]:
        """
        Create a new stream to connect to a remote host.
        
        Args:
            host: Target host
            port: Target port
            
        Returns:
            Stream if created successfully, None otherwise
        """
        stream_id = self._allocate_stream_id()
        
        stream = Stream(
            stream_id=stream_id,
            remote_node_id=self.gateway_node_id,
            config=self.config,
            send_callback=self._create_stream_send_callback(),
        )
        
        with self._lock:
            self._streams[stream_id] = stream
        
        # Send SYN with CONNECT request
        connect_payload = f"CONNECT {host}:{port}".encode("utf-8")
        
        # Create SYN frame directly with payload
        syn_frame = Frame(
            stream_id=stream_id,
            seq=stream.window.allocate_seq(),
            ack=stream.window.next_expected_seq,
            flags=FrameFlags.SYN,
            payload=connect_payload,
        )
        
        encoded = encode_frame(syn_frame)
        if self._send_callback(self.gateway_node_id, encoded):
            stream.window.mark_sent(syn_frame)
            stream.state = StreamState.SYN_SENT
            logger.info(
                f"Stream {stream_id:#x}: SYN sent for CONNECT {host}:{port}"
            )
            return stream
        
        # Failed to send
        with self._lock:
            self._streams.pop(stream_id, None)
        
        return None
    
    def get_stream(self, stream_id: int) -> Optional[Stream]:
        """Get a stream by ID."""
        with self._lock:
            return self._streams.get(stream_id)
    
    def handle_frame(self, from_node: int, raw_data: bytes) -> None:
        """
        Handle incoming frame from gateway.
        
        Args:
            from_node: Source node ID
            raw_data: Raw frame bytes
        """
        try:
            frame = decode_frame(raw_data)
        except FrameDecodeError as e:
            logger.error(f"Failed to decode frame from node {from_node:#x}: {e}")
            return
        
        stream_id = frame.stream_id
        
        with self._lock:
            stream = self._streams.get(stream_id)
        
        if stream:
            stream.receive_frame(frame)
            
            # Handle stream closure
            if stream.state in (StreamState.CLOSED, StreamState.FIN_RECV):
                self._cleanup_stream(stream_id)
        else:
            # Unknown stream - might be a late RST
            if not frame.is_rst():
                logger.warning(
                    f"Received frame for unknown stream {stream_id:#x}"
                )
    
    def close_stream(self, stream_id: int) -> None:
        """Close a stream gracefully."""
        with self._lock:
            stream = self._streams.get(stream_id)
        
        if stream:
            stream.close()
    
    def _cleanup_stream(self, stream_id: int) -> None:
        """Clean up a closed stream."""
        with self._lock:
            stream = self._streams.pop(stream_id, None)
        
        if stream:
            logger.info(
                f"Stream {stream_id:#x} closed: "
                f"sent={stream.stats.bytes_sent}B, "
                f"recv={stream.stats.bytes_received}B, "
                f"retransmits={stream.stats.retransmits}"
            )
    
    def check_streams(self) -> None:
        """
        Periodic check for retransmits and timeouts.
        
        Should be called by the retransmit timer.
        """
        with self._lock:
            stream_ids = list(self._streams.keys())
        
        for stream_id in stream_ids:
            with self._lock:
                stream = self._streams.get(stream_id)
            
            if not stream:
                continue
            
            # Check retransmits
            if not stream.check_retransmits():
                logger.warning(
                    f"Stream {stream_id:#x}: Giving up, too many retransmits"
                )
                stream.reset()
                self._cleanup_stream(stream_id)
                continue
            
            # Check timeout
            if stream.is_timed_out():
                logger.warning(f"Stream {stream_id:#x}: Timed out")
                stream.reset()
                self._cleanup_stream(stream_id)
    
    def shutdown(self) -> None:
        """Shutdown all streams."""
        with self._lock:
            for stream_id, stream in list(self._streams.items()):
                stream.reset()
                self._streams.pop(stream_id, None)
        
        logger.info("Client stream manager shut down")
