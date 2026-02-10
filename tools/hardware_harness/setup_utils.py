from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path
from typing import Tuple

ROOT = Path(__file__).resolve()
while ROOT != ROOT.parent and not (ROOT / "src").exists():
    ROOT = ROOT.parent
SRC = ROOT / "src"
if SRC.exists() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from gateway import MeshtasticGateway
from radio import build_radio
from transport import MeshtasticTransport


def build_transport(
    simulate: bool,
    port: str,
    node_id: str,
    spool_dir: str,
    spool_name: str,
    *,
    chunk_ttl_per_chunk: float | None = None,
    chunk_ttl_max: float | None = None,
    chunk_delay_threshold: int | None = None,
    chunk_delay_seconds: float | None = None,
    nack_max_per_seq: int | None = None,
    nack_interval: float | None = None,
    disable_dedupe: bool = False,
    dedupe_lease_seconds: float | None = None,
    segment_size: int | None = None,
) -> MeshtasticTransport:
    os.makedirs(spool_dir, exist_ok=True)
    radio = build_radio(simulate, port, node_id, disable_dedupe=disable_dedupe)
    spool_path = os.path.join(spool_dir, f"{spool_name}_spool.json")
    transport_kwargs: dict[str, object] = {
        "spool_path": spool_path,
        "disable_dedupe": disable_dedupe,
        "dedupe_lease_seconds": dedupe_lease_seconds,
    }
    if segment_size is not None:
        transport_kwargs["segment_size"] = segment_size
    if chunk_ttl_per_chunk is not None:
        transport_kwargs["chunk_ttl_per_chunk"] = chunk_ttl_per_chunk
    if chunk_ttl_max is not None:
        transport_kwargs["chunk_ttl_max"] = chunk_ttl_max
    if chunk_delay_threshold is not None:
        transport_kwargs["chunk_delay_threshold"] = chunk_delay_threshold
    if chunk_delay_seconds is not None:
        transport_kwargs["chunk_delay_seconds"] = chunk_delay_seconds
    if nack_max_per_seq is not None:
        transport_kwargs["nack_max_per_seq"] = nack_max_per_seq
    if nack_interval is not None:
        transport_kwargs["nack_interval"] = nack_interval
    return MeshtasticTransport(radio, **transport_kwargs)


def start_gateway(
    transport: MeshtasticTransport,
    mode_config: dict | None = None,
) -> Tuple[MeshtasticGateway, threading.Thread]:
    gateway = MeshtasticGateway(transport, mode_config=mode_config)

    thread = threading.Thread(target=gateway.run_forever, daemon=True, name="meshtastic-gateway")
    thread.start()
    return gateway, thread


def close_transport(transport: MeshtasticTransport) -> None:
    radio = transport.radio
    if hasattr(radio, "close"):
        try:
            radio.close()
        except (AttributeError, OSError, RuntimeError) as exc:
            logging.warning(
                "Cleanup: failed to close radio cleanly; if the device stays busy, "
                "unplug and reconnect it. Details: %s",
                exc,
            )
