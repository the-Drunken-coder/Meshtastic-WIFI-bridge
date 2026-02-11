"""Unit tests for SerialRadioAdapter destination handling."""

from __future__ import annotations

import sys
import types

import pytest

from radio import SerialRadioAdapter
from radio import build_radio
from radio import open_serial_interface


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


def test_build_radio_retries_serial_connect(monkeypatch) -> None:
    calls: dict[str, int] = {"count": 0}

    class _FlakySerialInterface:
        def __init__(self, _dev_path: str | None = None, *args, **kwargs) -> None:
            calls["count"] += 1
            if calls["count"] < 3:
                raise RuntimeError("transient connect failure")

        def close(self) -> None:
            return

    # Provide a fake meshtastic module so `from meshtastic import serial_interface` resolves
    # without touching real hardware.
    fake_serial_interface_mod = types.ModuleType("meshtastic.serial_interface")
    fake_serial_interface_mod.SerialInterface = _FlakySerialInterface  # type: ignore[attr-defined]
    fake_meshtastic_mod = types.ModuleType("meshtastic")
    fake_meshtastic_mod.serial_interface = fake_serial_interface_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "meshtastic", fake_meshtastic_mod)

    # Keep the test fast/deterministic.
    monkeypatch.setenv("MESHTASTIC_SERIAL_CONNECT_ATTEMPTS", "3")
    monkeypatch.setenv("MESHTASTIC_SERIAL_CONNECT_DELAY_SECONDS", "0")
    import radio as radio_module

    monkeypatch.setattr(radio_module.time, "sleep", lambda _s: None)

    adapter = None
    try:
        adapter = build_radio(simulate=False, port="COM17", node_id="node-1")
        assert calls["count"] == 3
        assert isinstance(adapter, SerialRadioAdapter)
    finally:
        if adapter is not None:
            adapter.close()


def test_build_radio_raises_after_all_retries_exhausted(monkeypatch) -> None:
    """When every attempt fails, the last exception must propagate."""

    class _AlwaysFailInterface:
        def __init__(self, _dev_path: str | None = None, *args, **kwargs) -> None:
            raise RuntimeError("permanent failure")

        def close(self) -> None:
            return

    fake_serial_interface_mod = types.ModuleType("meshtastic.serial_interface")
    fake_serial_interface_mod.SerialInterface = _AlwaysFailInterface  # type: ignore[attr-defined]
    fake_meshtastic_mod = types.ModuleType("meshtastic")
    fake_meshtastic_mod.serial_interface = fake_serial_interface_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "meshtastic", fake_meshtastic_mod)

    monkeypatch.setenv("MESHTASTIC_SERIAL_CONNECT_ATTEMPTS", "2")
    monkeypatch.setenv("MESHTASTIC_SERIAL_CONNECT_DELAY_SECONDS", "0")
    import radio as radio_module

    monkeypatch.setattr(radio_module.time, "sleep", lambda _s: None)

    with pytest.raises(RuntimeError, match="permanent failure"):
        build_radio(simulate=False, port="COM17", node_id="node-1")


def test_open_serial_interface_closes_orphan_on_init_failure(monkeypatch) -> None:
    """If SerialInterface.__init__ raises after self is created, .close() must be called."""
    closed: list[bool] = []

    class _OrphanInterface:
        def __init__(self, _dev_path: str | None = None, *args, **kwargs) -> None:
            # __init__ body runs (self exists) then fails
            raise RuntimeError("init explosion")

        def close(self) -> None:
            closed.append(True)

    fake_mod = types.ModuleType("fake_serial_interface")
    fake_mod.SerialInterface = _OrphanInterface  # type: ignore[attr-defined]

    with pytest.raises(RuntimeError, match="init explosion"):
        open_serial_interface(fake_mod, "COM99")

    assert closed, "orphaned interface was not closed"
