#!/usr/bin/env python3
"""
Test runner that wraps the dual radio harness to run multiple test scenarios
with different configurations and produce a results file.

Usage:
    python tools/scenario_runner/runner.py
"""
from __future__ import annotations

import base64
import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


def _ensure_imports() -> None:
    """Ensure src and hardware_harness are importable."""
    root = Path(__file__).resolve()
    while root != root.parent and not (root / "src").exists():
        root = root.parent
    
    src = root / "src"
    if src.exists() and str(src) not in sys.path:
        sys.path.insert(0, str(src))
    
    harness = root / "tools" / "hardware_harness"
    if harness.exists() and str(harness) not in sys.path:
        sys.path.insert(0, str(harness))


_ensure_imports()

from client import MeshtasticClient
from logging_utils import configure_logging

from setup_utils import build_transport, close_transport, start_gateway
from config_utils import (
    TRANSPORT_DEFAULTS,
    load_config,
    resolve_gateway_node_id,
    resolve_ports,
)
from transport_helpers import (
    ack_spool_entry,
    clear_spool,
    retarget_spool_destination,
    wait_for_settled,
)


# Path to hardware_harness config
HARNESS_CONFIG_PATH = Path(__file__).resolve().parent.parent / "hardware_harness" / "config.json"


@dataclass
class TestResult:
    """Result from a single test scenario run."""
    scenario_name: str
    description: str
    overrides: Dict[str, Any]
    command: str
    payload: Dict[str, Any]
    status: str  # "success", "error", "timeout"
    duration_seconds: float
    request_bytes: int
    response_bytes: int
    chunks_sent: int
    throughput_kbps: float
    error: Optional[str] = None
    response_data: Optional[Dict[str, Any]] = None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class TestScenario:
    """Configuration for a test scenario."""
    name: str
    description: str
    overrides: Dict[str, Any]
    payload_overrides: Dict[str, Any]


