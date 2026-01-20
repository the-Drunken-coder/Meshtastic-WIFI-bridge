"""Interactive testing tool for two Meshtastic radios.

This script:
1) Detects two Meshtastic radios connected over serial.
2) Lets the user assign one as gateway and one as client (or auto-assign).
3) Provides a simple terminal UI to send messages and run bandwidth trials.
4) Can run bandwidth tests across multiple Meshtastic modem presets
   (short/medium/long fast/slow) sequentially for comparison.

Requires meshtastic>=2.0.0 installed.
"""

import argparse
import sys
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

from meshtastic.serial_interface import SerialInterface
from pubsub import pub

# Use shared port number with transport layer
from transport.meshtastic_transport import MeshtasticTransport

# Map friendly mode names to Meshtastic modem presets
MODE_PRESETS = {
    "short_fast": "SHORT_FAST",
    "short_slow": "SHORT_SLOW",
    "medium_fast": "MEDIUM_FAST",
    "medium_slow": "MEDIUM_SLOW",
    "long_fast": "LONG_FAST",
    "long_slow": "LONG_SLOW",
}

# Human-friendly labels for menu
MODE_LABELS = {
    "short_fast": "Short Fast",
    "short_slow": "Short Slow",
    "medium_fast": "Medium Fast",
    "medium_slow": "Medium Slow",
    "long_fast": "Long Fast",
    "long_slow": "Long Slow",
}


@dataclass
class RadioInfo:
    port: str
    node_id: int


def list_meshtastic_ports(timeout: float = 3.0) -> List[RadioInfo]:
    """Detect Meshtastic radios on available serial ports."""
    import serial.tools.list_ports

    radios: List[RadioInfo] = []
    ports = serial.tools.list_ports.comports()
    start_time = time.monotonic()
    for port in ports:
        if time.monotonic() - start_time > timeout:
            break
        port_path = port.device
        try:
            iface = SerialInterface(port_path, debugOut=None, noProto=True)
            if iface.myInfo:
                radios.append(RadioInfo(port=port_path, node_id=iface.myInfo.my_node_num))
            iface.close()
        except Exception:
            continue
    return radios


def select_two_radios(radios: List[RadioInfo]) -> Tuple[RadioInfo, RadioInfo]:
    """Let user pick two radios; auto if exactly two."""
    if len(radios) < 2:
        raise RuntimeError("Need at least two Meshtastic radios connected.")
    if len(radios) == 2:
        return radios[0], radios[1]

    # Prompt user to select gateway radio with validation
    while True:
        print("Select Gateway radio:")
        for idx, r in enumerate(radios, 1):
            print(f"{idx}) {r.port} (node {r.node_id:#x})")
        choice = input("Gateway choice: ").strip()
        try:
            g_idx = int(choice) - 1
        except ValueError:
            print("Invalid input. Please enter a number corresponding to a radio.")
            continue
        if g_idx < 0 or g_idx >= len(radios):
            print(f"Invalid selection. Please enter a number between 1 and {len(radios)}.")
            continue
        break

    # Prompt user to select client radio with validation
    while True:
        print("Select Client radio:")
        for idx, r in enumerate(radios, 1):
            if idx - 1 == g_idx:
                continue
            print(f"{idx}) {r.port} (node {r.node_id:#x})")
        choice = input("Client choice: ").strip()
        try:
            c_idx = int(choice) - 1
        except ValueError:
            print("Invalid input. Please enter a number corresponding to a radio.")
            continue
        if c_idx < 0 or c_idx >= len(radios):
            print(f"Invalid selection. Please enter a number between 1 and {len(radios)} (excluding the gateway).")
            continue
        if c_idx == g_idx:
            print("Client radio must be different from the gateway radio. Please choose another.")
            continue
        break

    return radios[g_idx], radios[c_idx]


def set_modem_preset(iface: SerialInterface, preset_name: str) -> None:
    """Apply a modem preset by name."""
    try:
    from meshtastic.protobufs import config_pb2
    except ImportError:
        print("meshtastic protobufs not available; cannot set preset.")
        return

    preset = getattr(config_pb2.Config.LoraConfig.ModemPreset, preset_name, None)
    if preset is None:
        print(f"Unknown preset {preset_name}, skipping.")
        return
    iface.localConfig.lora.modem_preset = preset
    iface.writeConfig("lora")
    iface.waitForConfig()


