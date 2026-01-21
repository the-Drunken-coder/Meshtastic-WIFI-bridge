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
import logging
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple, Iterable

from meshtastic.serial_interface import SerialInterface
from pubsub import pub

from common.config import Config
from common.logging_setup import setup_logging, get_logger
# Use shared port number with transport layer
from transport.meshtastic_transport import MeshtasticTransport
from framing.codec import decode_frame, FrameDecodeError
from reliability.stream import Stream, StreamState
from reliability.retransmit import RetransmitTimer
from reliability.ack_methods.base import AckNackMethod
from reliability.ack_methods.smart import SmartAckNack

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

logger = get_logger(__name__)


def prompt_radio_mode(default_index: int = 6) -> str:
    """Prompt user for radio modem preset and return preset name."""
    print("Radio Modem Preset")
    keys = list(MODE_PRESETS.keys())
    for idx, key in enumerate(keys, 1):
        label = MODE_LABELS.get(key, key)
        print(f"  {idx}. {label}")
    prompt = f"Select radio mode [{default_index}]: "
    while True:
        choice = input(prompt).strip()
        if not choice:
            choice = str(default_index)
        try:
            idx = int(choice)
        except ValueError:
            print("Please enter a valid number.")
            continue
        if 1 <= idx <= len(keys):
            return MODE_PRESETS[keys[idx - 1]]
        print(f"Please enter a number between 1 and {len(keys)}.")


def prompt_test_type(default_index: int = 1) -> str:
    """Prompt user for test type and return selection key."""
    options = [
        ("single", "Run single bandwidth test"),
        ("compare", "Run bandwidth comparison across modes"),
        ("report", "Run repeated tests and generate report"),
        ("ui", "Interactive UI"),
    ]
    print("Test Type")
    for idx, (_, label) in enumerate(options, 1):
        print(f"  {idx}. {label}")
    prompt = f"Select test type [{default_index}]: "
    while True:
        choice = input(prompt).strip()
        if not choice:
            choice = str(default_index)
        try:
            idx = int(choice)
        except ValueError:
            print("Please enter a valid number.")
            continue
        if 1 <= idx <= len(options):
            return options[idx - 1][0]
        print(f"Please enter a number between 1 and {len(options)}.")


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
    logger.info("Serial ports detected: %s", [p.device for p in ports])
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
            logger.debug("Probe failed for %s", port_path, exc_info=True)
            continue
        finally:
            if iface:
                try:
                    iface.close()
                except Exception:
                    # Ignore errors during cleanup - interface may already be closed
                    pass
    return radios


def probe_meshtastic_port(port_path: str, per_port_timeout: float) -> Optional[RadioInfo]:
    iface = None
    try:
        iface = SerialInterface(port_path, debugOut=None)
        deadline = time.monotonic() + per_port_timeout
        while time.monotonic() < deadline and not iface.myInfo:
            time.sleep(0.1)
        if iface.myInfo:
            return RadioInfo(port=port_path, node_id=iface.myInfo.my_node_num)
    except Exception:
        logger.debug("Probe failed for %s", port_path, exc_info=True)
    finally:
        if iface:
            try:
                iface.close()
            except Exception:
                pass
    return None


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
    else:
        logger.info("Queued %d bytes on stream %s", queued, hex(stream.stream_id))


