from __future__ import annotations

import logging
from typing import TYPE_CHECKING, List, Protocol

from message import (
    FLAG_ACK,
    FLAG_NACK,
    MessageEnvelope,
    build_ack_chunk,
    build_nack_chunk,
    parse_nack_payload,
)

if TYPE_CHECKING:  # pragma: no cover
    from transport import MeshtasticTransport

__all__ = [
    "ReliabilityStrategy",
    "NoAckNackStrategy",
    "SimpleAckNackStrategy",
    "StageAckNackStrategy",
    "WindowedSelectiveStrategy",
    "ParityWindowStrategy",
    "strategy_from_name",
]

logger = logging.getLogger(__name__)


class ReliabilityStrategy(Protocol):
    """Strategy interface for ACK/NACK behaviours."""

    name: str

    def on_send(
        self,
        transport: "MeshtasticTransport",
        envelope: MessageEnvelope,
        destination: str,
        total_chunks: int,
    ) -> None: ...

    def on_chunks_sent(
        self,
        transport: "MeshtasticTransport",
        envelope: MessageEnvelope,
        destination: str,
        total_chunks: int,
    ) -> None: ...

    def handle_control(
        self,
        flags: int,
        chunk_id: str,
        payload: bytes,
        sender: str,
        transport: "MeshtasticTransport",
    ) -> bool: ...
    def on_missing(
        self, sender: str, chunk_id: str, missing: List[int], transport: "MeshtasticTransport"
    ) -> None: ...
    def on_complete(
        self, sender: str, message: MessageEnvelope, transport: "MeshtasticTransport"
    ) -> None: ...


class NoAckNackStrategy:
    name = "none"

    def on_send(
        self,
        transport: "MeshtasticTransport",
        envelope: MessageEnvelope,
        destination: str,
        total_chunks: int,
    ) -> None:
        return

    def on_chunks_sent(
        self,
        transport: "MeshtasticTransport",
        envelope: MessageEnvelope,
        destination: str,
        total_chunks: int,
    ) -> None:
        return

    def handle_control(
        self,
        flags: int,
        chunk_id: str,
        payload: bytes,
        sender: str,
        transport: "MeshtasticTransport",
    ) -> bool:
        # Ignore all control flags
        if flags & (FLAG_ACK | FLAG_NACK):
            return True
        return False

    def on_missing(
        self, sender: str, chunk_id: str, missing: List[int], transport: "MeshtasticTransport"
    ) -> None:
        return

    def on_complete(
        self, sender: str, message: MessageEnvelope, transport: "MeshtasticTransport"
    ) -> None:
        return


class SimpleAckNackStrategy:
    """Minimal ACK/NACK strategy similar to the original behaviour."""

    name = "simple"

    def on_send(
        self,
        transport: "MeshtasticTransport",
        envelope: MessageEnvelope,
        destination: str,
        total_chunks: int,
    ) -> None:
        # Cache chunks for potential NACK-driven resends (handled elsewhere)
        return

    def on_chunks_sent(
        self,
        transport: "MeshtasticTransport",
        envelope: MessageEnvelope,
        destination: str,
        total_chunks: int,
    ) -> None:
        return

    def handle_control(
        self,
        flags: int,
        chunk_id: str,
        payload: bytes,
        sender: str,
        transport: "MeshtasticTransport",
    ) -> bool:
        if flags & FLAG_NACK:
            missing = parse_nack_payload(payload)
            transport._handle_nack(sender, chunk_id, missing)
            return True
        if flags & FLAG_ACK:
            # Decode full ACK ID from payload, or use chunk_id (header prefix) if empty
            # Empty payload is an optimization for simple ACKs
            ack_id = payload.decode("utf-8", errors="replace").strip() if payload else ""
            # Drop cached chunks; spool ack if present
            transport._drop_chunk_cache(chunk_id)
            # Use chunk_id from header if payload is empty (optimized simple ACK)
            effective_id = ack_id if ack_id else chunk_id
            if transport.spool and effective_id:
                transport.spool.ack(effective_id)
                transport._record_spool_depth()
            return True
        return False

    def on_missing(
        self, sender: str, chunk_id: str, missing: List[int], transport: "MeshtasticTransport"
    ) -> None:
        if not missing:
            return
        transport.radio.send(sender, build_nack_chunk(chunk_id, missing))

    def on_complete(
        self, sender: str, message: MessageEnvelope, transport: "MeshtasticTransport"
    ) -> None:
        # Use empty payload for simple ACK - header has 8-byte ID prefix
        transport.radio.send(sender, build_ack_chunk(message.id, include_payload=False))