def send_test_message(src: SerialInterface, dest_id: int, text: str) -> None:
    payload = text.encode("utf-8")
    src.sendData(payload, destinationId=dest_id, portNum=MeshtasticTransport.PORTNUM, wantAck=False)


def measure_bandwidth(src: SerialInterface, dest: SerialInterface, bytes_len: int = 2048) -> float:
    """Send a payload and measure effective throughput using ACK timing; return bytes/sec."""
    data = b"x" * bytes_len
    start = time.time()
    src.sendData(
        data,
        destinationId=dest.myInfo.my_node_num,
        portNum=MeshtasticTransport.PORTNUM,
        wantAck=True,
    )
    # Prefer waiting for an actual acknowledgment if supported.
    if hasattr(src, "waitForAck"):
        try:
            src.waitForAck(timeout=10.0)
        except Exception:
            pass
    end = time.time()
    duration = max(end - start, 0.001)
    return bytes_len / duration


def interactive_ui(gateway: SerialInterface, client: SerialInterface) -> None:
    client_id = client.myInfo.my_node_num
    gateway_id = gateway.myInfo.my_node_num

    while True:
        print("\nMeshtastic Test UI")
        print("1) Send message from gateway -> client")
        print("2) Send message from client -> gateway")
        print("3) Run single bandwidth test (client -> gateway)")
        print("4) Run bandwidth comparison across modes")
        print("5) Quit")
        choice = input("Select: ").strip()

        if choice == "1":
            msg = input("Enter message: ")
            send_test_message(gateway, client_id, msg)
            print("Sent.")
        elif choice == "2":
            msg = input("Enter message: ")
            send_test_message(client, gateway_id, msg)
            print("Sent.")
        elif choice == "3":
            bw = measure_bandwidth(client, gateway)
            print(f"Approx bandwidth: {bw:.1f} bytes/sec")
        elif choice == "4":
            run_bandwidth_comparison(client, gateway)
        elif choice == "5":
            break
        else:
            print("Invalid choice.")


def run_bandwidth_comparison(client: SerialInterface, gateway: SerialInterface) -> None:
    """Iterate presets and measure bandwidth for each."""
    results = []
    for key, preset_name in MODE_PRESETS.items():
        label = MODE_LABELS.get(key, key)
        print(f"Setting mode: {label} ({preset_name})")
        set_modem_preset(client, preset_name)
        set_modem_preset(gateway, preset_name)
        time.sleep(3.0)
        bw = measure_bandwidth(client, gateway)
        results.append((label, bw))
        print(f"  {label}: {bw:.1f} bytes/sec")
    print("\nSummary:")
    for label, bw in results:
        print(f"- {label}: {bw:.1f} bytes/sec")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Meshtastic radio test tool")
    parser.add_argument("--auto", action="store_true", help="Auto-select first two radios")
    args = parser.parse_args(argv)

    radios = list_meshtastic_ports()
    if len(radios) < 2:
        print("Need at least two Meshtastic radios connected.")
        return 1
    gateway_info, client_info = select_two_radios(radios) if not args.auto else (radios[0], radios[1])

    print(f"Gateway: {gateway_info.port} (node {gateway_info.node_id:#x})")
    print(f"Client:  {client_info.port} (node {client_info.node_id:#x})")

    gateway_iface = SerialInterface(gateway_info.port)
    client_iface = SerialInterface(client_info.port)

    # subscribe to print received text payloads
def on_rx(packet, _iface):
        decoded = packet.get("decoded", {})
        if decoded.get("portnum") not in ("PRIVATE_APP", MeshtasticTransport.PORTNUM):
            return
        payload = decoded.get("payload", b"")
        if isinstance(payload, str):
            payload = payload.encode("latin-1")
        src = packet.get("fromId") or packet.get("from")
        try:
            src_int = int(str(src).replace("!", ""), 16) if isinstance(src, str) else int(src)
        except Exception:
            src_int = src
        print(f"\nRX from {src_int}: {payload}")

    pub.subscribe(on_rx, "meshtastic.receive.data")

    try:
        interactive_ui(gateway_iface, client_iface)
    finally:
        gateway_iface.close()
        client_iface.close()
        pub.unsubscribe(on_rx, "meshtastic.receive.data")

    return 0


if __name__ == "__main__":
    sys.exit(main())
