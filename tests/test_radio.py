"""Unit tests for SerialRadioAdapter destination handling."""

from __future__ import annotations

from radio import SerialRadioAdapter


class _FakeSerialInterface:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    def sendData(  # noqa: N802 - matches meshtastic API name
        self,
        payload: bytes,
        *,
        destinationId: str,
        wantAck: bool,
        portNum: int,
    ) -> None:
        self.sent.append(
            {
                "payload": payload,
                "destinationId": destinationId,
                "wantAck": wantAck,
                "portNum": portNum,
            }
        )


def test_send_preserves_prefixed_user_id() -> None:
    iface = _FakeSerialInterface()
    radio = SerialRadioAdapter(iface)

    radio.send("!90965648", b"hello")

    assert iface.sent
    assert iface.sent[0]["destinationId"] == "!90965648"


def test_send_prefixed_user_id_skips_numeric_conversion(monkeypatch) -> None:
    iface = _FakeSerialInterface()
    radio = SerialRadioAdapter(iface)
    called = False

    def _fake_convert(_numeric_id: str) -> str:
        nonlocal called
        called = True
        return "!deadbeef"

    monkeypatch.setattr(radio, "_convert_numeric_to_user_id", _fake_convert)
    radio.send("!90965648", b"hello")

    assert called is False
    assert iface.sent[0]["destinationId"] == "!90965648"


def test_send_converts_decimal_numeric_id(monkeypatch) -> None:
    iface = _FakeSerialInterface()
    radio = SerialRadioAdapter(iface)

    monkeypatch.setattr(radio, "_convert_numeric_to_user_id", lambda _numeric_id: "!90965648")
    radio.send("2425771592", b"hello")

    assert iface.sent
    assert iface.sent[0]["destinationId"] == "!90965648"
