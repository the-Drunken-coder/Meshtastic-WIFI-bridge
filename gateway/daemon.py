"""Gateway daemon for LoRa WiFi Forwarder."""

import argparse
import signal
import sys
import time

from transport.meshtastic_transport import MeshtasticTransport, ReceivedMessage
from gateway.stream_manager import GatewayStreamManager
from reliability.retransmit import RetransmitTimer
from common.config import Config
from common.logging_setup import setup_logging, get_logger

logger = get_logger(__name__)


class GatewayDaemon:
    """
    Gateway daemon that bridges LoRa streams to the Internet.
    
    Receives stream requests from client nodes, establishes
    outbound TCP connections, and forwards data bidirectionally.
    """
    
    def __init__(self, config: Config):
        """
        Initialize gateway daemon.
        
        Args:
            config: Configuration settings
        """
        self.config = config
        self._transport: MeshtasticTransport = None  # type: ignore
        self._stream_manager: GatewayStreamManager = None  # type: ignore
        self._retransmit_timer: RetransmitTimer = None  # type: ignore
        self._running = False
    
    def _send_to_node(self, node_id: int, data: bytes) -> bool:
        """Send data to a specific node."""
        return self._transport.send(node_id, data)
    
    def _on_receive(self, msg: ReceivedMessage) -> None:
        """Handle received message from transport."""
        self._stream_manager.handle_frame(msg.from_id, msg.data)
    
    def _on_retransmit_check(self) -> None:
        """Periodic retransmit check callback."""
        self._stream_manager.check_streams()
    
    def start(self) -> None:
        """Start the gateway daemon."""
        logger.info("Starting gateway daemon...")
        
        # Initialize transport
        self._transport = MeshtasticTransport(self.config.serial_port)
        self._transport.start()
        
        # Initialize stream manager
        self._stream_manager = GatewayStreamManager(
            config=self.config,
            send_callback=self._send_to_node,
        )
        
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
            logger.info(f"Gateway daemon started, node ID: {local_id:#x}")
        else:
            logger.info("Gateway daemon started")
        
        logger.info(f"Internet interface: {self.config.internet_iface}")
    
    def run(self) -> None:
        """Run the gateway daemon main loop."""
        try:
            while self._running:
                # Main loop - could do periodic tasks here
                # Most work is done in callbacks
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Received interrupt, shutting down...")
    
    def stop(self) -> None:
        """Stop the gateway daemon."""
        logger.info("Stopping gateway daemon...")
        
        self._running = False
        
        if self._retransmit_timer:
            self._retransmit_timer.stop()
        
        if self._stream_manager:
            self._stream_manager.shutdown()
        
        if self._transport:
            self._transport.stop()
        
        logger.info("Gateway daemon stopped")


def main() -> None:
    """Main entry point for gatewayd."""
    parser = argparse.ArgumentParser(
        description="LoRa WiFi Forwarder - Gateway Daemon",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    
    parser.add_argument(
        "--serial",
        default="/dev/ttyUSB0",
        help="Serial port for Meshtastic device",
    )
    
    parser.add_argument(
        "--internet-iface",
        default="wlan0",
        help="Network interface with Internet connectivity",
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
    
    # Set up logging
    setup_logging(level=args.log_level, log_file=args.log_file)
    
    # Create config
    config = Config(
        serial_port=args.serial,
        internet_iface=args.internet_iface,
        log_level=args.log_level,
        log_file=args.log_file,
        window_size=args.window_size,
        retransmit_timeout_ms=args.retransmit_timeout,
    )
    
    # Create and run daemon
    daemon = GatewayDaemon(config)
    
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
        logger.error(f"Gateway daemon error: {e}")
        daemon.stop()
        sys.exit(1)


if __name__ == "__main__":
    main()
