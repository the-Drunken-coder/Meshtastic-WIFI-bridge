"""Meshtastic transport abstraction for LoRa WiFi Forwarder.

This module provides a clean interface for sending and receiving
messages through Meshtastic radios via serial connection.
"""

import threading
import queue
import time
from typing import Callable, Optional, Any
from dataclasses import dataclass

from common.logging_setup import get_logger

logger = get_logger(__name__)


@dataclass
class ReceivedMessage:
    """Represents a message received from Meshtastic."""
    
    from_id: int  # Sender node ID
    to_id: int    # Destination node ID
    data: bytes   # Raw payload data
    timestamp: float  # Unix timestamp when received


class MeshtasticTransport:
    """
    Abstraction layer for Meshtastic radio communication.
    
    This class handles:
    - Serial connection to Meshtastic device
    - Sending messages to specific nodes
    - Receiving messages and queuing them for processing
    """
    
    # Meshtastic private app portnum for custom data
    # Using PRIVATE_APP (256) for our protocol
    PORTNUM = 256
    
    def __init__(self, serial_port: str, modem_preset: Optional[str] = None):
        """
        Initialize the Meshtastic transport.
        
        Args:
            serial_port: Path to serial device (e.g., /dev/ttyUSB0)
        """
        self.serial_port = serial_port
        self._interface: Any = None
        self._receive_queue: queue.Queue[ReceivedMessage] = queue.Queue()
        self._running = False
        self._on_receive_callback: Optional[Callable[[ReceivedMessage], None]] = None
        self._local_node_id: Optional[int] = None
        self._lock = threading.Lock()
        self._modem_preset = modem_preset
    
    def start(self) -> None:
        """
        Start the transport and connect to the Meshtastic device.
        
        Raises:
            ImportError: If meshtastic library is not installed
            Exception: If connection to device fails
        """
        try:
            from meshtastic.serial_interface import SerialInterface
        except ImportError:
            logger.error("meshtastic library not installed. Install with: pip install meshtastic")
            raise
        
        logger.info(f"Connecting to Meshtastic device on {self.serial_port}")
        
        try:
            self._interface = SerialInterface(self.serial_port)

            # Apply modem preset if requested
            if self._modem_preset:
                try:
                    from meshtastic.protobufs import config_pb2

                    preset_value = getattr(
                        config_pb2.Config.LoraConfig.ModemPreset, self._modem_preset, None
                    )
                    if preset_value is not None:
                        self._interface.localConfig.lora.modem_preset = preset_value
                        self._interface.writeConfig("lora")
                        time.sleep(0.1)
                        self._interface.waitForConfig()
                        logger.info(f"Applied modem preset: {self._modem_preset}")
                    else:
                        logger.warning(f"Unknown modem preset '{self._modem_preset}', skipping apply")
                except Exception as e:
                    logger.warning(f"Failed to apply modem preset '{self._modem_preset}': {e}")
            
            # Get local node ID
            if self._interface.myInfo:
                self._local_node_id = self._interface.myInfo.my_node_num
                logger.info(f"Connected to Meshtastic node {self._local_node_id:#x}")
            
            # Subscribe to incoming messages
            from pubsub import pub
            pub.subscribe(self._on_meshtastic_receive, "meshtastic.receive.data")
            
            # Only set _running = True after all initialization is complete
            self._running = True
            
        except Exception as e:
            # Reset state on failure to ensure consistent state
            self._running = False
            self._interface = None
            logger.error(f"Failed to connect to Meshtastic device: {e}")
            raise
    
    def stop(self) -> None:
        """Stop the transport and disconnect from the device."""
        self._running = False
        
        if self._interface:
            try:
                from pubsub import pub
                pub.unsubscribe(self._on_meshtastic_receive, "meshtastic.receive.data")
            except Exception as e:
                # Ignore unsubscribe errors during shutdown, but log for diagnostics
                logger.debug(f"Failed to unsubscribe from Meshtastic pubsub topic during stop: {e}")
            
            try:
                self._interface.close()
            except Exception as e:
                logger.warning(f"Error closing Meshtastic interface: {e}")
            
            self._interface = None
            logger.info("Meshtastic transport stopped")
    
    @property
    def local_node_id(self) -> Optional[int]:
        """Get the local node ID."""
        return self._local_node_id
    
    def send(self, dest_id: int, data: bytes) -> bool:
        """
        Send data to a specific Meshtastic node.
        
        Args:
            dest_id: Destination node ID
            data: Raw bytes to send
            
        Returns:
            True if send was queued successfully, False otherwise
        """
        if not self._interface:
            logger.error("Transport not started")
            return False
        
        with self._lock:
            try:
                logger.debug(
                    f"Sending {len(data)} bytes to node {dest_id:#x}"
                )
                self._interface.sendData(
                    data,
                    destinationId=dest_id,
                    portNum=self.PORTNUM,
                    wantAck=False,  # We handle our own ACKs
                )
                return True
            except Exception as e:
                logger.error(f"Failed to send data: {e}")
                return False
    
    def send_broadcast(self, data: bytes) -> bool:
        """
        Broadcast data to all Meshtastic nodes.
        
        Args:
            data: Raw bytes to send
            
        Returns:
            True if send was queued successfully, False otherwise
        """
        if not self._interface:
            logger.error("Transport not started")
            return False
        
        with self._lock:
            try:
                logger.debug(f"Broadcasting {len(data)} bytes")
                self._interface.sendData(
                    data,
                    portNum=self.PORTNUM,
                    wantAck=False,
                )
                return True
            except Exception as e:
                logger.error(f"Failed to broadcast data: {e}")
                return False
    
    def receive(self, timeout: Optional[float] = None) -> Optional[ReceivedMessage]:
        """
        Receive a message from the queue.
        
        Args:
            timeout: Timeout in seconds, None for blocking, 0 for non-blocking
            
        Returns:
            ReceivedMessage if available, None on timeout
        """
        try:
            return self._receive_queue.get(timeout=timeout)
        except queue.Empty:
            return None
    
    def set_receive_callback(
        self, callback: Callable[[ReceivedMessage], None]
    ) -> None:
        """
        Set a callback for incoming messages.
        
        The callback will be invoked on a background thread.
        
        Args:
            callback: Function to call with ReceivedMessage
        """
        self._on_receive_callback = callback
    
    def _on_meshtastic_receive(self, packet: dict, interface: Any) -> None:
        """Handle incoming Meshtastic packet."""
        try:
            # Only process data packets for our portnum
            if packet.get("decoded", {}).get("portnum") != "PRIVATE_APP":
                return
            
            from_id = packet.get("fromId") or packet.get("from", 0)
            to_id = packet.get("toId") or packet.get("to", 0)
            
            # Convert string IDs to int if needed
            if isinstance(from_id, str) and from_id.startswith("!"):
                from_id = int(from_id[1:], 16)
            if isinstance(to_id, str) and to_id.startswith("!"):
                to_id = int(to_id[1:], 16)
            
            payload = packet.get("decoded", {}).get("payload", b"")
            if isinstance(payload, str):
                # Meshtastic may return payload as ISO-8859-1 encoded string
                # for binary data. Use latin-1 which maps bytes 0-255 directly.
                payload = payload.encode("latin-1")
            
            msg = ReceivedMessage(
                from_id=from_id,
                to_id=to_id,
                data=payload,
                timestamp=time.time(),
            )
            
            logger.debug(
                f"Received {len(payload)} bytes from node {from_id:#x}"
            )
            
            # Queue for polling
            self._receive_queue.put(msg)
            
            # Call callback if set
            if self._on_receive_callback:
                try:
                    self._on_receive_callback(msg)
                except Exception as e:
                    logger.error(f"Error in receive callback: {e}")
                    
        except Exception as e:
            logger.error(f"Error processing received packet: {e}")