def load_test_scenarios(path: str) -> tuple[List[TestScenario], str, Dict[str, Any]]:
    """Load test scenarios from JSON file."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    scenarios = []
    for s in data.get("scenarios", []):
        scenarios.append(TestScenario(
            name=s["name"],
            description=s.get("description", ""),
            overrides=s.get("overrides", {}),
            payload_overrides=s.get("payload_overrides", {}),
        ))
    
    default_command = data.get("default_command", "http_request")
    default_payload = data.get("default_payload", {"url": "https://example.com", "method": "GET"})
    
    return scenarios, default_command, default_payload


def apply_overrides(base_config: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
    """
    Apply scenario overrides to the base config and global transport defaults.

    Non-transport keys are applied directly to a copy of base_config, and the
    resulting dictionary is returned. If the "transport" key is present and is
    a dict, its entries are merged into the global TRANSPORT_DEFAULTS and are
    not added directly to the returned config.
    """
    config = dict(base_config)
    
    for key, value in overrides.items():
        if key == "transport" and isinstance(value, dict):
            # Merge transport overrides into TRANSPORT_DEFAULTS
            for tk, tv in value.items():
                TRANSPORT_DEFAULTS[tk] = tv
        else:
            config[key] = value
    
    return config


def _apply_modem_preset(preset_name: str, gateway_port: str, client_port: str, simulate: bool) -> None:
    """Best-effort apply a Meshtastic modem preset to both radios."""
    if simulate:
        logging.info("Simulation enabled; skipping modem preset change (%s)", preset_name)
        return
    try:
        from meshtastic import config_pb2, serial_interface
    except ImportError as exc:
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
        except Exception as exc:
            logging.warning("Failed to set preset %s on %s (%s): %s", preset_name, name, port, exc)


def run_single_test(
    client: MeshtasticClient,
    command: str,
    payload: Dict[str, Any],
    timeout: float,
    retries: int,
) -> tuple[str, float, int, int, Optional[str], Optional[Dict[str, Any]], Optional[str]]:
    """
    Run a single test command and return results.
    Returns: (status, duration, request_bytes, response_bytes, error, response_data, response_id)
    """
    run_start = time.time()
    request_bytes = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    response_bytes = 0
    error = None
    status = "error"
    response_data = None
    response_id = None

    try:
        if command == "http_request":
            clean_payload = dict(payload)
            if clean_payload.get("headers") in {"", None}:
                clean_payload.pop("headers", None)
            if clean_payload.get("body") in {"", None}:
                clean_payload.pop("body", None)
            response = client.http_request(
                timeout=timeout,
                max_retries=retries,
                **clean_payload,
            )
        elif hasattr(client, command):
            method = getattr(client, command)
            if callable(method):
                # Try to pass timeout and max_retries; fall back if not supported
                try:
                    response = method(**payload, timeout=timeout, max_retries=retries)
                except TypeError:
                    response = method(**payload)
            else:
                response = client.send_request(
                    command=command, data=payload, timeout=timeout, max_retries=retries
                )
        else:
            response = client.send_request(
                command=command, data=payload, timeout=timeout, max_retries=retries
            )

        if response is not None:
            response_data = response.to_dict()
            response_id = response_data.get("id")
            response_bytes = len(json.dumps(response_data, separators=(",", ":")).encode("utf-8"))
            ack_spool_entry(client.transport, response.id)
            status = "success" if response.type == "response" else "error"

    except TimeoutError as exc:
        status = "timeout"
        error = str(exc)
    except Exception as exc:
        status = "error"
        error = f"{exc.__class__.__name__}: {exc}"

    duration = time.time() - run_start
    return status, duration, request_bytes, response_bytes, error, response_data, response_id


def run_scenario(
    scenario: TestScenario,
    base_config: Dict[str, Any],
    command: str,
    payload: Dict[str, Any],
    stop_event: threading.Event,
) -> TestResult:
    """Run a single test scenario with its specific configuration."""
    print(f"\n{'='*60}")
    print(f"Running scenario: {scenario.name}")
    print(f"Description: {scenario.description}")
    print(f"Overrides: {json.dumps(scenario.overrides, indent=2)}")
    print(f"{'='*60}\n")

    # Reset transport defaults to prevent cross-scenario state leakage
    TRANSPORT_DEFAULTS.clear()
    
    # Reload base config to get fresh mode defaults (including transport settings)
    config = load_config(str(HARNESS_CONFIG_PATH))

    # Apply scenario overrides
    config = apply_overrides(config, scenario.overrides)
    
    gateway_port, client_port = resolve_ports(config)
    spool_dir = config.get("spool_dir")

    # Apply modem preset if specified
    modem_preset = scenario.overrides.get("modem_preset") or config.get("modem_preset")
    if modem_preset:
        logging.info("Applying modem preset: %s", modem_preset)
        _apply_modem_preset(modem_preset, gateway_port, client_port, bool(config.get("simulate")))

    # Build transports with scenario-specific settings
    gateway_transport = build_transport(
        config.get("simulate", False),
        gateway_port,
        config.get("gateway_node_id", "gateway"),
        spool_dir,
        "gateway",
        disable_dedupe=bool(config.get("disable_dedupe", False)),
        dedupe_lease_seconds=config.get("dedupe_lease_seconds"),
        segment_size=TRANSPORT_DEFAULTS.get("segment_size"),
        chunk_ttl_per_chunk=TRANSPORT_DEFAULTS.get("chunk_ttl_per_chunk"),
        chunk_ttl_max=TRANSPORT_DEFAULTS.get("chunk_ttl_max"),
        chunk_delay_threshold=TRANSPORT_DEFAULTS.get("chunk_delay_threshold"),
        chunk_delay_seconds=TRANSPORT_DEFAULTS.get("chunk_delay_seconds"),
        nack_max_per_seq=TRANSPORT_DEFAULTS.get("nack_max_per_seq"),
        nack_interval=TRANSPORT_DEFAULTS.get("nack_interval"),
    )
    client_transport = build_transport(
        config.get("simulate", False),
        client_port,
        config.get("client_node_id", "client"),
        spool_dir,
        "client",
        disable_dedupe=bool(config.get("disable_dedupe", False)),
        dedupe_lease_seconds=config.get("dedupe_lease_seconds"),
        segment_size=TRANSPORT_DEFAULTS.get("segment_size"),
        chunk_ttl_per_chunk=TRANSPORT_DEFAULTS.get("chunk_ttl_per_chunk"),
        chunk_ttl_max=TRANSPORT_DEFAULTS.get("chunk_ttl_max"),
        chunk_delay_threshold=TRANSPORT_DEFAULTS.get("chunk_delay_threshold"),
        chunk_delay_seconds=TRANSPORT_DEFAULTS.get("chunk_delay_seconds"),
        nack_max_per_seq=TRANSPORT_DEFAULTS.get("nack_max_per_seq"),
        nack_interval=TRANSPORT_DEFAULTS.get("nack_interval"),
    )

    if config.get("clear_spool"):
        clear_spool(gateway_transport)
        clear_spool(client_transport)

    gateway_node_id = resolve_gateway_node_id(config, gateway_transport)
    retarget_spool_destination(client_transport, gateway_node_id)

    gateway, gateway_thread = start_gateway(transport=gateway_transport)
    client = MeshtasticClient(client_transport, gateway_node_id=gateway_node_id)

    try:
        status, duration, req_bytes, resp_bytes, error, resp_data, response_id = run_single_test(
            client,
            command,
            payload,
            timeout=float(config["timeout"]),
            retries=int(config["retries"]),
        )
        chunks_sent = _get_chunks_sent_for_message(
            response_id,
            client_transport,
            gateway_transport,
        )

        # Wait for radio to settle before next test
        quiet_window = float(config.get("post_response_quiet", 10.0))
        quiet_timeout = float(config.get("post_response_timeout", 300.0))
        wait_for_settled(client_transport, quiet_window, quiet_timeout, stop_event)

    finally:
        gateway.stop()
        gateway_thread.join(timeout=2.0)
        close_transport(client_transport)
        close_transport(gateway_transport)

    total_bytes = req_bytes + resp_bytes
    throughput = (total_bytes * 8 / 1000 / duration) if duration > 0 else 0.0

    return TestResult(
        scenario_name=scenario.name,
        description=scenario.description,
        overrides=scenario.overrides,
        command=command,
        payload=payload,
        status=status,
        duration_seconds=duration,
        request_bytes=req_bytes,
        response_bytes=resp_bytes,
        chunks_sent=chunks_sent,
        throughput_kbps=throughput,
        error=error,
        response_data=resp_data,
    )


def format_results(results: List[TestResult]) -> str:
    """Format test results as a human-readable text report."""
    lines = []
    lines.append("=" * 70)
    lines.append("MESHTASTIC WIFI BRIDGE - TEST RESULTS")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 70)
    lines.append("")

    for i, result in enumerate(results, 1):
        lines.append(f"Test {i}: {result.scenario_name}")
        lines.append("-" * 50)
        lines.append(f"Description:    {result.description}")
        lines.append(f"Command:        {result.command}")
        lines.append(f"Status:         {result.status.upper()}")
        lines.append(f"Duration:       {result.duration_seconds:.2f}s")
        lines.append(f"Request size:   {_format_bytes(result.request_bytes)}")
        lines.append(f"Response size:  {_format_bytes(result.response_bytes)}")
        lines.append(f"Total payload:  {_format_bytes(result.request_bytes + result.response_bytes)}")
        lines.append(f"Chunks sent:    {result.chunks_sent}")
        lines.append(f"Throughput:     {result.throughput_kbps:.2f} kbps")
        
        if result.overrides:
            lines.append("Overrides:")
            for key, value in result.overrides.items():
                if isinstance(value, dict):
                    for k, v in value.items():
                        lines.append(f"  {key}.{k}: {v}")
                else:
                    lines.append(f"  {key}: {value}")
        
        if result.error:
            lines.append(f"Error:          {result.error}")
        
        lines.append("")

    # Summary section
    lines.append("=" * 70)
    lines.append("SUMMARY")
    lines.append("=" * 70)
    
    success_count = sum(1 for r in results if r.status == "success")
    timeout_count = sum(1 for r in results if r.status == "timeout")
    error_count = sum(1 for r in results if r.status == "error")
    
    lines.append(f"Total tests:    {len(results)}")
    lines.append(f"Successful:     {success_count}")
    lines.append(f"Timeouts:       {timeout_count}")
    lines.append(f"Errors:         {error_count}")
    lines.append("")
    
    if results:
        total_duration = sum(r.duration_seconds for r in results)
        total_bytes = sum(r.request_bytes + r.response_bytes for r in results)
        avg_throughput = sum(r.throughput_kbps for r in results) / len(results)
        
        lines.append(f"Total duration: {total_duration:.2f}s")
        lines.append(f"Total data:     {_format_bytes(total_bytes)}")
        lines.append(f"Avg throughput: {avg_throughput:.2f} kbps")
        
        # Find best/worst scenarios
        successful = [r for r in results if r.status == "success"]
        if successful:
            fastest = min(successful, key=lambda r: r.duration_seconds)
            slowest = max(successful, key=lambda r: r.duration_seconds)
            highest_tp = max(successful, key=lambda r: r.throughput_kbps)
            
            lines.append("")
            lines.append(f"Fastest:        {fastest.scenario_name} ({fastest.duration_seconds:.2f}s)")
            lines.append(f"Slowest:        {slowest.scenario_name} ({slowest.duration_seconds:.2f}s)")
            lines.append(f"Best throughput: {highest_tp.scenario_name} ({highest_tp.throughput_kbps:.2f} kbps)")

    lines.append("")
    lines.append("=" * 70)
    
    return "\n".join(lines)


def _format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    return f"{size / 1024:.1f} KB"


def _get_chunks_sent_for_message(
    message_id: Optional[str],
    client_transport: Any,
    gateway_transport: Any,
) -> int:
    if not message_id:
        return 0
    client_count = client_transport.get_sent_chunk_count(message_id)
    gateway_count = gateway_transport.get_sent_chunk_count(message_id)
    return client_count + gateway_count


def display_menu(scenarios: List[TestScenario], commands: List[str]) -> tuple[str, List[int]]:
    """Display the test selection menu and return user choices."""
    print("\n" + "=" * 60)
    print("MESHTASTIC SCENARIO RUNNER")
    print("=" * 60)
    
    print("\nAvailable test types:")
    for i, cmd in enumerate(commands, 1):
        print(f"  {i}. {cmd}")
    
    print("\nAvailable scenarios:")
    for i, scenario in enumerate(scenarios, 1):
        print(f"  {i}. {scenario.name}")
        print(f"     {scenario.description}")
    
    print("\nOptions:")
    print("  a - Run all scenarios")
    print("  q - Quit")
    print()
    
    # Get test type selection
    while True:
        choice = input("Select test type (number): ").strip()
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(commands):
                selected_command = commands[idx]
                break
        except ValueError:
            pass  # Invalid input, will prompt again
        print("Invalid selection. Please enter a valid number.")
    
    # Get scenario selection
    while True:
        choice = input("Select scenarios (comma-separated numbers, 'a' for all, 'q' to quit): ").strip().lower()
        
        if choice == 'q':
            return selected_command, []
        
        if choice == 'a':
            return selected_command, list(range(len(scenarios)))
        
        try:
            indices = [int(x.strip()) - 1 for x in choice.split(",")]
            if all(0 <= i < len(scenarios) for i in indices):
                return selected_command, indices
        except ValueError:
            pass  # Invalid input, will prompt again
        
        print("Invalid selection. Please enter valid numbers separated by commas.")


def prompt_payload_for_command(command: str, default_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Prompt user for command-specific payload."""
    if command == "http_request":
        print("\nConfigure HTTP request:")
        url = input(f"  URL [{default_payload.get('url', 'https://example.com')}]: ").strip()
        method = input(f"  Method [{default_payload.get('method', 'GET')}]: ").strip().upper()
        
        return {
            "url": url or default_payload.get("url", "https://example.com"),
            "method": method or default_payload.get("method", "GET"),
        }
    
    elif command == "echo":
        message = input("  Message [hello from test runner]: ").strip()
        return {"message": message or "hello from test runner"}
    
    elif command == "health":
        return {}
    
    elif command == "payload_digest":
        default_size = int(default_payload.get("size_kb", 10))
        default_random = bool(default_payload.get("payload_random", False))
        size_kb = input(f"  Size in KB [{default_size}]: ").strip()
        return {
            "size_kb": int(size_kb) if size_kb else default_size,
            "payload_random": default_random,
        }
    
    return default_payload


