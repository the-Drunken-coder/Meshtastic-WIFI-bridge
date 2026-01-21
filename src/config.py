from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BridgeConfig:
    mode: str
    gateway_node_id: str
    simulate_radio: bool = False
    timeout: float = 5.0
    spool_path: str | None = None
    metrics_host: str = "0.0.0.0"
    metrics_port: int = 9700
    metrics_enabled: bool = True
