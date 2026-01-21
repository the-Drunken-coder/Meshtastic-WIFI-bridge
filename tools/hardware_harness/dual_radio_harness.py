#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import logging
import os
import signal
import sys
import threading
import time
from typing import Any, Dict, List


def _ensure_package_imports() -> None:
    if __package__:
        return
    tools_dir = os.path.dirname(os.path.abspath(__file__))
    bridge_root = os.path.abspath(os.path.join(tools_dir, "..", ".."))
    if bridge_root not in sys.path:
        sys.path.insert(0, bridge_root)


_ensure_package_imports()

from cli import configure_logging
from client import MeshtasticClient
from transport import MeshtasticTransport

try:
    from .input_utils import prompt_custom_payload, prompt_for_payload, render_menu
    from .setup_utils import build_transport, close_transport, start_gateway
    from .command_presets import (
        COMMAND_PRESETS,
        apply_field_defaults,
        default_context,
        generate_realistic_content,
        update_context_from_payload,
    )
    from .config_utils import (
        TRANSPORT_DEFAULTS,
        load_config,
        parse_args,
        resolve_gateway_node_id,
        resolve_ports,
    )
    from .diagnostics import render_diagnostics
    from .transport_helpers import (
        ack_spool_entry,
        clear_spool,
        retarget_spool_destination,
        wait_for_settled,
    )
except ImportError:
    from input_utils import prompt_custom_payload, prompt_for_payload, render_menu
    from setup_utils import build_transport, close_transport, start_gateway
    from command_presets import (
        COMMAND_PRESETS,
        apply_field_defaults,
        default_context,
        generate_realistic_content,
        update_context_from_payload,
    )
    from config_utils import (
        TRANSPORT_DEFAULTS,
        load_config,
        parse_args,
        resolve_gateway_node_id,
        resolve_ports,
    )
    from diagnostics import render_diagnostics
    from transport_helpers import (
        ack_spool_entry,
        clear_spool,
        retarget_spool_destination,
        wait_for_settled,
    )
def _apply_modem_preset(preset_name: str, gateway_port: str, client_port: str, simulate: bool) -> None:
    """Best-effort apply a Meshtastic modem preset to both radios."""
    if simulate:
        logging.info("Simulation enabled; skipping modem preset change (%s)", preset_name)
        return
    try:
        from meshtastic import config_pb2, serial_interface
    except ImportError as exc:  # pragma: no cover - hardware-only path
        logging.warning("meshtastic not available; cannot set modem preset %s: %s", preset_name, exc)
        return

    preset_map = {
        "LONG_FAST": config_pb2.Config.LoRaConfig.ModemPreset.LONG_FAST,
        "LONG_SLOW": config_pb2.Config.LoRaConfig.ModemPreset.LONG_SLOW,
        "LONG_MODERATE": config_pb2.Config.LoRaConfig.ModemPreset.LONG_MODERATE,
        "VERY_LONG_SLOW": config_pb2.Config.LoRaConfig.ModemPreset.VERY_LONG_SLOW,
        "MEDIUM_FAST": config_pb2.Config.LoRaConfig.ModemPreset.MEDIUM_FAST,
        "MEDIUM_SLOW": config_pb2.Config.LoRaConfig.ModemPreset.MEDIUM_SLOW,
        "SHORT_FAST": config_pb2.Config.LoRaConfig.ModemPreset.SHORT_FAST,
        "SHORT_SLOW": config_pb2.Config.LoRaConfig.ModemPreset.SHORT_SLOW,
        "SHORT_TURBO": config_pb2.Config.LoRaConfig.ModemPreset.SHORT_TURBO,
    }
    preset_value = preset_map.get(preset_name.upper())
    if preset_value is None:
        logging.warning("Unknown modem preset %s; skipping preset change", preset_name)
        return

    for name, port in (("gateway", gateway_port), ("client", client_port)):
        try:
            iface = serial_interface.SerialInterface(port)
            cfg = iface.localNode.localConfig
            cfg.lora.modem_preset = preset_value
            iface.localNode.writeConfig("lora")
            logging.info("Set %s radio (%s) to preset %s", name, port, preset_name)
            iface.close()
            time.sleep(0.5)
        except Exception as exc:  # pragma: no cover - hardware-only path
            logging.warning("Failed to set preset %s on %s (%s): %s", preset_name, name, port, exc)