class StageAckNackStrategy:
    """Announce -> chunks -> complete/repair loop."""

    name = "stage"

    def on_send(
        self,
        transport: "MeshtasticTransport",
        envelope: MessageEnvelope,
        destination: str,
        total_chunks: int,
    ) -> None:
        # Send an announce packet with total chunk count; expect acks.
        announce_payload = f"announce|{envelope.id}|{total_chunks}"
        transport.radio.send(destination, build_ack_chunk(announce_payload))

    def on_chunks_sent(
        self,
        transport: "MeshtasticTransport",
        envelope: MessageEnvelope,
        destination: str,
        total_chunks: int,
    ) -> None:
        # Ask receiver to report missing chunks explicitly.
        complete_payload = f"complete|{envelope.id}"
        transport.radio.send(destination, build_ack_chunk(complete_payload))

    def handle_control(
        self,
        flags: int,
        chunk_id: str,
        payload: bytes,
        sender: str,
        transport: "MeshtasticTransport",
    ) -> bool:
        if not (flags & FLAG_ACK):
            if flags & FLAG_NACK:
                missing = parse_nack_payload(payload)
                transport._handle_nack(sender, chunk_id, missing)
                return True
            return False

        text = payload.decode("utf-8", errors="replace")
        if text.startswith("announce|"):
            # Respond to announce
            transport.radio.send(sender, build_ack_chunk(f"announce_ack|{chunk_id}"))
            return True
        if text.startswith("complete|"):
            # Report missing chunks (including trailing gaps)
            missing = transport.reassembler.missing_sequences(chunk_id, force=True) or []
            if missing:
                transport.radio.send(sender, build_nack_chunk(chunk_id, missing))
            else:
                transport.radio.send(sender, build_ack_chunk(f"all_received|{chunk_id}"))
            return True
        if text.startswith("all_received|"):
            # Extract full message ID from the ACK payload
            msg_id = text.split("|", 1)[1] if "|" in text else chunk_id
            # Drop cached chunks; spool ack if present
            transport._drop_chunk_cache(chunk_id)
            if transport.spool and msg_id:
                transport.spool.ack(msg_id)
                transport._record_spool_depth()
            return True
        if text.startswith("announce_ack|"):
            return True
        return False

    def on_missing(
        self, sender: str, chunk_id: str, missing: List[int], transport: "MeshtasticTransport"
    ) -> None:
        # Standard NACK for gaps observed mid-stream.
        if missing:
            transport.radio.send(sender, build_nack_chunk(chunk_id, missing))

    def on_complete(
        self, sender: str, message: MessageEnvelope, transport: "MeshtasticTransport"
    ) -> None:
        # Receiver already handled via complete| handshake; nothing else.
        return


