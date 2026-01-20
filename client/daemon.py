"""Client daemon for LoRa WiFi Forwarder."""

import argparse
import signal
import sys
import time

from transport.meshtastic_transport import MeshtasticTransport, ReceivedMessage
from client.stream_manager import ClientStreamManager
from client.proxy_server import ProxyServer
from reliability.retransmit import RetransmitTimer
from common.config import Config
from common.logging_setup import setup_logging, get_logger
from common.serial_detection import find_serial_port

logger = get_logger(__name__)


class ClientDaemon:
    """
    Client daemon that provides WiFi proxy to LoRa gateway.
    
    Hosts a local HTTP CONNECT proxy that tunnels connections
    through the LoRa mesh to the gateway node.
    """
    
    def __init__(self, config: Config):
        """
        Initialize client daemon.
        
        Args:
            config: Configuration settings
        """
        self.config = config
        self._transport: MeshtasticTransport = None  # type: ignore
        self._stream_manager: ClientStreamManager = None  # type: ignore
        self._proxy_server: ProxyServer = None  # type: ignore
        self._retransmit_timer: RetransmitTimer = None  # type: ignore
        self._running = False
    
    def _send_to_node(self, node_id: int, data: bytes) -> bool:
        """Send data to a specific node."""
        return self._transport.send(node_id, data)
    
    def _on_receive(self, msg: ReceivedMessage) -> None:
        """Handle received message from transport."""
        # Only process messages from the gateway
        if msg.from_id == self.config.gateway_node_id:
            self._stream_manager.handle_frame(msg.from_id, msg.data)
        else:
            logger.debug(
                f"Ignoring message from non-gateway node {msg.from_id:#x}"
            )
    
    def _on_retransmit_check(self) -> None:
        """Periodic retransmit check callback."""
        self._stream_manager.check_streams()
    
    def start(self) -> None:
        """Start the client daemon."""
        logger.info("Starting client daemon...")
        
        if not self.config.gateway_node_id:
            raise ValueError("Gateway node ID must be specified")
        
        # Initialize transport
        self._transport = MeshtasticTransport(self.config.serial_port)
        self._transport.start()
        
        # Initialize stream manager
        self._stream_manager = ClientStreamManager(
            gateway_node_id=self.config.gateway_node_id,
            config=self.config,
            send_callback=self._send_to_node,
        )
        
        # Initialize proxy server
        self._proxy_server = ProxyServer(
            host=self.config.listen_host,
            port=self.config.listen_port,
            stream_manager=self._stream_manager,
        )
        self._proxy_server.start()
        
        # Initialize retransmit timer
        self._retransmit_timer = RetransmitTimer(
            interval_ms=1000,
            callback=self._on_retransmit_check,
        )
        self._retransmit_timer.start()
        
        # Set up message handler
        self._transport.set_receive_callback(self._on_receive)
        
        self._running = True
        
        local_id = self._transport.local_node_id
        if local_id:
            logger.info(f"Client daemon started, local node ID: {local_id:#x}")
        else:
            logger.info("Client daemon started")
        
        logger.info(f"Gateway node ID: {self.config.gateway_node_id:#x}")
        logger.info(
            f"Proxy listening on {self.config.listen_host}:{self.config.listen_port}"
        )
    
    def run(self) -> None:
        """Run the client daemon main loop."""
        try:
            while self._running:
                # Main loop - could do periodic tasks here
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Received interrupt, shutting down...")
    
    def stop(self) -> None:
        """Stop the client daemon."""
        logger.info("Stopping client daemon...")
        
        self._running = False
        
        if self._retransmit_timer:
            self._retransmit_timer.stop()
        
        if self._proxy_server:
            self._proxy_server.stop()
        
        if self._stream_manager:
            self._stream_manager.shutdown()
        
        if self._transport:
            self._transport.stop()
        
        logger.info("Client daemon stopped")


def parse_node_id(value: str) -> int:
    """Parse a node ID from string (supports hex with ! or 0x prefix)."""
    value = value.strip()
    if value.startswith("!"):
        return int(value[1:], 16)
    elif value.startswith("0x") or value.startswith("0X"):
        return int(value, 16)
    else:
        return int(value)


def main() -> None:
    """Main entry point for clientd."""
    parser = argparse.ArgumentParser(
        description="LoRa WiFi Forwarder - Client Daemon",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    
    parser.add_argument(
        "--serial",
        default=None,
        help="Serial port for Meshtastic device (auto-detected if not specified)",
    )
    
    parser.add_argument(
        "--listen",
        default="0.0.0.0:3128",
        help="Address to listen on (host:port)",
    )
    
    parser.add_argument(
        "--gateway-node-id",
        required=True,
        help="Gateway node ID (hex with ! or 0x prefix, or decimal)",
    )
    
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level",
    )
    
    parser.add_argument(
        "--log-file",
        help="Log file path (optional)",
    )
    
    parser.add_argument(
        "--window-size",
        type=int,
        default=4,
        help="Sliding window size",
    )
    
    parser.add_argument(
        "--retransmit-timeout",
        type=int,
        default=5000,
        help="Retransmit timeout in milliseconds",
    )
    
    args = parser.parse_args()
    
    # Parse listen address
    if ":" in args.listen:
        listen_host, listen_port_str = args.listen.rsplit(":", 1)
        listen_port = int(listen_port_str)
    else:
        listen_host = "0.0.0.0"
        listen_port = int(args.listen)
    
    # Parse gateway node ID
    try:
        gateway_node_id = parse_node_id(args.gateway_node_id)
    except ValueError:
        print(f"Invalid gateway node ID: {args.gateway_node_id}")
        sys.exit(1)
    
    # Set up logging
    setup_logging(level=args.log_level, log_file=args.log_file)
    
    # Auto-detect serial port if not specified
    serial_port = find_serial_port(args.serial)
    
    # Create config
    config = Config(
        serial_port=serial_port,
        listen_host=listen_host,
        listen_port=listen_port,
        gateway_node_id=gateway_node_id,
        log_level=args.log_level,
        log_file=args.log_file,
        window_size=args.window_size,
        retransmit_timeout_ms=args.retransmit_timeout,
    )
    
    # Create and run daemon
    daemon = ClientDaemon(config)
    
    # Set up signal handlers
    def signal_handler(sig, frame):
        daemon.stop()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        daemon.start()
        daemon.run()
    except Exception as e:
        logger.error(f"Client daemon error: {e}")
        daemon.stop()
        sys.exit(1)


if __name__ == "__main__":
    main()