def interactive_loop(
    client: MeshtasticClient,
    timeout: float,
    retries: int,
    quiet_window: float,
    quiet_timeout: float,
    stop_event: threading.Event,
    loop: bool,
) -> List[Dict[str, Any]]:
    actions = list(COMMAND_PRESETS.keys())
    descriptions = {cmd: meta.get("description", "") for cmd, meta in COMMAND_PRESETS.items()}
    diagnostics: List[Dict[str, Any]] = []
    context = default_context()

    while not stop_event.is_set():
        render_menu(actions, descriptions)
        choice = input("Select an action: ").strip().lower()
        if choice in {"q", "quit", "exit"}:
            stop_event.set()
            break
        # Resolve command and prompt for payload
        if choice == "c":
            command = input("Command name: ").strip()
            if not command:
                continue
            payload = prompt_custom_payload()
        else:
            try:
                index = int(choice) - 1
                command = actions[index]
            except (ValueError, IndexError):
                print("Invalid selection.")
                continue
            fields = COMMAND_PRESETS.get(command, {}).get("fields", [])
            payload = prompt_for_payload(apply_field_defaults(command, fields, context))

        run_start = time.time()
        request_bytes = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        response_bytes = 0
        response_type = None
        error = None
        timed_out = False
        status = "error"
        file_content_b64 = None
        try:
            # Prefer typed helpers when available for clearer field validation
            response = None
            if command == "payload_digest":
                file_path = payload.pop("file_path", "").strip() if payload.get("file_path") else ""
                inline_content = payload.pop("content", "")
                size_kb = payload.pop("size_kb", None)
                content_type = payload.pop("content_type", "") or None
                raw: bytes | None = None

                if file_path:
                    try:
                        with open(file_path, "rb") as fh:
                            raw = fh.read()
                    except OSError as exc:
                        raise RuntimeError(f"Failed to read file {file_path}: {exc}") from exc
                elif inline_content:
                    raw = inline_content.encode("utf-8")
                elif size_kb:
                    kb = max(1, int(size_kb))
                    raw = generate_realistic_content(kb, content_type or "text/plain")
                else:
                    raw = generate_realistic_content(10, content_type or "text/plain")

                if raw is None:
                    raise ValueError("No content prepared for payload_digest")

                file_content_b64 = base64.b64encode(raw).decode("ascii")
                payload["content_b64"] = file_content_b64
                if content_type:
                    payload["content_type"] = content_type
                request_bytes = len(file_content_b64.encode("ascii"))
                response = client.payload_digest(
                    content_b64=file_content_b64,
                    timeout=timeout,
                    max_retries=retries,
                )
            elif hasattr(client, command):
                typed = getattr(client, command)
                if callable(typed):
                    try:
                        response = typed(**payload, timeout=timeout, max_retries=retries)
                    except TypeError:
                        response = typed(**payload)
                else:
                    response = client.send_request(
                        command=command, data=payload, timeout=timeout, max_retries=retries
                    )
            else:
                response = client.send_request(
                    command=command, data=payload, timeout=timeout, max_retries=retries
                )
            if response is not None:
                print("\n--- Response ---")
                print(json.dumps(response.to_dict(), indent=2))
                ack_spool_entry(client.transport, response.id)
                update_context_from_payload(command, payload, context)
                response_type = response.type
                response_bytes = len(
                    json.dumps(response.to_dict(), separators=(",", ":")).encode("utf-8")
                )
                status = "success" if response.type == "response" else "error"
        except TimeoutError as exc:
            timed_out = True
            error = f"{exc.__class__.__name__}: {exc}"
            print(
                f"[ERROR] Request timed out: {exc}. "
                "Potential packet loss or slow link; consider increasing timeout."
            )
        except Exception as exc:
            error = f"{exc.__class__.__name__}: {exc}"
            print(
                f"[ERROR] Request failed ({exc.__class__.__name__}): {exc}. "
                "Check radio connectivity and gateway logs."
            )
        finally:
            duration = time.time() - run_start
            diagnostics.append(
                {
                    "command": command,
                    "status": status,
                    "duration_seconds": duration,
                    "request_bytes": request_bytes,
                    "response_bytes": response_bytes,
                    "timeout_seconds": timeout,
                    "retries": retries,
                    "response_type": response_type,
                    "error": error,
                    "timed_out": timed_out,
                }
            )
            if loop and not stop_event.is_set():
                idle = wait_for_settled(
                    client.transport, quiet_window, quiet_timeout, stop_event
                )
                if not idle:
                    print(
                        "\n[WARN] Radio did not settle within the timeout; shutting down to avoid "
                        "overlapping inputs while messages are still in flight."
                    )
                    stop_event.set()
                    break
        if not loop:
            break
    return diagnostics


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    configure_logging(config.get("log_level", "INFO"))
    logging.info(
        "Resolved mode=%s reliability=%s timeout=%.1fs post_response_timeout=%.1fs retries=%s modem_preset=%s",
        config.get("mode"),
        config.get("reliability_method"),
        float(config.get("timeout", 0)),
        float(config.get("post_response_timeout", 0)),
        config.get("retries"),
        config.get("modem_preset"),
    )
    spool_dir = config.get("spool_dir")

    stop_event = threading.Event()

    def handle_signal(signum: int, _frame: Any) -> None:
        logging.info("Received signal %s, shutting down...", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    gateway_port, client_port = resolve_ports(config)
    logging.info("Using gateway port %s and client port %s", gateway_port, client_port)

    mode_preset = os.getenv("MESHTASTIC_MODE_PRESET") or config.get("modem_preset")
    if mode_preset:
        logging.info("Requested Meshtastic modem preset: %s", mode_preset)
        _apply_modem_preset(mode_preset, gateway_port, client_port, bool(config.get("simulate")))
    else:
        logging.info("Meshtastic modem preset: leave unchanged (no override)")

    gateway_transport = build_transport(
        config.get("simulate", False),
        gateway_port,
        config.get("gateway_node_id", "gateway"),
        spool_dir,
        "gateway",
        chunk_ttl_per_chunk=float(TRANSPORT_DEFAULTS.get("chunk_ttl_per_chunk", 20.0)),
        chunk_ttl_max=float(TRANSPORT_DEFAULTS.get("chunk_ttl_max", 1800.0)),
        chunk_delay_threshold=TRANSPORT_DEFAULTS.get("chunk_delay_threshold"),
        chunk_delay_seconds=float(TRANSPORT_DEFAULTS.get("chunk_delay_seconds", 0.0)),
        nack_max_per_seq=int(TRANSPORT_DEFAULTS.get("nack_max_per_seq", 5)),
        nack_interval=float(TRANSPORT_DEFAULTS.get("nack_interval", 0.5)),
    )
    client_transport = build_transport(
        config.get("simulate", False),
        client_port,
        config.get("client_node_id", "client"),
        spool_dir,
        "client",
        chunk_ttl_per_chunk=float(TRANSPORT_DEFAULTS.get("chunk_ttl_per_chunk", 20.0)),
        chunk_ttl_max=float(TRANSPORT_DEFAULTS.get("chunk_ttl_max", 1800.0)),
        chunk_delay_threshold=TRANSPORT_DEFAULTS.get("chunk_delay_threshold"),
        chunk_delay_seconds=float(TRANSPORT_DEFAULTS.get("chunk_delay_seconds", 0.0)),
        nack_max_per_seq=int(TRANSPORT_DEFAULTS.get("nack_max_per_seq", 5)),
        nack_interval=float(TRANSPORT_DEFAULTS.get("nack_interval", 0.5)),
    )
    if config.get("clear_spool"):
        clear_spool(gateway_transport)
        clear_spool(client_transport)
    gateway_node_id = resolve_gateway_node_id(config, gateway_transport)
    logging.info(
        "Starting harness with gateway port %s and client port %s (gateway node ID: %s)",
        gateway_port,
        client_port,
        gateway_node_id,
    )
    retarget_spool_destination(client_transport, gateway_node_id)

    gateway, gateway_thread = start_gateway(transport=gateway_transport)

    client = MeshtasticClient(client_transport, gateway_node_id=gateway_node_id)

    diagnostics: List[Dict[str, Any]] = []
    try:
        diagnostics = interactive_loop(
            client,
            timeout=float(config.get("timeout", 30.0)),
            retries=int(config.get("retries", 2)),
            quiet_window=float(config.get("post_response_quiet", 10.0)),
            quiet_timeout=float(config.get("post_response_timeout", 90.0)),
            stop_event=stop_event,
            loop=bool(config.get("loop", False)),
        )
    finally:
        stop_event.set()
        gateway.stop()
        gateway_thread.join(timeout=2.0)
        close_transport(client_transport)
        close_transport(gateway_transport)
        render_diagnostics(diagnostics)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
