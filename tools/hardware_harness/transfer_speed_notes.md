# Meshtastic Harness Transfer Speed Notes

- Scenario: 10 KB `payload_digest` run over dual-radio harness took ~68 s because chunk 3/6 was lost; NACKs started at +13 s, resent chunk landed at +34 s, and the 1-chunk response finally arrived ~31 s after the gateway sent it.
- Impact: Recovery depends on mid-stream NACKs; if a NACK or the resend is lost, completion stalls until a later retry or timeout.

## Current simplified defaults
- Moderate NACK settings (`nack_max_per_seq`=3, `nack_interval`=1.0) and no pacing by default (`chunk_delay_threshold`/`chunk_delay_seconds` disabled) to reduce chatter.
- Completion markers and periodic NACK driving removed; only gap-triggered NACKs are used.
- Client timeout still resets on progress with an overall cap to avoid premature retries.

## Packetization
- `SEGMENT_SIZE` is 210 (fits under ~230 bytes on-air) to keep chunk count low.
- Per-chunk TTL extensions remain capped to avoid lingering stale buckets.

## RF-side knobs
- Each chunk transmit took ~1 s; if range allows, use a faster LoRa profile (higher bandwidth/lower spreading factor) and minimal hop limit to shrink airtime.

## Validation ideas
- Re-run the 10 KB upload after increasing NACK aggressiveness to confirm tail latency drops.
- Add metrics/logs for “time from first NACK to resend received” and “response resend count” to spot regressions quickly.
