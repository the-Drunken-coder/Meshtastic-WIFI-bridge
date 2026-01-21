from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve()
while ROOT != ROOT.parent and not (ROOT / "src").exists():
    ROOT = ROOT.parent
SRC = ROOT / "src"
if SRC.exists() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from transport import MeshtasticTransport


def retarget_spool_destination(transport: MeshtasticTransport, destination: str) -> None:
    spool = getattr(transport, "spool", None)
    if not spool or not hasattr(spool, "_entries"):
        return
    updated = 0
    try:
        for entry in spool._entries.values():  # type: ignore[attr-defined]
            if entry.destination != destination:
                entry.destination = destination
                updated += 1
        if updated and hasattr(spool, "_flush"):
            spool._flush()  # type: ignore[attr-defined]
            logging.info("Retargeted %d pending spool entries to %s", updated, destination)
    except Exception:
        logging.debug("Failed to retarget spool entries", exc_info=True)


def wait_for_quiet(
    transport: MeshtasticTransport,
    quiet_window: float,
    max_wait: float,
    stop_event: threading.Event,
) -> bool:
    if quiet_window <= 0 or max_wait <= 0:
        return True
    deadline = time.time() + max_wait
    quiet_deadline = time.time() + quiet_window
    while time.time() < deadline and not stop_event.is_set():
        remaining = min(0.5, max(0.1, quiet_deadline - time.time()))
        sender, message = transport.receive_message(timeout=remaining)
        if message is None:
            if time.time() >= quiet_deadline:
                return True
            continue
        quiet_deadline = time.time() + quiet_window
    return False


def wait_for_settled(
    transport: MeshtasticTransport,
    quiet_window: float,
    max_wait: float,
    stop_event: threading.Event,
) -> bool:
    if quiet_window <= 0 or max_wait <= 0:
        return True
    deadline = time.time() + max_wait
    quiet_deadline = time.time() + quiet_window
    while time.time() < deadline and not stop_event.is_set():
        remaining = min(0.5, max(0.1, quiet_deadline - time.time()))
        sender, message = transport.receive_message(timeout=remaining)
        if message is None:
            if time.time() >= quiet_deadline and _spool_empty(transport):
                return True
            if time.time() >= quiet_deadline:
                quiet_deadline = time.time() + quiet_window
            continue
        quiet_deadline = time.time() + quiet_window
    return False


def ack_spool_entry(transport: MeshtasticTransport, message_id: str) -> None:
    spool = getattr(transport, "spool", None)
    if not spool or not hasattr(spool, "ack"):
        return
    try:
        spool.ack(message_id)
    except Exception:
        logging.debug("Failed to ack spool entry %s", message_id, exc_info=True)


def clear_spool(transport: MeshtasticTransport) -> None:
    spool = getattr(transport, "spool", None)
    if not spool or not hasattr(spool, "_entries"):
        return
    try:
        spool._entries.clear()  # type: ignore[attr-defined]
        flush = getattr(spool, "_flush", None)
        if callable(flush):
            flush()
        logging.info("Cleared spool at %s", getattr(spool, "_path", "<unknown>"))
    except Exception:
        logging.debug("Failed to clear spool", exc_info=True)


def _spool_empty(transport: MeshtasticTransport) -> bool:
    spool = getattr(transport, "spool", None)
    if not spool:
        return True
    depth_fn = getattr(spool, "depth", None)
    if not callable(depth_fn):
        return True
    try:
        return depth_fn() == 0
    except Exception:
        return True


__all__ = [
    "ack_spool_entry",
    "clear_spool",
    "retarget_spool_destination",
    "wait_for_quiet",
    "wait_for_settled",
]
