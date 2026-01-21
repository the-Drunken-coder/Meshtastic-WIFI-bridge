"""Command-line entrypoint for the Meshtastic bridge."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve()
while ROOT != ROOT.parent and not (ROOT / "src").exists():
    ROOT = ROOT.parent
SRC = ROOT / "src"
if SRC.exists() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from client import MeshtasticClient
from config import BridgeConfig
from gateway import MeshtasticGateway
from logging_utils import configure_logging
from metrics import get_metrics_registry, start_metrics_http_server
from radio import build_radio
from transport import MeshtasticTransport

LOGGER = logging.getLogger(__name__)


def parse_args() -> BridgeConfig:
    parser = argparse.ArgumentParser(description="Meshtastic bridge")
    parser.add_argument("--mode", choices=["gateway", "client"], required=True)
    parser.add_argument("--gateway-node-id", required=True)
    parser.add_argument("--simulate-radio", action="store_true", help="Use in-memory radio")
    parser.add_argument("--timeout", type=float, default=5.0, help="Client request timeout seconds")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--command", help="Client mode command")
    parser.add_argument("--data", default="{}")
    parser.add_argument(
        "--spool-path",
        default=os.path.expanduser("~/.meshtastic_bridge_spool.json"),
        help="Path for persistent outgoing message spool",
    )
    parser.add_argument("--radio-port", help="Serial port for Meshtastic")
    parser.add_argument("--node-id", help="Meshtastic node identifier for this machine")
    parser.add_argument(
        "--metrics-host",
        default=os.getenv("MESHTASTIC_METRICS_HOST", "0.0.0.0"),
        help="Host interface for metrics/health server",
    )
    parser.add_argument(
        "--metrics-port",
        type=int,
        default=int(os.getenv("MESHTASTIC_METRICS_PORT", "9700")),
        help="Port for metrics/health server",
    )
    parser.add_argument(
        "--disable-metrics",
        action="store_true",
        help="Disable metrics and health endpoints",
    )
    args = parser.parse_args()
    configure_logging(args.log_level)
    metrics_enabled_env = os.getenv("MESHTASTIC_METRICS_ENABLED")
    metrics_enabled = (
        metrics_enabled_env.lower() not in {"0", "false", "no"}
        if metrics_enabled_env is not None
        else not args.disable_metrics
    )
    config = BridgeConfig(
        mode=args.mode,
        gateway_node_id=args.gateway_node_id,
        simulate_radio=args.simulate_radio,
        timeout=args.timeout,
        spool_path=args.spool_path,
        metrics_host=args.metrics_host,
        metrics_port=args.metrics_port,
        metrics_enabled=metrics_enabled,
    )
    config._command = args.command  # type: ignore[attr-defined]
    config._data = args.data
    config._radio_port = args.radio_port
    config._node_id = args.node_id or ("gateway" if args.mode == "gateway" else "client")
    return config


def run_gateway(config: BridgeConfig, transport: MeshtasticTransport) -> None:
    gateway = MeshtasticGateway(transport)
    LOGGER.info("Starting Meshtastic gateway mode")
    try:
        gateway.run_forever()
    except KeyboardInterrupt:
        LOGGER.info("Gateway stopping")
        gateway.stop()


def run_client(config: BridgeConfig, transport: MeshtasticTransport) -> None:
    command = getattr(config, "_command", None)
    if not command:
        raise RuntimeError("Client mode requires --command")
    data_str = getattr(config, "_data", "{}")
    payload: dict[str, Any] = json.loads(data_str)
    client = MeshtasticClient(transport, config.gateway_node_id)
    response = client.send_request(command, payload, timeout=config.timeout)
    print(json.dumps(response.to_dict(), indent=2))


def start_observability_server(
    config: BridgeConfig, transport: MeshtasticTransport
) -> Optional[ThreadingHTTPServer]:
    if not config.metrics_enabled:
        LOGGER.info("Metrics and health endpoints disabled")
        return None

    registry = get_metrics_registry()

    def readiness() -> bool:
        try:
            if transport.spool:
                return transport.spool.depth() < 1000
        except Exception:
            return False
        return True

    def status() -> Dict[str, object]:
        details: Dict[str, object] = {}
        try:
            details["spool_depth"] = transport.spool.depth() if transport.spool else 0
        except Exception:
            details["spool_depth"] = -1
        try:
            details["dedupe"] = transport.deduper.stats()
        except Exception:
            details["dedupe"] = {}
        return details

    try:
        server = start_metrics_http_server(
            config.metrics_host,
            config.metrics_port,
            registry=registry,
            readiness_fn=readiness,
            status_fn=status,
        )
        LOGGER.info(
            "Metrics server listening on %s:%s (/metrics, /health, /ready, /status)",
            config.metrics_host,
            config.metrics_port,
        )
        return server
    except OSError as exc:
        LOGGER.warning("Failed to start metrics server: %s", exc)
        return None


def main() -> None:
    config = parse_args()
    radio = build_radio(
        config.simulate_radio,
        getattr(config, "_radio_port", None),
        getattr(config, "_node_id", None),
    )
    transport = MeshtasticTransport(radio, spool_path=config.spool_path)
    metrics_server = start_observability_server(config, transport)
    try:
        if config.mode == "gateway":
            run_gateway(config, transport)
        else:
            run_client(config, transport)
    finally:
        if metrics_server:
            metrics_server.shutdown()
            metrics_server.server_close()


if __name__ == "__main__":
    main()
