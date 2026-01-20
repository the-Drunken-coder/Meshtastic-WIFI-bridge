# LoRa WiFi Forwarder

A low-bandwidth internet proxy over Meshtastic LoRa mesh networks.

## Overview

LoRa WiFi Forwarder enables remote devices to access limited internet connectivity through a LoRa mesh network. A **client node** (Raspberry Pi + Meshtastic radio) provides WiFi access point for local devices (phones/laptops), tunneling their HTTP CONNECT proxy requests through the mesh to a **gateway node** that has real internet connectivity.

**Important**: This is NOT full internet. It's a low-bandwidth gateway suited for text browsing, messaging, telemetry, and small requests.

## Architecture

```
┌─────────────────────┐         LoRa Mesh          ┌─────────────────────┐
│   Client Node       │◄────────────────────────►│   Gateway Node       │
│  (Pi + Radio + AP)  │                           │  (Pi + Radio + Net)  │
│                     │                           │                      │
│  ┌───────────────┐  │                           │  ┌────────────────┐  │
│  │   clientd     │  │                           │  │    gatewayd    │  │
│  │ (HTTP Proxy)  │  │                           │  │ (TCP Gateway)  │  │
│  └───────────────┘  │                           │  └────────────────┘  │
│         ▲           │                           │         │            │
│         │           │                           │         ▼            │
│  Local WiFi AP      │                           │     Internet         │
│         │           │                           │                      │
│    ┌────┴────┐      │                           └──────────────────────┘
│    │ Phone/  │      │
│    │ Laptop  │      │
│    └─────────┘      │
└─────────────────────┘
```

## Features

- **HTTP CONNECT Proxy**: Tunnel TCP connections (HTTPS) over LoRa mesh
- **Reliable Transport**: Sliding window, retransmission, ACK/NACK handling
- **Multiplexed Streams**: Multiple concurrent connections over single radio link
- **CRC32 Integrity**: Frame validation to detect corruption
- **Backpressure**: Flow control to prevent mesh congestion
- **Structured Logging**: Clear visibility into stream lifecycle

## Installation

### Requirements

- Python 3.8+
- Raspberry Pi (or similar Linux device)
- Meshtastic-compatible LoRa radio (Heltec V3, T-Beam, etc.)

### Install

```bash
# Clone the repository
git clone <repository-url>
cd Meshtastic-WIFI-bridge

# Install dependencies
pip install -r requirements.txt

# Or install as package
pip install -e .
```

## Usage

### Gateway Node (has internet connectivity)

```bash
gatewayd --serial /dev/ttyUSB0 --internet-iface wlan0

# With options
gatewayd --serial /dev/ttyUSB0 --internet-iface eth0 --log-level DEBUG
```

### Client Node (provides WiFi AP)

```bash
# Get gateway node ID from gatewayd startup logs, e.g., 0x12345678
clientd --serial /dev/ttyUSB0 --listen 0.0.0.0:3128 --gateway-node-id !12345678

# With options
clientd --serial /dev/ttyUSB0 --listen 0.0.0.0:3128 --gateway-node-id 0x12345678 --log-level DEBUG
```

### Configure Client Device

Set your phone/laptop HTTP proxy to the client node's IP and port 3128.

Example for curl:
```bash
https_proxy=http://192.168.4.1:3128 curl https://example.com
```

## CLI Options

### gatewayd

| Option | Default | Description |
|--------|---------|-------------|
| `--serial` | `/dev/ttyUSB0` | Serial port for Meshtastic device |
| `--internet-iface` | `wlan0` | Network interface with internet |
| `--log-level` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |
| `--log-file` | None | Log file path |
| `--window-size` | `4` | Sliding window size |
| `--retransmit-timeout` | `5000` | Retransmit timeout (ms) |

### clientd

| Option | Default | Description |
|--------|---------|-------------|
| `--serial` | `/dev/ttyUSB0` | Serial port for Meshtastic device |
| `--listen` | `0.0.0.0:3128` | Proxy listen address |
| `--gateway-node-id` | Required | Gateway node ID (hex or decimal) |
| `--log-level` | `INFO` | Logging level |
| `--log-file` | None | Log file path |
| `--window-size` | `4` | Sliding window size |
| `--retransmit-timeout` | `5000` | Retransmit timeout (ms) |

## On-Wire Frame Format

Each frame transmitted over LoRa uses this binary format:

| Offset | Size | Field | Description |
|--------|------|-------|-------------|
| 0 | 4 bytes | stream_id | Unique stream identifier (uint32 LE) |
| 4 | 4 bytes | seq | Sequence number (uint32 LE) |
| 8 | 4 bytes | ack | Acknowledgment number (uint32 LE) |
| 12 | 1 byte | flags | Frame flags (bitfield) |
| 13 | 2 bytes | payload_len | Payload length (uint16 LE) |
| 15 | N bytes | payload | Actual payload data |
| 15+N | 4 bytes | crc32 | CRC32 of frame (uint32 LE) |

### Frame Flags

| Bit | Name | Description |
|-----|------|-------------|
| 0 | SYN | Stream synchronization (open) |
| 1 | FIN | Stream finish (close) |
| 2 | RST | Stream reset (abort) |
| 3 | ACK | Acknowledgment |
| 4 | NACK | Negative acknowledgment |

## Project Structure

```
├── common/           # Configuration and logging utilities
│   ├── config.py     # Configuration settings
│   └── logging_setup.py  # Structured logging
├── framing/          # Packet format and encoding
│   ├── frame.py      # Frame data structure
│   └── codec.py      # Encode/decode functions
├── reliability/      # Reliable transport layer
│   ├── window.py     # Sliding window implementation
│   ├── retransmit.py # Retransmission timer
│   └── stream.py     # Stream abstraction
├── transport/        # Meshtastic abstraction
│   └── meshtastic_transport.py
├── client/           # Client daemon and proxy
│   ├── daemon.py     # clientd entry point
│   ├── proxy_server.py   # HTTP CONNECT proxy
│   └── stream_manager.py # Client stream management
├── gateway/          # Gateway daemon
│   ├── daemon.py     # gatewayd entry point
│   └── stream_manager.py # Gateway stream management
└── tests/            # Test suite
    ├── test_framing.py
    ├── test_reliability.py
    └── test_lossy_channel.py
```

## Testing

```bash
# Run all tests
python -m pytest tests/ -v

# Run specific test file
python -m pytest tests/test_framing.py -v

# Run with coverage
python -m pytest tests/ --cov=. --cov-report=term-missing
```

### Test Plan

1. **Loopback tests**: Frame encode/decode roundtrip validation
2. **Lossy channel simulation**: Test reliability with simulated drops/reordering
3. **Real radio smoke test**: Send stream across 2 physical nodes

## Configuration

Default configuration values in `common/config.py`:

```python
chunk_payload_size = 180  # Bytes per chunk (conservative for LoRa)
window_size = 4           # Sliding window size
retransmit_timeout_ms = 5000  # 5 second retransmit timeout
max_retransmits = 5       # Max retransmit attempts
stream_timeout_s = 120    # Stream timeout (2 minutes)
```

## Non-Goals (MVP)

- ❌ Full IP tunnel (TUN/TAP)
- ❌ QUIC protocol
- ❌ Transparent proxying
- ❌ MITM TLS interception
- ❌ Fancy UI
- ❌ Mesh routing changes (Meshtastic handles that)

## License

[Add your license here]

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.
