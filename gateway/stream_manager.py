"""Stream manager for gateway node."""

import socket
import threading
from typing import Dict, Optional, Callable

from framing.frame import Frame, FrameFlags
from framing.codec import decode_frame, encode_frame, FrameDecodeError
from reliability.stream import Stream, StreamState
from common.config import Config
from common.logging_setup import get_logger

logger = get_logger(__name__)


class OutboundConnection:
    """Manages an outbound TCP connection to the Internet."""
    
    def __init__(
        self,
        stream: Stream,
        host: str,
        port: int,
    ):
        """
        Initialize outbound connection.
        
        Args:
            stream: Associated LoRa stream
            host: Target host
            port: Target port
        """
        self.stream = stream
        self.host = host
        self.port = port
        self.socket: Optional[socket.socket] = None
        self._running = False
        self._recv_thread: Optional[threading.Thread] = None
    
    def connect(self) -> bool:
        """
        Open TCP connection to target.
        
        Returns:
            True if connection succeeded
        """
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(30)  # 30 second connect timeout
            self.socket.connect((self.host, self.port))
            self.socket.settimeout(None)
            
            logger.info(
                f"Stream {self.stream.stream_id:#x}: Connected to {self.host}:{self.port}"
            )
            return True
            
        except Exception as e:
            logger.error(
                f"Stream {self.stream.stream_id:#x}: Failed to connect to "
                f"{self.host}:{self.port}: {e}"
            )
            # Clean up socket on connection failure to prevent resource leak
            if self.socket:
                try:
                    self.socket.close()
                except Exception:
                    pass
                finally:
                    self.socket = None
            return False
    
    def start_forwarding(self) -> None:
        """Start forwarding data from TCP socket to LoRa stream."""
        self._running = True
        self._recv_thread = threading.Thread(
            target=self._recv_loop,
            daemon=True,
        )
        self._recv_thread.start()
    
    def send(self, data: bytes) -> bool:
        """
        Send data to TCP socket.
        
        Args:
            data: Data to send
            
        Returns:
            True if sent successfully
        """
        if not self.socket:
            return False
        
        try:
            self.socket.sendall(data)
            return True
        except Exception as e:
            logger.error(
                f"Stream {self.stream.stream_id:#x}: Error sending to socket: {e}"
            )
            return False
    
    def _recv_loop(self) -> None:
        """Receive data from TCP and forward to LoRa stream."""
        while self._running and self.socket:
            try:
                self.socket.settimeout(1.0)
                data = self.socket.recv(4096)
                
                if not data:
                    logger.info(
                        f"Stream {self.stream.stream_id:#x}: "
                        f"Remote closed connection"
                    )
                    self.stream.close()
                    break
                
                logger.debug(
                    f"Stream {self.stream.stream_id:#x}: "
                    f"Received {len(data)} bytes from socket"
                )
                self.stream.send(data)
                
            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    logger.error(
                        f"Stream {self.stream.stream_id:#x}: "
                        f"Error receiving from socket: {e}"
                    )
                break
    
    def close(self) -> None:
        """Close the connection."""
        self._running = False
        
        if self.socket:
            try:
                self.socket.close()
            except Exception as e:
                logger.debug(
                    f"Stream {self.stream.stream_id:#x}: "
                    f"Error while closing socket: {e}",
                )
            self.socket = None
        
        logger.debug(
            f"Stream {self.stream.stream_id:#x}: Outbound connection closed"
        )


