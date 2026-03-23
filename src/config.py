from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

__all__ = ["BridgeConfig"]


@dataclass
class BridgeConfig:
    """Configuration for the Meshtastic bridge.

    Attributes:
        mode: Operation mode ('gateway' or 'client').
        gateway_node_id: Meshtastic node ID of the gateway.
        simulate_radio: Use in-memory radio instead of hardware.
        timeout: Client request timeout in seconds.
        spool_path: Path for persistent outgoing message spool.
        metrics_host: Host interface for metrics/health server.
            Default '0.0.0.0' binds to all interfaces. Use '127.0.0.1'
            to restrict access to localhost only.
        metrics_port: Port for metrics/health server.
        metrics_enabled: Enable metrics and health endpoints.
        command: Client mode command to execute (client mode only).
        data: JSON-encoded data string for the client command.
        radio_port: Serial port for the Meshtastic radio hardware.
        node_id: Meshtastic node identifier for this machine.
        web_browser: Start the web browser UI (client mode only).
        web_host: Host interface for the web browser UI.
        web_port: Port for the web browser UI.
    """

    mode: str
    gateway_node_id: str
    simulate_radio: bool = False
    timeout: float = 5.0
    spool_path: Optional[str] = None
    metrics_host: str = "0.0.0.0"
    metrics_port: int = 9700
    metrics_enabled: bool = True
    # Client / runtime fields
    command: Optional[str] = None
    data: str = "{}"
    radio_port: Optional[str] = None
    node_id: Optional[str] = None
    web_browser: bool = False
    web_host: str = "127.0.0.1"
    web_port: int = 8080