def measure_bandwidth(
    sender: Stream,
    receiver: Stream,
    bytes_len: int = 2048,
    timeout_s: float = 60.0,
) -> dict:
    """Send a payload and measure effective throughput; return stats."""
    data = b"x" * bytes_len
    start = time.monotonic()
    stats_start = {
        "sender_frames_sent": sender.stats.frames_sent,
        "receiver_frames_received": receiver.stats.frames_received,
        "sender_retransmits": sender.stats.retransmits,
        "receiver_retransmits": receiver.stats.retransmits,
        "sender_rtt_count": sender.stats.rtt_count,
        "sender_rtt_sum_ms": sender.stats.rtt_sum_ms,
    }
    queued = sender.send(data)
    if queued == 0:
        return {
            "bytes_sent": 0,
            "bytes_received": 0,
            "duration_s": 0.0,
            "bytes_per_sec": 0.0,
            "sender_retransmits": 0,
            "receiver_retransmits": 0,
            "sender_frames_sent": 0,
            "receiver_frames_received": 0,
        }
    logger.info(
        "Bandwidth test started: stream=%s bytes=%d", hex(sender.stream_id), bytes_len
    )

    received = 0
    timeouts = 0
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
            timeouts += 1
            continue
        received += len(chunk)

    duration = max(time.monotonic() - start, 0.001)
    bytes_per_sec = received / duration
    stats_end = {
        "sender_frames_sent": sender.stats.frames_sent,
        "receiver_frames_received": receiver.stats.frames_received,
        "sender_retransmits": sender.stats.retransmits,
        "receiver_retransmits": receiver.stats.retransmits,
    }
    result = {
        "bytes_sent": queued,
        "bytes_received": received,
        "duration_s": duration,
        "bytes_per_sec": bytes_per_sec,
        "sender_retransmits": stats_end["sender_retransmits"] - stats_start["sender_retransmits"],
        "receiver_retransmits": stats_end["receiver_retransmits"] - stats_start["receiver_retransmits"],
        "sender_frames_sent": stats_end["sender_frames_sent"] - stats_start["sender_frames_sent"],
        "receiver_frames_received": stats_end["receiver_frames_received"] - stats_start["receiver_frames_received"],
        "recv_timeouts": timeouts,
        "sender_rtt_count": sender.stats.rtt_count - stats_start["sender_rtt_count"],
        "sender_rtt_avg_ms": 0.0,
        "sender_rtt_max_ms": sender.stats.rtt_max_ms,
        "sender_max_pending": sender.stats.max_pending,
    }
    if result["sender_rtt_count"]:
        result["sender_rtt_avg_ms"] = (
            sender.stats.rtt_sum_ms - stats_start["sender_rtt_sum_ms"]
        ) / result["sender_rtt_count"]
    logger.info(
        "Bandwidth test finished: received=%d duration=%.3fs bytes_per_sec=%.1f",
        received,
        duration,
        bytes_per_sec,
    )
    return result


def _parse_list(values: Optional[str], cast_func, default: Iterable):
    if not values:
        return list(default)
    items = []
    for item in values.split(","):
        item = item.strip()
        if not item:
            continue
        items.append(cast_func(item))
    return items or list(default)


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
            print(
                "Bandwidth results:"
                f"\n- bytes_sent: {bw['bytes_sent']}"
                f"\n- bytes_received: {bw['bytes_received']}"
                f"\n- duration_s: {bw['duration_s']:.3f}"
                f"\n- bytes_per_sec: {bw['bytes_per_sec']:.1f}"
                f"\n- sender_frames_sent: {bw['sender_frames_sent']}"
                f"\n- receiver_frames_received: {bw['receiver_frames_received']}"
                f"\n- sender_retransmits: {bw['sender_retransmits']}"
                f"\n- receiver_retransmits: {bw['receiver_retransmits']}"
                f"\n- recv_timeouts: {bw['recv_timeouts']}"
                f"\n- sender_rtt_avg_ms: {bw['sender_rtt_avg_ms']:.1f}"
                f"\n- sender_rtt_max_ms: {bw['sender_rtt_max_ms']:.1f}"
                f"\n- sender_max_pending: {bw['sender_max_pending']}"
            )
            break
        elif choice == "4":
            run_bandwidth_comparison(link, client_iface, gateway_iface)
            break
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
            print(
                f"  {label}: {bw['bytes_per_sec']:.1f} bytes/sec "
                f"(recv {bw['bytes_received']} in {bw['duration_s']:.1f}s, "
                f"retransmits {bw['sender_retransmits']}/{bw['receiver_retransmits']}, "
                f"rtt_avg {bw['sender_rtt_avg_ms']:.0f}ms)"
            )
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
        print(
            f"- {label}: {bw['bytes_per_sec']:.1f} bytes/sec "
            f"(recv {bw['bytes_received']} in {bw['duration_s']:.1f}s, "
            f"retransmits {bw['sender_retransmits']}/{bw['receiver_retransmits']})"
        )


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


