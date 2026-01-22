from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve()
while ROOT != ROOT.parent and not (ROOT / "src").exists():
    ROOT = ROOT.parent
SRC = ROOT / "src"
if SRC.exists() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from transport import MeshtasticTransport

DEFAULT_CONFIG: Dict[str, Any] = {
    "gateway_port": None,
    "client_port": None,
    "gateway_node_id": "gateway",
    "client_node_id": "client",
    "mode": "general",
    "reliability_method": None,
    "modem_preset": None,
    "timeout": 90.0,
    "retries": 2,
    "log_level": "INFO",
    "log_file": None,
    "simulate": False,
    "disable_dedupe": False,
    "dedupe_lease_seconds": 8.0,
    "spool_dir": os.path.expanduser("~/.meshtastic_bridge_harness"),
    "post_response_quiet": 10.0,
    "post_response_timeout": 150.0,
    "loop": False,
    "clear_spool": False,
    "transport_overrides": {},
}

# Transport defaults come only from the selected mode profile.
BASE_TRANSPORT_DEFAULTS: Dict[str, Any] = {}
TRANSPORT_DEFAULTS: Dict[str, Any] = dict(BASE_TRANSPORT_DEFAULTS)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Meshtastic hardware harness (config-driven)")
    parser.add_argument(
        "--config",
        default=os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "config.json",
        ),
        help="Path to JSON config file (defaults to tools/hardware_harness/config.json)",
    )
    return parser.parse_args()


def load_config(path: str, mode_override: Optional[str] = None) -> Dict[str, Any]:
    config_path = os.path.expanduser(path)
    config_dir = os.path.dirname(config_path) or "."
    os.makedirs(config_dir, exist_ok=True)

    config: Dict[str, Any] = dict(DEFAULT_CONFIG)
    user_keys: set[str] = set()
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                config.update(loaded)
                user_keys = set(loaded.keys())
        except (json.JSONDecodeError, OSError, PermissionError) as exc:
            logging.warning("Failed to read config at %s (%s); using defaults", config_path, exc)
    else:
        try:
            with open(config_path, "w", encoding="utf-8") as handle:
                json.dump(config, handle, indent=2)
            logging.info("Wrote default config to %s", config_path)
        except (OSError, PermissionError) as exc:
            logging.warning("Could not write default config to %s (%s)", config_path, exc)

    if mode_override is not None:
        config["mode"] = mode_override

    config["spool_dir"] = os.path.expanduser(config.get("spool_dir", DEFAULT_CONFIG["spool_dir"]))

    # Reset transport defaults each load to avoid cross-run leakage.
    TRANSPORT_DEFAULTS.clear()
    TRANSPORT_DEFAULTS.update(BASE_TRANSPORT_DEFAULTS)

    # Apply mode defaults (best-effort; user overrides win).
    raw_mode = config.get("mode")
    mode_name = (raw_mode or "").strip() if isinstance(raw_mode, str) or raw_mode is None else raw_mode
    apply_mode = mode_name not in {"", "none", "null", None}

    profile: Dict[str, Any] = {}
    mode_path: Optional[Path] = None
    if apply_mode:
        try:
            mode_path = ROOT / "modes" / f"{mode_name}.json"
            with mode_path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                profile = loaded
            else:
                raise ValueError("Mode file did not contain an object")
        except Exception as exc:
            logging.warning("Failed to load mode '%s' (%s); using built-in defaults", mode_name, exc)
            profile = {}
            mode_path = None
    config["_mode_path"] = str(mode_path) if mode_path else None

    # Config-level keys (mode is authoritative for these)
    for key in (
        "reliability_method",
        "modem_preset",
        "timeout",
        "retries",
        "post_response_timeout",
        "post_response_quiet",
    ):
        if key in profile:
            config[key] = profile[key]

    # Transport defaults
    transport_overrides = profile.get("transport", {}) if isinstance(profile, dict) else {}
    if isinstance(transport_overrides, dict):
        TRANSPORT_DEFAULTS.update(transport_overrides)
    return config


def discover_ports() -> List[str]:
    try:
        from meshtastic import util as meshtastic_util

        ports = meshtastic_util.findPorts()
        if ports:
            return _normalize_ports(ports)
    except Exception as exc:
        logging.debug("Meshtastic-based port discovery failed; falling back to serial ports: %s", exc)
    try:
        from serial.tools import list_ports

        return [port.device for port in list_ports.comports()]
    except Exception:
        return []


def _normalize_ports(ports: List[Any]) -> List[str]:
    normalized: List[str] = []
    for port in ports:
        if isinstance(port, str):
            normalized.append(port)
        elif isinstance(port, dict) and "device" in port:
            normalized.append(str(port["device"]))
        elif hasattr(port, "device"):
            normalized.append(str(port.device))
    return normalized


def resolve_ports(config: Dict[str, Any]) -> tuple[str, str]:
    if config.get("simulate"):
        return ("simulate-gateway", "simulate-client")

    gateway_port = config.get("gateway_port")
    client_port = config.get("client_port")

    if gateway_port and client_port:
        return (gateway_port, client_port)

    ports = discover_ports()
    if not ports:
        raise RuntimeError(
            "No serial ports detected. Plug in two Meshtastic radios or pass "
            "--gateway-port/--client-port explicitly."
        )

    if gateway_port and not client_port:
        remaining = [port for port in ports if port != gateway_port]
        if not remaining:
            raise RuntimeError(
                f"Only found {ports}; unable to auto-select a client port distinct from {gateway_port}."
            )
        return (gateway_port, remaining[0])

    if client_port and not gateway_port:
        remaining = [port for port in ports if port != client_port]
        if not remaining:
            raise RuntimeError(
                f"Only found {ports}; unable to auto-select a gateway port distinct from {client_port}."
            )
        return (remaining[0], client_port)

    if len(ports) < 2:
        raise RuntimeError(
            f"Found {ports}; need two radios to run the dual harness. "
            "Specify ports explicitly or connect a second radio."
        )
    return (ports[0], ports[1])


def resolve_gateway_node_id(
    config: Dict[str, Any], gateway_transport: MeshtasticTransport
) -> str:
    gateway_node_id = config.get("gateway_node_id") or "gateway"
    if gateway_node_id not in {"gateway", "client"}:
        return gateway_node_id
    if config.get("simulate"):
        return gateway_node_id or "gateway"

    radio = gateway_transport.radio
    if hasattr(radio, "node_id"):
        return str(getattr(radio, "node_id"))

    interface = getattr(radio, "_interface", None)
    if interface and hasattr(interface, "getMyNodeInfo"):
        try:
            info = interface.getMyNodeInfo()
        except Exception:
            info = None
        user_id = _extract_user_id(info)
        if user_id:
            return user_id

    return gateway_node_id or "gateway"


def _extract_user_id(info: object) -> Optional[str]:
    if isinstance(info, dict):
        user = info.get("user")
        if isinstance(user, dict):
            user_id = user.get("id")
            if user_id:
                return str(user_id)
    if hasattr(info, "user"):
        user = getattr(info, "user")
        if hasattr(user, "id"):
            user_id = getattr(user, "id")
            if user_id:
                return str(user_id)
    return None


__all__ = [
    "DEFAULT_CONFIG",
    "TRANSPORT_DEFAULTS",
    "load_config",
    "parse_args",
    "resolve_gateway_node_id",
    "resolve_ports",
]
