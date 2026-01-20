"""HTTP CONNECT proxy server for client node."""

import socket
import threading
import time
from typing import Optional

from client.stream_manager import ClientStreamManager
from reliability.stream import Stream, StreamState
from common.logging_setup import get_logger

logger = get_logger(__name__)


class ProxyConnection:
    """Handles a single proxy connection from a local client."""
    
    def __init__(
        self,
        client_socket: socket.socket,
        client_addr: tuple,
        stream_manager: ClientStreamManager,
    ):
        """
        Initialize proxy connection.
        
        Args:
            client_socket: Socket from accepted client
            client_addr: Client address tuple
            stream_manager: Client stream manager for creating streams
        """
        self.client_socket = client_socket
        self.client_addr = client_addr
        self.stream_manager = stream_manager
        self.stream: Optional[Stream] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
    
    def start(self) -> None:
        """Start handling the proxy connection."""
        self._running = True
        self._thread = threading.Thread(target=self._handle, daemon=True)
        self._thread.start()
    
    def _handle(self) -> None:
        """Main handler for the proxy connection."""
        try:
            self.client_socket.settimeout(30)
            
            # Read HTTP CONNECT request
            request = self._read_http_request()
            if not request:
                logger.warning(f"No request from {self.client_addr}")
                return
            
            # Parse CONNECT request
            host, port = self._parse_connect_request(request)
            if not host or not port:
                self._send_http_error(400, "Bad Request")
                return
            
            logger.info(f"CONNECT request: {host}:{port} from {self.client_addr}")
            
            # Create LoRa stream to gateway
            self.stream = self.stream_manager.create_stream(host, port)
            if not self.stream:
                self._send_http_error(502, "Bad Gateway - Failed to create stream")
                return
            
            # Wait for stream to be established
            if not self._wait_for_stream_open(timeout=30):
                if self.stream.state == StreamState.CLOSED:
                    self._send_http_error(502, "Bad Gateway - Connection refused")
                else:
                    self._send_http_error(504, "Gateway Timeout")
                return
            
            # Send success response
            self._send_http_response(200, "Connection Established")
            logger.info(f"Tunnel established: {host}:{port}")
            
            # Start bidirectional forwarding
            self._forward_data()
            
        except Exception as e:
            logger.error(f"Proxy connection error: {e}")
        finally:
            self._cleanup()
    
    def _read_http_request(self) -> Optional[str]:
        """Read HTTP request line and headers."""
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = self.client_socket.recv(4096)
                if not chunk:
                    return None
                data += chunk
                if len(data) > 8192:  # Limit header size
                    return None
            return data.decode("utf-8", errors="replace")
        except Exception as e:
            logger.error(f"Error reading HTTP request: {e}")
            return None
    
    def _parse_connect_request(self, request: str) -> tuple:
        """
        Parse HTTP CONNECT request.
        
        Returns:
            Tuple of (host, port) or (None, None) on error
        """
        try:
            lines = request.split("\r\n")
            first_line = lines[0]
            parts = first_line.split()
            
            if len(parts) < 2 or parts[0].upper() != "CONNECT":
                return None, None
            
            target = parts[1]
            
            # Parse host:port
            if ":" in target:
                host, port_str = target.rsplit(":", 1)
                try:
                    port = int(port_str)
                except ValueError:
                    logger.warning(f"Invalid port in CONNECT request: {port_str!r}")
                    return None, None
            else:
                host = target
                port = 443  # Default HTTPS

            # Basic validation of host and port
            if not host:
                logger.warning("Empty host in CONNECT request")
                return None, None

            if not (1 <= port <= 65535):
                logger.warning(f"Port out of valid range in CONNECT request: {port}")
                return None, None
            
            return host, port
            
        except Exception as e:
            logger.error(f"Error parsing CONNECT request: {e}")
            return None, None
    
    def _wait_for_stream_open(self, timeout: float) -> bool:
        """Wait for stream to be established."""
        deadline = time.time() + timeout
        
        while time.time() < deadline:
            if self.stream.state == StreamState.OPEN:
                return True
            if self.stream.state == StreamState.CLOSED:
                return False
            time.sleep(0.1)
        
        return False
    
    def _send_http_response(self, code: int, message: str) -> None:
        """Send HTTP response."""
        response = f"HTTP/1.1 {code} {message}\r\n\r\n"
        try:
            self.client_socket.sendall(response.encode("utf-8"))
        except Exception as e:
            logger.error(f"Error sending HTTP response: {e}")
    
    def _send_http_error(self, code: int, message: str) -> None:
        """Send HTTP error response."""
        self._send_http_response(code, message)
    
    def _forward_data(self) -> None:
        """Forward data bidirectionally between client and stream."""
        self.client_socket.setblocking(False)
        
        # Use a simple polling loop
        while self._running and self.stream.state == StreamState.OPEN:
            # Check for data from client
            try:
                data = self.client_socket.recv(4096)
                if data:
                    logger.debug(f"Client -> LoRa: {len(data)} bytes")
                    self.stream.send(data)
                else:  # Empty bytes (recv returned b"") means connection closed
                    logger.info("Client closed connection")
                    break
            except BlockingIOError:
                # Non-blocking socket: no data available right now, continue polling
                pass
            except Exception as e:
                logger.error(f"Error receiving from client: {e}")
                break
            
            # Check for data from stream
            try:
                data = self.stream.recv(max_bytes=4096, timeout=0.1)
                if data:
                    logger.debug(f"LoRa -> Client: {len(data)} bytes")
                    self.client_socket.sendall(data)
            except Exception as e:
                logger.error(f"Error sending to client: {e}")
                break
        
        logger.info("Tunnel closed")
    
    def _cleanup(self) -> None:
        """Clean up resources."""
        self._running = False
        
        try:
            self.client_socket.close()
        except OSError as e:
            # Ignore socket close errors during cleanup, but log for diagnostics
            logger.debug(f"Error closing client socket {self.client_addr}: {e}")
        
        if self.stream:
            self.stream.close()
    
    def stop(self) -> None:
        """Stop the connection handler."""
        self._running = False