def setup_stream_link(
    gateway_iface: SerialInterface,
    client_iface: SerialInterface,
    debug_frames: bool = False,
    config: Optional[Config] = None,
    ack_method_factory: Optional[Callable[[], AckNackMethod]] = None,
) -> StreamLink:
    gateway_id = gateway_iface.myInfo.my_node_num
    client_id = client_iface.myInfo.my_node_num
    stream_id = random.randint(1, 0x7FFFFFFF)
    config = config or Config()
    ack_method_factory = ack_method_factory or (lambda: SmartAckNack())

    gateway_stream = Stream(
        stream_id=stream_id,
        remote_node_id=client_id,
        config=config,
        send_callback=_make_send_callback(gateway_iface, client_id),
        ack_method=ack_method_factory(),
    )
    client_stream = Stream(
        stream_id=stream_id,
        remote_node_id=gateway_id,
        config=config,
        send_callback=_make_send_callback(client_iface, gateway_id),
        ack_method=ack_method_factory(),
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
        if not payload:
            return
        if isinstance(payload, str):
            payload = payload.encode("latin-1")
        try:
            frame = decode_frame(payload)
        except FrameDecodeError:
            return
        if debug_frames:
            logger.debug(
                "RX frame stream=%s seq=%d ack=%d flags=%s len=%d",
                hex(frame.stream_id),
                frame.seq,
                frame.ack,
                frame.flags,
                len(frame.payload),
            )
        if frame.stream_id != link.stream_id:
            return

        if interface == gateway_iface:
            stream = link.gateway_stream
            label = "gateway"
        elif interface == client_iface:
            stream = link.client_stream
            label = "client"
        else:
            src = packet.get("fromId") or packet.get("from")
            try:
                src_int = int(str(src).replace("!", ""), 16) if isinstance(src, str) else int(src)
            except Exception:
                src_int = src
            if src_int == client_id:
                stream = link.gateway_stream
                label = "gateway"
            elif src_int == gateway_id:
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

    pub.subscribe(on_rx, "meshtastic.receive")
    link.on_rx = on_rx

    if not client_stream.open():
        print("Warning: failed to send SYN for test stream.")
    else:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and client_stream.state != StreamState.OPEN:
            time.sleep(0.1)
        if client_stream.state != StreamState.OPEN:
            print("Warning: test stream not open yet; proceeding anyway.")
        else:
            logger.info("Test stream opened: %s", hex(stream_id))

    link.retransmit_timer.start()
    return link


def close_stream_link(link: StreamLink) -> None:
    link.retransmit_timer.stop()
    if link.on_rx:
        pub.unsubscribe(link.on_rx, "meshtastic.receive")


def run_report(
    gateway_iface: SerialInterface,
    client_iface: SerialInterface,
    bytes_len: int,
    timeout_s: float,
    repeat_count: int,
    repeat_delay_s: float,
    window_sizes: List[int],
    retransmit_timeouts_ms: List[int],
    ack_every_n_values: List[int],
    ack_interval_s_values: List[float],
    nack_interval_s_values: List[float],
    debug_frames: bool,
    show_handshake_logs: bool,
    report_file: Optional[str],
) -> None:
    results = []
    total = (
        len(window_sizes)
        * len(retransmit_timeouts_ms)
        * len(ack_every_n_values)
        * len(ack_interval_s_values)
        * len(nack_interval_s_values)
    )
    logger.info("Running report: %d configurations x %d repeats", total, repeat_count)

    for window_size in window_sizes:
        for rto_ms in retransmit_timeouts_ms:
            for ack_every_n in ack_every_n_values:
                for ack_interval_s in ack_interval_s_values:
                    for nack_interval_s in nack_interval_s_values:
                        label = (
                            f"window={window_size} rto={rto_ms} "
                            f"ack_every={ack_every_n} ack_int={ack_interval_s} "
                            f"nack_int={nack_interval_s}"
                        )
                        logger.info("Testing %s", label)
                        run_stats = []

                        for _ in range(repeat_count):
                            config = Config(
                                window_size=window_size,
                                retransmit_timeout_ms=rto_ms,
                            )

                            ack_factory = lambda: SmartAckNack(
                                ack_every_n=ack_every_n,
                                ack_interval_s=ack_interval_s,
                                nack_interval_s=nack_interval_s,
                            )

                            if not show_handshake_logs:
                                logging.disable(logging.CRITICAL)
                            link = setup_stream_link(
                                gateway_iface,
                                client_iface,
                                debug_frames=debug_frames,
                                config=config,
                                ack_method_factory=ack_factory,
                            )
                            if not show_handshake_logs:
                                logging.disable(logging.NOTSET)

                            try:
                                link.suppress_rx_output = True
                                bw = measure_bandwidth(
                                    link.client_stream,
                                    link.gateway_stream,
                                    bytes_len=bytes_len,
                                    timeout_s=timeout_s,
                                )
                                run_stats.append(bw)
                            finally:
                                close_stream_link(link)

                            if repeat_delay_s > 0:
                                time.sleep(repeat_delay_s)

                        avg_bps = sum(s["bytes_per_sec"] for s in run_stats) / len(run_stats)
                        avg_rtt = sum(s["sender_rtt_avg_ms"] for s in run_stats) / len(run_stats)
                        avg_retx = sum(s["sender_retransmits"] for s in run_stats) / len(run_stats)
                        results.append(
                            {
                                "label": label,
                                "avg_bps": avg_bps,
                                "avg_rtt_ms": avg_rtt,
                                "avg_retx": avg_retx,
                                "runs": run_stats,
                            }
                        )
                        print(
                            f"{label} -> avg_bps {avg_bps:.1f}, "
                            f"avg_rtt_ms {avg_rtt:.0f}, avg_retx {avg_retx:.1f}"
                        )

    if results:
        best = max(results, key=lambda r: r["avg_bps"])
        print("\nBest configuration:")
        print(
            f"{best['label']} -> avg_bps {best['avg_bps']:.1f}, "
            f"avg_rtt_ms {best['avg_rtt_ms']:.0f}, avg_retx {best['avg_retx']:.1f}"
        )
    if report_file:
        import json
        with open(report_file, "w", encoding="utf-8") as handle:
            json.dump(results, handle, indent=2)
        print(f"Report saved to {report_file}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Meshtastic radio test tool")
    parser.add_argument("--auto", action="store_true", help="Auto-select first two radios")
    parser.add_argument(
        "--mode",
        choices=sorted(MODE_PRESETS.keys()),
        help="Run a single bandwidth test in the selected mode and exit",
    )
    parser.add_argument(
        "--no-prompt-mode",
        action="store_true",
        help="Disable the startup mode prompt",
    )
    parser.add_argument(
        "--bytes-len",
        type=int,
        default=2048,
        help="Bytes to send per bandwidth test",
    )
    parser.add_argument(
        "--test-timeout",
        type=float,
        default=60.0,
        help="Timeout per bandwidth test (seconds)",
    )
    parser.add_argument(
        "--repeat-count",
        type=int,
        default=3,
        help="Number of repeats per configuration in report mode",
    )
    parser.add_argument(
        "--repeat-delay",
        type=float,
        default=1.0,
        help="Delay between repeated tests (seconds)",
    )
    parser.add_argument(
        "--sweep-window-sizes",
        help="Comma-separated window sizes for report mode",
    )
    parser.add_argument(
        "--sweep-retransmit-ms",
        help="Comma-separated retransmit timeouts (ms) for report mode",
    )
    parser.add_argument(
        "--sweep-ack-every",
        help="Comma-separated ack_every_n values for report mode",
    )
    parser.add_argument(
        "--sweep-ack-interval",
        help="Comma-separated ack interval seconds for report mode",
    )
    parser.add_argument(
        "--sweep-nack-interval",
        help="Comma-separated nack interval seconds for report mode",
    )
    parser.add_argument(
        "--report-file",
        help="Write report results to a JSON file",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Run report mode without prompting",
    )
    parser.add_argument(
        "--scan-timeout",
        type=float,
        default=10.0,
        help="Total timeout for scanning serial ports (seconds)",
    )
    parser.add_argument(
        "--port-timeout",
        type=float,
        default=2.0,
        help="Per-port timeout while probing for Meshtastic (seconds)",
    )
    parser.add_argument(
        "--ports",
        help="Comma-separated serial ports to use (bypass auto-detection)",
    )
    parser.add_argument(
        "--log-level",
        default="DEBUG",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level",
    )
    parser.add_argument(
        "--log-file",
        help="Log file path (optional)",
    )
    parser.add_argument(
        "--no-debug-frames",
        action="store_true",
        help="Disable decoded frame logging",
    )
    parser.add_argument(
        "--meshtastic-debug",
        action="store_true",
        help="Enable meshtastic library debug logs",
    )
    parser.add_argument(
        "--show-handshake-logs",
        action="store_true",
        help="Show stream setup logs before tests start",
    )
    args = parser.parse_args(argv)

    setup_logging(level=args.log_level, log_file=args.log_file)
    if not args.meshtastic_debug:
        logging.getLogger("meshtastic").setLevel(logging.WARNING)

    if args.ports:
        radios = []
        for port in [p.strip() for p in args.ports.split(",") if p.strip()]:
            info = probe_meshtastic_port(port, args.port_timeout)
            if info:
                radios.append(info)
            else:
                logger.warning("Port %s did not respond as Meshtastic", port)
    else:
        radios = list_meshtastic_ports(
            timeout=args.scan_timeout,
            per_port_timeout=args.port_timeout,
        )

    if len(radios) < 2:
        manual = input(
            "Auto-detect found fewer than two radios. Enter ports (comma-separated) or press Enter to exit: "
        ).strip()
        if manual:
            radios = []
            for port in [p.strip() for p in manual.split(",") if p.strip()]:
                info = probe_meshtastic_port(port, args.port_timeout)
                if info:
                    radios.append(info)
                else:
                    logger.warning("Port %s did not respond as Meshtastic", port)
    if len(radios) < 2:
        print("Need at least two Meshtastic radios connected.")
        return 1
    gateway_info, client_info = select_two_radios(radios) if not args.auto else (radios[0], radios[1])

    print(f"Gateway: {gateway_info.port} (node {gateway_info.node_id:#x})")
    print(f"Client:  {client_info.port} (node {client_info.node_id:#x})")

    run_ui = False
    run_compare = False
    run_report_mode = False

    sweep_requested = any(
        [
            args.sweep_window_sizes,
            args.sweep_retransmit_ms,
            args.sweep_ack_every,
            args.sweep_ack_interval,
            args.sweep_nack_interval,
            args.report_file,
        ]
    )

    if args.report or sweep_requested:
        run_report_mode = True
    elif not args.no_prompt_mode and not args.mode:
        test_type = prompt_test_type(default_index=1)
        if test_type == "ui":
            run_ui = True
        elif test_type == "compare":
            run_compare = True
        elif test_type == "report":
            run_report_mode = True
        else:
            selected = prompt_radio_mode(default_index=6)
            for key, preset_name in MODE_PRESETS.items():
                if preset_name == selected:
                    args.mode = key
                    break
    elif not args.mode:
        run_ui = True

    if not args.show_handshake_logs:
        logging.disable(logging.CRITICAL)

    gateway_iface = SerialInterface(gateway_info.port)
    client_iface = SerialInterface(client_info.port)

    link = None
    if not run_report_mode:
        link = setup_stream_link(
            gateway_iface,
            client_iface,
            debug_frames=not args.no_debug_frames,
        )

    if not args.show_handshake_logs:
        logging.disable(logging.NOTSET)

    try:
        if run_report_mode:
            default_config = Config()
            window_sizes = _parse_list(
                args.sweep_window_sizes, int, [default_config.window_size]
            )
            retransmit_ms = _parse_list(
                args.sweep_retransmit_ms, int, [default_config.retransmit_timeout_ms]
            )
            ack_every = _parse_list(args.sweep_ack_every, int, [4])
            ack_interval = _parse_list(args.sweep_ack_interval, float, [0.5])
            nack_interval = _parse_list(args.sweep_nack_interval, float, [0.5])

            run_report(
                gateway_iface=gateway_iface,
                client_iface=client_iface,
                bytes_len=args.bytes_len,
                timeout_s=args.test_timeout,
                repeat_count=args.repeat_count,
                repeat_delay_s=args.repeat_delay,
                window_sizes=window_sizes,
                retransmit_timeouts_ms=retransmit_ms,
                ack_every_n_values=ack_every,
                ack_interval_s_values=ack_interval,
                nack_interval_s_values=nack_interval,
                debug_frames=not args.no_debug_frames,
                show_handshake_logs=args.show_handshake_logs,
                report_file=args.report_file,
            )
            return 0
        if run_compare:
            run_bandwidth_comparison(link, client_iface, gateway_iface)
            return 0
        if args.mode:
            preset_name = MODE_PRESETS[args.mode]
            label = MODE_LABELS.get(args.mode, args.mode)
            print(f"Setting mode: {label} ({preset_name})")
            set_modem_preset(client_iface, preset_name)
            set_modem_preset(gateway_iface, preset_name)
            time.sleep(3.0)
            link.suppress_rx_output = True
            try:
                bw = measure_bandwidth(link.client_stream, link.gateway_stream)
            finally:
                link.suppress_rx_output = False
            print(
                "Bandwidth results:"
                f"\n- bytes_sent: {bw['bytes_sent']}"
                f"\n- bytes_received: {bw['bytes_received']}"
                f"\n- duration_s: {bw['duration_s']:.3f}"
                f"\n- bytes_per_sec: {bw['bytes_per_sec']:.1f}"
                f"\n- sender_frames_sent: {bw['sender_frames_sent']}"
                f"\n- receiver_frames_received: {bw['receiver_frames_received']}"
                f"\n- sender_retransmits: {bw['sender_retransmits']}"
                f"\n- receiver_retransmits: {bw['receiver_retransmits']}"
                f"\n- recv_timeouts: {bw['recv_timeouts']}"
                f"\n- sender_rtt_avg_ms: {bw['sender_rtt_avg_ms']:.1f}"
                f"\n- sender_rtt_max_ms: {bw['sender_rtt_max_ms']:.1f}"
                f"\n- sender_max_pending: {bw['sender_max_pending']}"
            )
        else:
            interactive_ui(link, gateway_iface, client_iface)
    finally:
        if link:
            link.retransmit_timer.stop()
            if link.on_rx:
                pub.unsubscribe(link.on_rx, "meshtastic.receive")
        gateway_iface.close()
        client_iface.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
