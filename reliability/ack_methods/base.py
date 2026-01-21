"""ACK/NACK strategy interface for reliable streams."""

from typing import List, TYPE_CHECKING

from framing.frame import Frame

if TYPE_CHECKING:
    from reliability.stream import Stream


class AckNackMethod:
    """Base class for ACK/NACK strategies."""

    def on_send(self, stream: "Stream", frame: Frame) -> List[Frame]:
        """
        Hook invoked before sending a payload frame.

        Returns:
            List of control frames to send before the payload frame.
        """
        return []

    def on_chunks_sent(self, stream: "Stream") -> List[Frame]:
        """
        Hook invoked after a batch of payload frames is sent.

        Returns:
            List of control frames to send after the batch.
        """
        return []

    def handle_control(self, stream: "Stream", frame: Frame) -> List[Frame]:
        """
        Handle incoming control information from a frame (ACK/NACK).

        Returns:
            List of frames to send (e.g., retransmits).
        """
        return []

    def on_missing(self, stream: "Stream", missing_seqs: List[int]) -> List[Frame]:
        """
        Hook invoked when receiver detects missing sequence numbers.

        Returns:
            List of control frames to send (e.g., NACK).
        """
        return []

    def on_complete(self, stream: "Stream") -> List[Frame]:
        """
        Hook invoked when receiver delivers in-order data (and buffered data).

        Returns:
            List of control frames to send (e.g., ACK).
        """
        return []
