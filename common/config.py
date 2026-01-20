"""Configuration management for LoRa WiFi Forwarder."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class Config:
    """Configuration settings for the LoRa WiFi Forwarder."""
    
    # Serial port for Meshtastic radio
    serial_port: str = "/dev/ttyUSB0"
    
    # Default chunk payload size (conservative for LoRa)
    # Meshtastic has ~237 byte payload limit, we use less for overhead
    chunk_payload_size: int = 180
    
    # Reliability settings
    window_size: int = 4  # Sliding window size
    retransmit_timeout_ms: int = 5000  # Retransmit after 5 seconds
    max_retransmits: int = 5  # Max retransmit attempts before giving up
    stream_timeout_s: int = 120  # Stream timeout in seconds
    
    # Client settings
    listen_host: str = "0.0.0.0"
    listen_port: int = 3128
    gateway_node_id: Optional[int] = None
    
    # Gateway settings
    internet_iface: str = "wlan0"
    
    # Logging
    log_level: str = "INFO"
    log_file: Optional[str] = None

    # Radio modem preset (Meshtastic preset enum name)
    modem_preset: Optional[str] = None


# Default configuration instance
default_config = Config()
