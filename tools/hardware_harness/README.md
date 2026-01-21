# Meshtastic Bridge Hardware Harness

This directory contains a **manual testing harness** for the Meshtastic bridge. It is **not**
packaged with the library; it is intended for developers to exercise the bridge against real radios.

## Assumptions

- Two Meshtastic radios are plugged into the laptop running this harness (or use simulation).
- `meshtastic-python` is installed so the gateway can talk to the radios.

## Quick start

```bash
python tools/hardware_harness/dual_radio_harness.py
```

Config file (JSON, default path: `tools/hardware_harness/config.json`):

```json
{
  "gateway_port": "/dev/ttyUSB0",
  "client_port": "/dev/ttyUSB1",
  "gateway_node_id": "!abcdef12",
  "client_node_id": "!1234abcd",
  "simulate": false,
  "timeout": 30.0,
  "retries": 2,
  "post_response_quiet": 10.0,
  "post_response_timeout": 90.0,
  "loop": false,
  "clear_spool": false,
  "spool_dir": "~/.meshtastic_bridge_harness"
}
```

When the harness starts it will:

1. Spin up a gateway process using the gateway radio.
2. Connect a client instance to the second radio.
3. Present a terminal menu so you can choose a command (echo, payload digest, custom JSON).
4. Send the client request over the mesh; the gateway responds with the handler output.

Press `q` to exit the harness; it will shut down both radios and the gateway thread.

## Notes

- Use the `--simulate` flag to run without hardware; this is helpful for dry runs but will not hit
  the real radios.
- Spool files are stored under `~/.meshtastic_bridge_harness/` to avoid interfering with other runs.
- The harness supports a payload digest command that can be used for large transfers without
  echoing the full payload back over the mesh.
