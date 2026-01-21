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

## Quick start (simulated radios)

Terminal 1 - start a gateway:

```bash
python cli.py \
  --mode gateway \
  --gateway-node-id gw-1 \
  --simulate-radio \
  --node-id gw-1
```

Terminal 2 - send a request:

```bash
python cli.py \
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

## Notes

- The binary protocol is described in `docs/SYSTEMS_DESIGN.md`.
- The gateway exposes default handlers for `echo`, `payload_digest`, and `health`.
- For large payload testing, prefer the harness `payload_digest` flow so the
  response stays small.