class GatewayStreamManager:
    """
    Manages streams and outbound connections on the gateway.
    
    Responsibilities:
    - Track active streams from client nodes
    - Handle stream lifecycle (SYN, FIN, RST)
    - Manage outbound TCP connections
    - Forward data between LoRa and Internet
    """
    
    def __init__(
        self,
        config: Config,
        send_callback: Callable[[int, bytes], bool],
    ):
        """
        Initialize gateway stream manager.
        
        Args:
            config: Configuration settings
            send_callback: Function to send data to a node (node_id, data) -> success
        """
        self.config = config
        self._send_callback = send_callback
        
        self._streams: Dict[int, Stream] = {}
        self._connections: Dict[int, OutboundConnection] = {}
        self._lock = threading.RLock()
    
    def _create_stream_send_callback(self, node_id: int) -> Callable[[bytes], bool]:
        """Create a send callback for a stream."""
        def send(data: bytes) -> bool:
            return self._send_callback(node_id, data)
        return send
    
    def handle_frame(self, from_node: int, raw_data: bytes) -> None:
        """
        Handle incoming frame from a client node.
        
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
            
            # Handle new stream (SYN)
            if frame.is_syn() and not stream:
                # Parse connect request from payload
                # Format: "CONNECT host:port"
                try:
                    payload_str = frame.payload.decode("utf-8")
                    if payload_str.startswith("CONNECT "):
                        target = payload_str[8:].strip()
                        host, port_str = target.rsplit(":", 1)
                        port = int(port_str)
                        
                        self._handle_new_stream(
                            stream_id, from_node, host, port, frame
                        )
                    else:
                        logger.warning(
                            f"Invalid SYN payload from {from_node:#x}: {payload_str}"
                        )
                except Exception as e:
                    logger.error(
                        f"Error parsing SYN from {from_node:#x}: {e}"
                    )
                return
            
            if stream:
                # Forward frame to stream
                stream.receive_frame(frame)
                
                # Forward any received data to outbound connection
                connection = self._connections.get(stream_id)
                if connection:
                    data = stream.recv(max_bytes=65536, timeout=0)
                    if data:
                        connection.send(data)
                
                # Handle stream closure
                if stream.state in (StreamState.CLOSED, StreamState.FIN_RECV):
                    self._cleanup_stream(stream_id)
    
    def _handle_new_stream(
        self,
        stream_id: int,
        from_node: int,
        host: str,
        port: int,
        syn_frame: Frame,
    ) -> None:
        """Handle a new incoming stream request."""
        logger.info(
            f"New stream {stream_id:#x} from node {from_node:#x}: "
            f"CONNECT {host}:{port}"
        )
        
        # Create stream
        stream = Stream(
            stream_id=stream_id,
            remote_node_id=from_node,
            config=self.config,
            send_callback=self._create_stream_send_callback(from_node),
        )
        
        # Create outbound connection
        connection = OutboundConnection(stream, host, port)
        
        if connection.connect():
            # Connection successful - accept stream
            self._streams[stream_id] = stream
            self._connections[stream_id] = connection
            
            try:
                # Accept the stream (sends SYN-ACK)
                stream.window.receive_frame(syn_frame)  # Process received SYN
                accept_result = stream.accept()
                if not accept_result:
                    # Treat a False return as a failure to accept the stream
                    raise RuntimeError(
                        f"Stream {stream_id:#x}: accept() returned False"
                    )
                
                # Start forwarding
                connection.start_forwarding()
            
                logger.info(
                    f"Stream {stream_id:#x}: Established tunnel to {host}:{port}"
                )
            except Exception as exc:
                logger.error(
                    f"Stream {stream_id:#x}: Failed to establish tunnel to "
                    f"{host}:{port}: {exc}"
                )
                
                # Notify remote side of failure
                rst_frame = Frame(
                    stream_id=stream_id,
                    seq=0,
                    ack=0,
                    flags=FrameFlags.RST,
                    payload=b"Stream accept/start failed",
                )
                self._send_callback(from_node, encode_frame(rst_frame))
                
                # Reset and clean up partially initialized stream/connection
                try:
                    stream.reset()
                finally:
                    self._cleanup_stream(stream_id)
        else:
            # Connection failed - send RST
            rst_frame = Frame(
                stream_id=stream_id,
                seq=0,
                ack=0,
                flags=FrameFlags.RST,
                payload=b"Connection failed",
            )
            self._send_callback(from_node, encode_frame(rst_frame))
            logger.warning(f"Stream {stream_id:#x}: Connection failed, sent RST")
    
    def _cleanup_stream(self, stream_id: int) -> None:
        """Clean up a closed stream."""
        with self._lock:
            stream = self._streams.pop(stream_id, None)
            connection = self._connections.pop(stream_id, None)
            
            if connection:
                connection.close()
            
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
            for stream_id, stream in list(self._streams.items()):
                # Check retransmits
                if not stream.check_retransmits():
                    logger.warning(f"Stream {stream_id:#x}: Giving up, too many retransmits")
                    stream.reset()
                    self._cleanup_stream(stream_id)
                    continue
                
                # Check timeout
                if stream.is_timed_out():
                    logger.warning(f"Stream {stream_id:#x}: Timed out")
                    stream.reset()
                    self._cleanup_stream(stream_id)
    
    def shutdown(self) -> None:
        """Shutdown all streams and connections."""
        with self._lock:
            for stream_id in list(self._streams.keys()):
                stream = self._streams.get(stream_id)
                if stream:
                    stream.reset()
                self._cleanup_stream(stream_id)
        
        logger.info("Gateway stream manager shut down")
