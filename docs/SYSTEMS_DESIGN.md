# Meshtastic Bridge Design

This document describes the transport protocol used to move larger payloads
over Meshtastic links while preserving reliability and deduplication.

## Protocol sketch

1. **Envelope (MessagePack + Zstandard)**
   ```
   {
     "id": "message-id",
     "type": "request" | "response" | "error",
     "command": "echo",
     "correlation_id": "optional",
     "data": { ... },
     "meta": { ... }
   }
   ```
   - `id` correlates request/response pairs.
   - `command` selects a gateway handler.
   - `data` carries the payload; `meta` may include a semantic dedupe key.

2. **Binary chunk header**
   Each PRIVATE_APP payload begins with a fixed 16-byte header (Meshtastic adds its
   own LoRa header outside the payload):
   - Magic: `MB` (2 bytes)
   - Version: `1` (1 byte)
   - Flags: bitfield (1 byte, `0x01` = ACK, `0x02` = NACK)
   - Message ID prefix: first 8 bytes of the ID (padded)
   - Sequence: uint16, 1-based
   - Total: uint16

3. **Chunking**
   - Compressed envelopes are split into segments to stay under Meshtastic limits.
   - LoRa frames max out at 256 bytes; Meshtastic prepends a 16-byte LoRa header
     outside the payload, so the usable app payload is ~240 bytes. We keep chunks
     below a conservative cap to avoid edge cases.
   - Receiver reassembles chunks by message ID prefix, dropping partial loads
     after a configurable TTL.
   - **Burst mode**: Chunks are sent in bursts (default: 5 chunks per burst) with
     small delays between bursts. This significantly improves throughput by reducing
     per-chunk overhead while maintaining reliability through the NACK mechanism.

4. **ACK/NACK reliability**
   - ACKs are single-packet frames with the ACK flag and the message ID in the payload.
   - NACKs carry missing sequence numbers to support selective resends.
   - Multiple strategies are available (simple, staged, windowed, parity window).

5. **Deduplication**
   - Messages are deduped on `(sender, command, id)` by default.
   - Optional semantic keys can be provided via `meta.dedupe_key` to suppress
     replays of the same logical operation.

## Running the bridge

Gateway example:

```bash
python scripts/cli.py \
  --mode gateway \
  --gateway-node-id GATEWAY_NODE \
  --simulate-radio
```

Client example:

```bash
python scripts/cli.py \
  --mode client \
  --gateway-node-id GATEWAY_NODE \
  --command echo \
  --data '{"message":"hello"}' \
  --simulate-radio
```
