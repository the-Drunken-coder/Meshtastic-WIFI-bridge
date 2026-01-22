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
    chunk_ttl_per_chunk: float,
    chunk_ttl_max: float,
    chunk_delay_threshold: int | None,
    chunk_delay_seconds: float,
    nack_max_per_seq: int,
    nack_interval: float,
    disable_dedupe: bool = False,
    dedupe_lease_seconds: float | None = None,
    segment_size: int = 200,
) -> MeshtasticTransport:
    os.makedirs(spool_dir, exist_ok=True)
    radio = build_radio(simulate, port, node_id, disable_dedupe=disable_dedupe)
    spool_path = os.path.join(spool_dir, f"{spool_name}_spool.json")
    return MeshtasticTransport(
        radio,
        spool_path=spool_path,
        disable_dedupe=disable_dedupe,
        dedupe_lease_seconds=dedupe_lease_seconds,
        segment_size=segment_size,
        chunk_ttl_per_chunk=chunk_ttl_per_chunk,
        chunk_ttl_max=chunk_ttl_max,
        chunk_delay_threshold=chunk_delay_threshold,
        chunk_delay_seconds=chunk_delay_seconds,
        nack_max_per_seq=nack_max_per_seq,
        nack_interval=nack_interval,
    )


def start_gateway(
    transport: MeshtasticTransport,
) -> Tuple[MeshtasticGateway, threading.Thread]:
    gateway = MeshtasticGateway(transport)

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
