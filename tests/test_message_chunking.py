from message import MessageEnvelope, chunk_envelope
from reassembly import MessageReassembler


def test_chunk_and_reassemble() -> None:
    envelope = MessageEnvelope(
        id="test",
        type="request",
        command="ping",
        data={"payload": "x" * 128},
    )
    segments = chunk_envelope(envelope, segment_size=32)
    reassembler = MessageReassembler()
    collected = None
    for chunk in segments:
        collected = reassembler.add_chunk(chunk)
    assert collected is not None
    assert collected.id == envelope.id
    assert collected.command == envelope.command
    assert collected.data == envelope.data