def _prepare_payload(command: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    prepared = dict(payload)
    if command == "payload_digest":
        size_kb = prepared.pop("size_kb", None)
        payload_random = bool(prepared.pop("payload_random", False))
        if size_kb is not None:
            size_bytes = max(0, int(size_kb) * 1024)
            if payload_random:
                prepared["content_b64"] = base64.b64encode(os.urandom(size_bytes)).decode("ascii")
            else:
                prepared["payload"] = "A" * size_bytes
    return prepared


def main() -> None:
    # Set up paths
    runner_dir = Path(__file__).resolve().parent
    scenarios_path = runner_dir / "test_scenarios.json"
    
    # Load scenarios
    if not scenarios_path.exists():
        print(f"Error: test_scenarios.json not found at {scenarios_path}")
        sys.exit(1)
    
    scenarios, default_command, default_payload = load_test_scenarios(str(scenarios_path))
    
    if not scenarios:
        print("Error: No scenarios defined in test_scenarios.json")
        sys.exit(1)
    
    # Configure logging using hardware_harness config
    config = load_config(str(HARNESS_CONFIG_PATH))
    configure_logging(
        config.get("log_level", "INFO"),
        log_file=config.get("log_file"),
    )
    
    # Available commands
    commands = ["http_request", "echo", "health", "payload_digest"]
    
    # Display menu and get selections
    selected_command, selected_indices = display_menu(scenarios, commands)
    
    if not selected_indices:
        print("No scenarios selected. Exiting.")
        return
    
    # Prompt for payload
    payload = prompt_payload_for_command(selected_command, default_payload)
    
    print(f"\nWill run {len(selected_indices)} scenario(s) with command '{selected_command}'")
    print(f"Payload: {json.dumps(payload)}")
    confirm = input("\nProceed? [Y/n]: ").strip().lower()
    if confirm == 'n':
        print("Cancelled.")
        return
    
    # Set up signal handling
    stop_event = threading.Event()
    
    def handle_signal(signum: int, _frame: Any) -> None:
        logging.info("Received signal %s, shutting down...", signum)
        stop_event.set()
    
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    
    # Run selected scenarios
    results: List[TestResult] = []
    selected_scenarios = [scenarios[i] for i in selected_indices]

    # Create results directory and path up front for incremental updates
    results_dir = runner_dir / "results"
    results_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    results_filename = f"test_results_{timestamp}.txt"
    results_path = results_dir / results_filename

    def write_results_snapshot() -> None:
        report_text = format_results(results)
        with open(results_path, "w", encoding="utf-8") as f:
            f.write(report_text)

    for i, scenario in enumerate(selected_scenarios, 1):
        if stop_event.is_set():
            print("\nTest run interrupted.")
            break
        
        print(f"\n[{i}/{len(selected_scenarios)}] Running: {scenario.name}")
        scenario_payload = dict(payload)
        if isinstance(scenario.payload_overrides, dict):
            scenario_payload.update(scenario.payload_overrides)
        scenario_payload = _prepare_payload(selected_command, scenario_payload)
        
        try:
            result = run_scenario(
                scenario=scenario,
                base_config=config,
                command=selected_command,
                payload=scenario_payload,
                stop_event=stop_event,
            )
            results.append(result)
            
            print(f"Result: {result.status.upper()} - {result.duration_seconds:.2f}s - {result.throughput_kbps:.2f} kbps")
            write_results_snapshot()
            
        except Exception as exc:
            logging.error("Scenario %s failed with exception: %s", scenario.name, exc)
            results.append(TestResult(
                scenario_name=scenario.name,
                description=scenario.description,
                overrides=scenario.overrides,
                command=selected_command,
                payload=scenario_payload,
                status="error",
                duration_seconds=0.0,
                request_bytes=0,
                response_bytes=0,
                chunks_sent=0,
                throughput_kbps=0.0,
                error=str(exc),
            ))
            write_results_snapshot()
    
    # Generate and save results
    if results:
        report = format_results(results)
        print("\n" + report)
        with open(results_path, "w", encoding="utf-8") as f:
            f.write(report)
        
        print(f"\nResults saved to: {results_path}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(0)
