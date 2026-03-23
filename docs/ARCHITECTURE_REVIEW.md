# Architecture Review

This document is a maintainer-focused review of the current project structure.
It is intended to capture the higher-level issues that should be considered
before adding major new features or upgrade work.

## Current shape of the project

The repository already has a good transport-focused core:

- `src/message.py` owns the binary envelope, compression, and chunk framing.
- `src/reassembly.py` and `src/reliability.py` implement the receive/retry rules.
- `src/transport.py` coordinates chunk send/receive, ACK/NACK handling, spool
  integration, deduplication, and metrics.
- `src/gateway.py` and `src/client.py` add request/response behavior on top of
  the transport.
- `ui_service/` adds a Rich TUI plus a Flask-powered web browser interface.

The existing automated coverage is strong for protocol behavior and message
handling (`pytest` currently passes with `107 passed`), so the main risks now
are architectural rather than basic correctness.

## What is working well

- The transport protocol itself is clearly separated from the UI entry points.
- The test suite provides strong regression coverage for chunking, reassembly,
  reliability strategies, deduplication, spool behavior, and the web UI.
- `InMemoryRadio` and the simulation-oriented tests make protocol work easier to
  change safely than a hardware-only design would.
- `docs/SYSTEMS_DESIGN.md` already explains the wire protocol well.

## Higher-level issues to address before major upgrades

### 1. `MeshtasticTransport` is effectively the system's god object

`src/transport.py` centralizes transport orchestration, cache management,
cleanup, resend behavior, spool bookkeeping, progress tracking, and metrics.
That makes it the hardest file to safely evolve because many concerns are
coupled together in one mutable object.

Relevant code:

- `src/transport.py:101-203`
- `src/transport.py:204-252`
- `src/transport.py:253-832`

Why this matters:

- New features will keep landing in the same class unless the boundaries are
  improved first.
- Behavior is harder to reason about because send, receive, cleanup, retry, and
  persistence decisions are interleaved.

Recommended direction:

- Keep `MeshtasticTransport` as an orchestration layer only.
- Move mutable sub-systems behind narrower interfaces:
  - outbound queue / scheduler
  - inbound session tracking
  - chunk resend cache
  - metrics adapter
  - persistence adapter

### 2. The transport has no explicit concurrency contract

`MeshtasticTransport` stores shared mutable state in dictionaries and lists such
as `_chunk_cache`, `_last_progress`, `_active_chunks`, and `_inbound_sessions`
without any synchronization. At the same time, `ui_service/backend_service.py`
starts background threads and wraps the transport for long-lived use.

Relevant code:

- `src/transport.py:162-200`
- `ui_service/backend_service.py:164-205`

Why this matters:

- If future upgrades add more background work, thread-based handlers, or
  multiple producers, race conditions become likely.
- Even if the intended model is single-threaded, the repository does not state
  that clearly enough for contributors.

Recommended direction:

- Pick one model and document it:
  - **Single-threaded core**: preferred for simplicity; all transport access
    happens on one loop/thread and outer layers communicate via queues.
  - **Thread-safe transport**: only if required; add explicit locking and tests
    for concurrent access.

Short-term recommendation: treat the transport as **single-thread-affine** and
move cross-thread communication to bounded queues in the UI/service layer.

### 3. Some APIs look non-blocking but still perform blocking work

The project mixes queue-like naming with direct transmission loops and sleep-
based pacing. That makes it easy to accidentally call transport methods from a
latency-sensitive path and block the caller.

Relevant code:

- `src/transport.py` send path and pacing logic
- `src/client.py` request flow

Why this matters:

- Future upgrades such as richer UI actions, streaming payloads, or automation
  hooks will be harder to schedule predictably.
- It prevents a clean split between "accept work" and "perform radio I/O".

Recommended direction:

- Separate API layers into:
  - command submission (`enqueue` / queue write)
  - scheduler / worker loop
  - radio adapter send/receive
- Prefer a single event loop or worker thread with bounded queues over calling
  time-based transmission logic directly from UI actions.

### 4. The repo mixes protocol, hardware integration, and product UI too tightly

This repository currently contains:

- the reusable transport core
- Meshtastic hardware integration
- CLI/TUI management
- a Flask web browser proxy
- npm packaging glue

Why this matters:

- The upgrade path is unclear: are future changes intended for a library, a
  desktop tool, a service, or all of them?
- Packaging, runtime, and dependency decisions for one layer affect every other
  layer.

