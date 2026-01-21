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
import random
import sys
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from meshtastic.serial_interface import SerialInterface
from pubsub import pub

from common.config import Config
# Use shared port number with transport layer
from transport.meshtastic_transport import MeshtasticTransport
from framing.codec import decode_frame, FrameDecodeError
from reliability.stream import Stream, StreamState
from reliability.retransmit import RetransmitTimer

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


@dataclass
class StreamLink:
    stream_id: int
    gateway_stream: Stream
    client_stream: Stream
    retransmit_timer: RetransmitTimer
    suppress_rx_output: bool = False
    on_rx: Optional[Callable[..., None]] = None


def list_meshtastic_ports(timeout: float = 10.0, per_port_timeout: float = 2.0) -> List[RadioInfo]:
    """Detect Meshtastic radios on available serial ports."""
    import serial.tools.list_ports

    radios: List[RadioInfo] = []
    ports = serial.tools.list_ports.comports()
    start_time = time.monotonic()
    for port in ports:
        if time.monotonic() - start_time > timeout:
            break
        port_path = port.device
        iface = None
        try:
            iface = SerialInterface(port_path, debugOut=None)
            deadline = time.monotonic() + per_port_timeout
            while time.monotonic() < deadline and not iface.myInfo:
                time.sleep(0.1)
            if iface.myInfo:
                radios.append(RadioInfo(port=port_path, node_id=iface.myInfo.my_node_num))
        except Exception:
            continue
        finally:
            if iface:
                try:
                    iface.close()
                except Exception:
                    pass
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


def _get_modem_preset_enum():
    try:
        from meshtastic.protobufs import config_pb2
    except ImportError:
        try:
            from meshtastic.protobuf import config_pb2
        except ImportError:
            return None
    lora_config = getattr(config_pb2.Config, "LoRaConfig", None) or getattr(
        config_pb2.Config, "LoraConfig", None
    )
    if not lora_config:
        return None
    return lora_config.ModemPreset


def set_modem_preset(iface: SerialInterface, preset_name: str) -> None:
    """Apply a modem preset by name."""
    modem_preset_enum = _get_modem_preset_enum()
    if not modem_preset_enum:
        print("meshtastic protobufs not available; cannot set preset.")
        return

    preset_value = getattr(modem_preset_enum, preset_name, None)
    if preset_value is None:
        print(f"Unknown preset {preset_name}, skipping.")
        return
    
    try:
        iface.localNode.localConfig.lora.modem_preset = preset_value
        iface.localNode.writeConfig("lora")
        time.sleep(0.1)
        iface.waitForConfig()
    except Exception as e:
        print(f"Failed to apply modem preset '{preset_name}': {e}")


def send_test_message(stream: Stream, text: str) -> None:
    payload = text.encode("utf-8")
    queued = stream.send(payload)
    if queued == 0:
        print("Send failed.")


def measure_bandwidth(
    sender: Stream,
    receiver: Stream,
    bytes_len: int = 2048,
    timeout_s: float = 60.0,
) -> float:
    """Send a payload and measure effective throughput; return bytes/sec."""
    data = b"x" * bytes_len
    start = time.monotonic()
    queued = sender.send(data)
    if queued == 0:
        return 0.0

    received = 0
    deadline = start + timeout_s
    while received < bytes_len:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        chunk = receiver.recv(
            max_bytes=min(4096, bytes_len - received),
            timeout=remaining,
        )
        if not chunk:
            if receiver.state in (StreamState.CLOSED, StreamState.FIN_RECV):
                break
            continue
        received += len(chunk)

    duration = max(time.monotonic() - start, 0.001)
    return received / duration


def interactive_ui(link: StreamLink, gateway_iface: SerialInterface, client_iface: SerialInterface) -> None:
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
            send_test_message(link.gateway_stream, msg)
            print("Sent.")
        elif choice == "2":
            msg = input("Enter message: ")
            send_test_message(link.client_stream, msg)
            print("Sent.")
        elif choice == "3":
            link.suppress_rx_output = True
            try:
                bw = measure_bandwidth(link.client_stream, link.gateway_stream)
            finally:
                link.suppress_rx_output = False
            print(f"Approx bandwidth: {bw:.1f} bytes/sec")
        elif choice == "4":
            run_bandwidth_comparison(link, client_iface, gateway_iface)
        elif choice == "5":
            break
        else:
            print("Invalid choice.")


