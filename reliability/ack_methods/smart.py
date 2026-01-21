"""Smarter ACK/NACK strategy with throttling and cumulative ACKs."""

from typing import List, TYPE_CHECKING
import time

from framing.frame import Frame, FrameFlags
from reliability.ack_methods.base import AckNackMethod

if TYPE_CHECKING:
    from reliability.stream import Stream


class SmartAckNack(AckNackMethod):
    """
    ACK every N frames or after a time interval; NACK with debounce.

    This reduces control chatter while still reacting to gaps.
    """

    def __init__(
        self,
        ack_every_n: int = 4,
        ack_interval_s: float = 0.5,
        nack_interval_s: float = 0.5,
    ) -> None:
        self.ack_every_n = max(1, ack_every_n)
        self.ack_interval_s = max(0.0, ack_interval_s)
        self.nack_interval_s = max(0.0, nack_interval_s)
        self._pending_acks = 0
        self._last_ack_time = 0.0
        self._last_nack_time = 0.0
        self._last_nack_seq: int | None = None

    def handle_control(self, stream: "Stream", frame: Frame) -> List[Frame]:
        # Import StreamState here to avoid circular import
        from reliability.stream import StreamState

        frames: List[Frame] = []
        if frame.is_syn() and frame.is_ack():
            if stream.state == StreamState.SYN_SENT:
                stream.window.receive_frame(frame)
                stream.window.remove_pending(0)
                stream.state = StreamState.OPEN
                frames.append(
                    Frame(
                        stream_id=stream.stream_id,
                        seq=stream.window.allocate_seq(),
                        ack=stream.window.next_expected_seq,
                        flags=FrameFlags.ACK,
                        payload=b"",
                    )
                )
            return frames

        if frame.is_ack():
            acked = stream.window.process_ack(frame.ack)
            if acked:
                stream.record_ack_rtts(acked)
            if acked and stream.state == StreamState.SYN_SENT:
                stream.state = StreamState.OPEN
        if frame.is_nack():
            to_retransmit = stream.window.process_nack(frame.ack)
            if to_retransmit:
                stream.stats.retransmits += 1
                frames.append(to_retransmit)
        return frames

    def on_missing(self, stream: "Stream", missing_seqs: List[int]) -> List[Frame]:
        if not missing_seqs:
            return []
        now = time.monotonic()
        seq = missing_seqs[0]
        if (
            self._last_nack_seq == seq
            and now - self._last_nack_time < self.nack_interval_s
        ):
            return []
        self._last_nack_seq = seq
        self._last_nack_time = now
        return [
            Frame(
                stream_id=stream.stream_id,
                seq=stream.window.allocate_seq(),
                ack=seq,
                flags=FrameFlags.NACK,
                payload=b"",
            )
        ]

    def on_complete(self, stream: "Stream") -> List[Frame]:
        self._pending_acks += 1
        now = time.monotonic()
        if (
            self._pending_acks < self.ack_every_n
            and now - self._last_ack_time < self.ack_interval_s
        ):
            return []
        self._pending_acks = 0
        self._last_ack_time = now
        return [
            Frame(
                stream_id=stream.stream_id,
                seq=stream.window.allocate_seq(),
                ack=stream.window.next_expected_seq,
                flags=FrameFlags.ACK,
                payload=b"",
            )
        ]