Recommended direction:

- Preserve the current mono-repo, but treat it as separate layers:
  1. **Core transport library** (`src/`)
  2. **Meshtastic adapter/runtime layer** (`src/radio.py`, CLI glue)
  3. **Product surfaces** (`ui_service/`, Node wrapper)

Before major upgrades, avoid adding new UI-specific behavior directly into the
transport core unless it is protocol state that must live there.

### 5. The radio integration is fragile because it relies on monkey-patching

`src/radio.py` patches Meshtastic library behavior to handle initialization
failures. That may be necessary today, but it is a long-term maintenance risk.

Why this matters:

- Upgrading the upstream Meshtastic dependency could break startup behavior in a
  hard-to-diagnose way.
- It makes the adapter layer harder to replace or test independently.

Recommended direction:

- Isolate Meshtastic-specific workarounds into a thin adapter boundary.
- Keep all upstream compatibility logic in one place.
- If the project grows, consider making the radio adapter swappable by interface
  rather than letting Meshtastic assumptions spread deeper into the transport.

### 6. Persistence and back-pressure are under-specified

The spool and retry behavior are useful, but the higher-level resource policy is
not yet clearly bounded. There is no strong architectural statement about queue
limits, memory ceilings, or how the system should behave when radio throughput
falls behind message production.

Why this matters:

- Upgrades involving file transfer, richer browsing, or automation can create
  bursts that exceed available radio bandwidth.
- Without bounded queues and clear overflow behavior, reliability features can
  turn into memory-pressure problems.

Recommended direction:

- Add explicit resource limits before expanding payload-heavy features:
  - max queued outbound messages
  - max active inbound sessions
  - max retained resend cache size
  - spool write policy and corruption recovery expectations
- Define whether the system should drop, reject, delay, or persist work when
  limits are reached.

### 7. Test coverage is strong at the protocol level, but light on architecture boundaries

The tests show that the wire behavior is in good shape. What they do not define
as strongly is the intended runtime model around threading, scheduler behavior,
service lifecycle, and failure isolation between the transport core and UI.

Why this matters:

- Architectural refactors become risky because the contract between layers is
  not written down as clearly as packet-level behavior is.

Recommended direction:

- Keep the current protocol-heavy tests.
- Add targeted architectural tests only when boundaries are formalized, such as:
  - queue/back-pressure behavior
  - service loop ownership of transport access
  - persistence recovery semantics
  - adapter failure isolation

## Upgrade guidance

If the next round of work involves major capability expansion, the safest path
is likely:

1. **Freeze the transport contract**
   - Treat the current message/chunk protocol as stable unless a protocol change
     is truly required.
2. **Clarify the runtime model**
   - Decide whether the application is a single-threaded service loop with UI
     clients, or a thread-safe shared object model.
3. **Introduce clearer boundaries**
   - Keep core transport state isolated from UI state, HTTP behavior, and
     Meshtastic-specific connection management.
4. **Bound resources before adding throughput-heavy features**
   - Add queue limits, session limits, and spool policy.
5. **Refactor adapters before deep feature work**
   - Especially the radio adapter and packaging/runtime entry points.

## Suggested target architecture

The most maintainable direction for future upgrades would be:

- **Protocol core**
  - envelope encoding/decoding
  - chunking/reassembly
  - retry strategy decisions
- **Stateful service loop**
  - owns all transport mutation
  - processes inbound/outbound work from queues
  - records metrics/events
- **Adapters**
  - Meshtastic serial adapter
  - spool storage adapter
  - UI/HTTP presentation adapters
- **Presentation layer**
  - TUI
  - browser UI
  - CLI commands

That architecture keeps the proven protocol logic while avoiding the current
pattern where one object carries too much system responsibility.

## Immediate low-risk improvements

These are good first upgrades because they reduce long-term risk without
requiring a protocol rewrite:

- Document that `MeshtasticTransport` must currently be treated as
  single-thread-affine.
- Introduce bounded queues between the UI/service threads and transport access.
- Extract radio compatibility code into a narrower adapter boundary.
- Document spool/resource policies before expanding browser or file-transfer
  features.
- Add one maintainer-facing architecture document alongside the protocol spec
  so upgrade work has a shared reference point.

## Validation

Current repository validation steps:

```bash
python -m pip install -r requirements.txt
python -m pip install pytest
pytest
```

At the time this review was added, the full test suite passed locally with:

```text
107 passed in 3.67s
```
