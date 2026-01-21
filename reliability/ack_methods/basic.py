"""Default ACK/NACK strategy matching the current behavior."""

from typing import List, TYPE_CHECKING

from framing.frame import Frame, FrameFlags
from reliability.ack_methods.base import AckNackMethod

if TYPE_CHECKING:
    from reliability.stream import Stream


class BasicAckNack(AckNackMethod):
    """ACK on in-order delivery; NACK first missing sequence on gaps."""

    def handle_control(self, stream: "Stream", frame: Frame) -> List[Frame]:
        # Import StreamState here to avoid circular import
        from reliability.stream import StreamState
        
        frames: List[Frame] = []
        if frame.is_ack():
            acked = stream.window.process_ack(frame.ack)
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
        return [
            Frame(
                stream_id=stream.stream_id,
                seq=stream.window.allocate_seq(),
                ack=missing_seqs[0],
                flags=FrameFlags.NACK,
                payload=b"",
            )
        ]

    def on_complete(self, stream: "Stream") -> List[Frame]:
        return [
            Frame(
                stream_id=stream.stream_id,
                seq=stream.window.allocate_seq(),
                ack=stream.window.next_expected_seq,
                flags=FrameFlags.ACK,
                payload=b"",
            )
        ]