class WindowedSelectiveStrategy:
    """
    Lightweight selective-repeat: sender asks for a bitmap once after sending,
    receiver replies with NACK for any gaps or all_received when complete.

    Optimization: For single-chunk messages, the sender skips the explicit
    bitmap request (Optimistic Reliability) and relies on the receiver's
    implicit on-complete ACK to confirm delivery.
    """

    name = "window"

    def __init__(self, max_nack: int = 5) -> None:
        self.max_nack = max_nack

    def on_send(
        self,
        transport: "MeshtasticTransport",
        envelope: MessageEnvelope,
        destination: str,
        total_chunks: int,
    ) -> None:
        # Cache chunks for potential targeted resends
        return

    def on_chunks_sent(
        self,
        transport: "MeshtasticTransport",
        envelope: MessageEnvelope,
        destination: str,
        total_chunks: int,
    ) -> None:
        # Prompt receiver to report any missing chunks soon after send completes.
        if total_chunks == 1:
            return
        transport.radio.send(destination, build_ack_chunk(f"bitmap_req|{envelope.id}"))

    def handle_control(
        self,
        flags: int,
        chunk_id: str,
        payload: bytes,
        sender: str,
        transport: "MeshtasticTransport",
    ) -> bool:
        if flags & FLAG_NACK:
            missing = parse_nack_payload(payload)
            transport._handle_nack(sender, chunk_id, missing)
            return True
        if not (flags & FLAG_ACK):
            return False

        text = payload.decode("utf-8", errors="replace")
        if text.startswith("bitmap_req|"):
            missing = transport.reassembler.missing_sequences(chunk_id, force=True) or []
            if missing:
                transport.radio.send(sender, build_nack_chunk(chunk_id, missing[: self.max_nack]))
            else:
                transport.radio.send(sender, build_ack_chunk(f"all_received|{chunk_id}"))
            return True
        if text.startswith("all_received|"):
            # Extract full message ID from the ACK payload
            parts = text.split("|", 1)
            if len(parts) == 2 and parts[1]:
                msg_id = parts[1]
            else:
                # Fallback: use chunk_id prefix if payload format is unexpected
                # This may cause issues with spool if it's keyed by full message IDs
                logger.warning("ACK payload missing message ID, using chunk_id prefix: %s", text)
                msg_id = chunk_id
            transport._drop_chunk_cache(chunk_id)
            if transport.spool and msg_id:
                transport.spool.ack(msg_id)
                transport._record_spool_depth()
            return True
        return False

    def on_missing(
        self, sender: str, chunk_id: str, missing: List[int], transport: "MeshtasticTransport"
    ) -> None:
        if missing:
            transport.radio.send(sender, build_nack_chunk(chunk_id, missing[: self.max_nack]))

    def on_complete(
        self, sender: str, message: MessageEnvelope, transport: "MeshtasticTransport"
    ) -> None:
        transport.radio.send(sender, build_ack_chunk(f"all_received|{message.id}"))


class ParityWindowStrategy(WindowedSelectiveStrategy):
    """
    Windowed selective-repeat with a lightweight redundancy hint:
    after sending all chunks, emit a duplicate of the last chunk to help
    recover a single loss without waiting for NACKs.
    """

    name = "window_fec"

    def on_chunks_sent(
        self,
        transport: "MeshtasticTransport",
        envelope: MessageEnvelope,
        destination: str,
        total_chunks: int,
    ) -> None:
        # Regular bitmap prompt
        super().on_chunks_sent(transport, envelope, destination, total_chunks)
        # Opportunistic duplicate of the last chunk in cache (if available)
        prefix = envelope.id[:8]
        cache = getattr(transport, "_chunk_cache", {}).get(prefix)
        if cache:
            last_seq = max(cache.keys())
            dup_chunk = cache.get(last_seq)
            if dup_chunk:
                transport.radio.send(destination, dup_chunk)


def strategy_from_name(name: str | None) -> ReliabilityStrategy:
    lowered = (name or "").strip().lower()
    if lowered in {"simple", "ack", "ack_nack"}:
        return SimpleAckNackStrategy()
    if lowered in {"stage", "staged"}:
        return StageAckNackStrategy()
    if lowered in {"window", "selective", "selective_repeat"}:
        return WindowedSelectiveStrategy()
    if lowered in {"window_fec", "window_parity", "selective_fec"}:
        return ParityWindowStrategy()
    # Default to windowed selective if unspecified
    if name:
        logger.warning("Unrecognized reliability strategy '%s', defaulting to 'window'", name)
    return WindowedSelectiveStrategy()
