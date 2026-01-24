# Meshtastic Bridge

Reliable chunking and reassembly for sending larger payloads over Meshtastic links.
This package focuses on the transport layer: binary envelopes, chunk splitting,
ACK/NACK reliability strategies, deduplication, and optional spooling.

## Features

- Binary envelopes (MessagePack + Zstandard) with compact headers
- Chunking/reassembly tuned for Meshtastic payload limits
- Reliability strategies (simple ACK, staged, windowed selective, parity window)
- Deduplication and semantic-key support to avoid replayed requests
- Optional on-disk spool for retrying outgoing messages
- In-memory radio for local simulation/testing
- Hardware harness for manual testing with real radios
- Interactive TUI with command palette for radio and mode selection
- **Web browser UI** for browsing websites over the mesh network
- Port accessibility detection to verify radio connections
- JSON-based mode configuration for transport parameters

## Install as npm CLI

This project ships a small Node wrapper so you can run the Python CLI via an
`npm`-installed command.

```bash
# Install from npm registry (recommended)
npm install -g meshtastic-bridge

# Or, from a cloned checkout of this repository:
npm install -g .

# Ensure Python dependencies are installed:
pip install -r requirements.txt

# Run the CLI:
meshbridge
```

Set `MESHTASTIC_BRIDGE_PYTHON` if you need to point at a specific Python executable.
The UI uses the Meshtastic Python stack, so ensure `requirements.txt` is installed.

## Interactive TUI

The `meshbridge` command launches an interactive terminal UI that provides:

- **Real-time monitoring**: View radio connection status, gateway traffic, and message payloads
- **Command palette** (Ctrl+P): Access all commands and settings
- **Radio port selection**: Choose which serial port to connect to from detected and accessible ports
- **Mode configuration**: Switch between transport modes with different reliability strategies and parameters
- **Gateway/client switching**: Start gateway service or send client requests via the command palette

### TUI Key Bindings

- `Ctrl+P` - Open command palette
- `↑/↓` or `j/k` - Navigate palette options
- `Enter` - Select palette option
- `Esc` - Close palette / Cancel
- `Ctrl+C` - Quit application

### Mode Configuration

Transport modes are defined in JSON files in the `modes/` directory. Each mode can customize:
- Chunk size and header format
- ACK/NACK reliability strategy (simple, staged, windowed, parity)
- Timeout and retry parameters
- Compression settings

Create custom modes by adding new JSON files to the `modes/` directory.

## Web Browser

When you select **Open Client** in the TUI, a web browser server automatically starts on `http://127.0.0.1:8080`. This allows you to browse websites over your Meshtastic mesh network.

### How it works

1. Run `meshbridge` and select **Open Client**
2. Enter the Gateway Node ID in the TUI
3. Open `http://127.0.0.1:8080` in your web browser
4. Enter any URL (e.g., `example.com`) in the search bar
5. The page is fetched through the gateway over the mesh and displayed

### Features

- Real-time progress tracking with chunk counts and ETA
- Automatic URL normalization (adds `https://` if missing)
- Relative link handling via `<base>` tag injection
- Click-through navigation (links are fetched through the mesh)
- Works best with simple, text-based websites

### Standalone Usage

You can also run the web browser directly without the TUI:

```bash
python ui_service/web_ui.py --gateway-node-id !abcd1234
```

Or via the CLI:

```bash
python scripts/cli.py \
  --mode client \
  --gateway-node-id !abcd1234 \
  --web-browser
```

## Quick start (simulated radios)

Terminal 1 - start a gateway:

```bash
python scripts/cli.py \
  --mode gateway \
  --gateway-node-id gw-1 \
  --simulate-radio \
  --node-id gw-1
```

Terminal 2 - send a request:

```bash
python scripts/cli.py \
  --mode client \
  --gateway-node-id gw-1 \
  --simulate-radio \
  --node-id client-1 \
  --command echo \
  --data '{"message":"hello"}'
```

## Hardware harness

The manual harness lives in `tools/hardware_harness`. It spins up a gateway and
client on two radios (or simulation), then provides an interactive menu for
echo and payload-digest tests.

```bash
python tools/hardware_harness/dual_radio_harness.py
```

## CLI flags

| Flag | Description |
| --- | --- |
| `--mode {gateway,client}` | Run as gateway or client (required). |
| `--gateway-node-id` | Meshtastic node ID of the gateway (required). |
| `--simulate-radio` | Use in-memory radio instead of hardware. |
| `--radio-port` | Serial port path (hardware mode). |
| `--node-id` | Override local node ID (default: `gateway` or `client`). |
| `--command` | Client command to run (client mode). |
| `--data` | JSON payload for the command (client mode). |
| `--timeout` | Client request timeout in seconds (default: 5). |
| `--spool-path` | Path for persistent outgoing message spool. |
| `--log-level` | Logging level (default: `INFO`). |
| `--web-browser` | Start web browser UI for browsing over mesh (client mode). |
| `--web-host` | Host for web browser UI (default: `127.0.0.1`). |
| `--web-port` | Port for web browser UI (default: `8080`). |

## Notes

- The binary protocol is described in `docs/SYSTEMS_DESIGN.md`.
- The gateway exposes default handlers for `echo`, `payload_digest`, and `health`.
- For large payload testing, prefer the harness `payload_digest` flow so the
  response stays small.