class ProxyServer:
    """
    HTTP CONNECT proxy server.
    
    Accepts local connections and tunnels them over LoRa.
    """
    
    def __init__(
        self,
        host: str,
        port: int,
        stream_manager: ClientStreamManager,
    ):
        """
        Initialize proxy server.
        
        Args:
            host: Host to listen on
            port: Port to listen on
            stream_manager: Client stream manager
        """
        self.host = host
        self.port = port
        self.stream_manager = stream_manager
        self._server_socket: Optional[socket.socket] = None
        self._running = False
        self._connections: list = []
        self._thread: Optional[threading.Thread] = None
    
    def start(self) -> None:
        """Start the proxy server."""
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind((self.host, self.port))
        self._server_socket.listen(10)
        self._server_socket.settimeout(1.0)
        
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        
        logger.info(f"Proxy server listening on {self.host}:{self.port}")
    
    def _accept_loop(self) -> None:
        """Accept incoming connections."""
        while self._running:
            try:
                client_socket, client_addr = self._server_socket.accept()
                logger.info(f"Accepted connection from {client_addr}")
                
                connection = ProxyConnection(
                    client_socket=client_socket,
                    client_addr=client_addr,
                    stream_manager=self.stream_manager,
                )
                self._connections.append(connection)
                connection.start()
                
            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    logger.error(f"Error accepting connection: {e}")
    
    def stop(self) -> None:
        """Stop the proxy server."""
        self._running = False
        
        for conn in self._connections:
            conn.stop()
        
        if self._server_socket:
            try:
                self._server_socket.close()
            except Exception as e:
                logger.debug(f"Error while closing server socket during stop: {e}")
        
        if self._thread:
            self._thread.join(timeout=2.0)
        
        logger.info("Proxy server stopped")