def run_bandwidth_comparison(
    link: StreamLink,
    client_iface: SerialInterface,
    gateway_iface: SerialInterface,
) -> None:
    """Iterate presets and measure bandwidth for each."""
    results = []
    client_original = getattr(
        getattr(client_iface.localNode.localConfig, "lora", None), "modem_preset", None
    )
    gateway_original = getattr(
        getattr(gateway_iface.localNode.localConfig, "lora", None), "modem_preset", None
    )
    try:
        for key, preset_name in MODE_PRESETS.items():
            label = MODE_LABELS.get(key, key)
            print(f"Setting mode: {label} ({preset_name})")
            set_modem_preset(client_iface, preset_name)
            set_modem_preset(gateway_iface, preset_name)
            time.sleep(3.0)
            link.suppress_rx_output = True
            try:
                bw = measure_bandwidth(link.client_stream, link.gateway_stream)
            finally:
                link.suppress_rx_output = False
            results.append((label, bw))
            print(f"  {label}: {bw:.1f} bytes/sec")
    finally:
        if client_original is not None:
            client_iface.localNode.localConfig.lora.modem_preset = client_original
            client_iface.localNode.writeConfig("lora")
            time.sleep(0.1)
            client_iface.waitForConfig()
        if gateway_original is not None:
            gateway_iface.localNode.localConfig.lora.modem_preset = gateway_original
            gateway_iface.localNode.writeConfig("lora")
            time.sleep(0.1)
            gateway_iface.waitForConfig()
    print("\nSummary:")
    for label, bw in results:
        print(f"- {label}: {bw:.1f} bytes/sec")


def _make_send_callback(
    iface: SerialInterface,
    dest_id: int,
) -> Callable[[bytes], bool]:
    def send(data: bytes) -> bool:
        packet = iface.sendData(
            data,
            destinationId=dest_id,
            portNum=MeshtasticTransport.PORTNUM,
            wantAck=False,
        )
        return packet is not None
    return send


def setup_stream_link(gateway_iface: SerialInterface, client_iface: SerialInterface) -> StreamLink:
    gateway_id = gateway_iface.myInfo.my_node_num
    client_id = client_iface.myInfo.my_node_num
    stream_id = random.randint(1, 0x7FFFFFFF)
    config = Config()

    gateway_stream = Stream(
        stream_id=stream_id,
        remote_node_id=client_id,
        config=config,
        send_callback=_make_send_callback(gateway_iface, client_id),
    )
    client_stream = Stream(
        stream_id=stream_id,
        remote_node_id=gateway_id,
        config=config,
        send_callback=_make_send_callback(client_iface, gateway_id),
    )

    link = StreamLink(
        stream_id=stream_id,
        gateway_stream=gateway_stream,
        client_stream=client_stream,
        retransmit_timer=RetransmitTimer(
            interval_ms=1000,
            callback=lambda: (
                gateway_stream.check_retransmits(),
                client_stream.check_retransmits(),
            ),
        ),
    )

    def on_rx(packet=None, interface=None, **_kwargs):
        decoded = packet.get("decoded", {})
        portnum = decoded.get("portnum")
        if portnum not in ("PRIVATE_APP", MeshtasticTransport.PORTNUM):
            return
        payload = decoded.get("payload", b"")
        if isinstance(payload, str):
            payload = payload.encode("latin-1")
        try:
            frame = decode_frame(payload)
        except FrameDecodeError:
            return
        if frame.stream_id != link.stream_id:
            return

        if interface == gateway_iface:
            stream = link.gateway_stream
            label = "gateway"
        elif interface == client_iface:
            stream = link.client_stream
            label = "client"
        else:
            return

        if frame.is_syn() and stream.state == StreamState.CLOSED:
            stream.window.receive_frame(frame)
            stream.accept()
            return

        stream.receive_frame(frame)

        if link.suppress_rx_output:
            return

        data = stream.recv(max_bytes=4096, timeout=0)
        if data:
            src = packet.get("fromId") or packet.get("from")
            try:
                src_int = int(str(src).replace("!", ""), 16) if isinstance(src, str) else int(src)
            except Exception:
                src_int = src
            print(f"\nRX from {src_int} ({label}): {data}")

    pub.subscribe(on_rx, "meshtastic.receive.data")
    link.on_rx = on_rx

    if not client_stream.open():
        print("Warning: failed to send SYN for test stream.")
    else:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and client_stream.state != StreamState.OPEN:
            time.sleep(0.1)
        if client_stream.state != StreamState.OPEN:
            print("Warning: test stream not open yet; proceeding anyway.")

    link.retransmit_timer.start()
    return link


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

    link = setup_stream_link(gateway_iface, client_iface)

    try:
        interactive_ui(link, gateway_iface, client_iface)
    finally:
        link.retransmit_timer.stop()
        if link.on_rx:
            pub.unsubscribe(link.on_rx, "meshtastic.receive.data")
        gateway_iface.close()
        client_iface.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
