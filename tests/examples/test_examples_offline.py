"""Example entry points stop safely with mocked discovery or cancelled input."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from examples import (
    inspect_h7126,
    verify_h7102_ble,
    verify_h7126_ble,
    watch_h5179_ble,
    watch_h7102_raw_ble,
    watch_h7126_ble,
)
from examples import investigate_h7102_oscillation as probe
from govee_ble_client import build_packet

EXAMPLES = (
    inspect_h7126,
    verify_h7102_ble,
    verify_h7126_ble,
    watch_h5179_ble,
    watch_h7102_raw_ble,
    watch_h7126_ble,
)


@pytest.mark.parametrize("module", EXAMPLES, ids=lambda m: m.__name__.split(".")[-1])
def test_no_devices_returns_without_connecting(module, monkeypatch, run_async, capsys):
    scanner = SimpleNamespace(scan=AsyncMock(return_value=[]))
    monkeypatch.setattr(module, "GoveeScanner", lambda: scanner)
    run_async(module.main())
    assert "No " in capsys.readouterr().out
    scanner.scan.assert_awaited_once()


@pytest.mark.parametrize("module", EXAMPLES, ids=lambda m: m.__name__.split(".")[-1])
def test_invalid_selection_never_connects(module, monkeypatch, run_async, capsys):
    device = SimpleNamespace(
        name="H7126 H7102 H5179",
        rssi=-40,
        manufacturer_data={},
        service_data={},
        service_uuids=[],
        connect=Mock(),
    )
    monkeypatch.setattr(
        module,
        "GoveeScanner",
        lambda: SimpleNamespace(scan=AsyncMock(return_value=[device])),
    )
    monkeypatch.setattr("builtins.input", lambda _: "not an index")
    if hasattr(module, "_ainput"):
        monkeypatch.setattr(module, "_ainput", AsyncMock(return_value="not an index"))
    run_async(module.main())
    assert "Invalid" in capsys.readouterr().out
    device.connect.assert_not_called()


def test_probe_cancel_does_not_even_scan(monkeypatch, run_async):
    scanner = Mock()
    monkeypatch.setattr(probe, "GoveeScanner", scanner)
    monkeypatch.setattr(probe, "_ainput", AsyncMock(return_value="n"))
    monkeypatch.setattr("sys.argv", ["probe"])
    run_async(probe.main())
    scanner.assert_not_called()


@pytest.mark.parametrize(
    "arguments", [["--samples", "1"], ["--timeout", "0"], ["--timeout", "nan"]]
)
def test_probe_rejects_bad_cli_options_before_discovery(
    arguments, monkeypatch, run_async
):
    monkeypatch.setattr("sys.argv", ["probe", *arguments])
    with pytest.raises(SystemExit) as error:
        run_async(probe.main())
    assert error.value.code == 2


def test_probe_does_not_overwrite_existing_report(tmp_path, monkeypatch, run_async):
    output = tmp_path / "existing.json"
    output.write_text("keep this")
    monkeypatch.setattr("sys.argv", ["probe", "--output", str(output)])
    with pytest.raises(SystemExit):
        run_async(probe.main())
    assert output.read_text() == "keep this"


def test_probe_records_application_frames_but_not_handshake_material(run_async, capsys):
    query = probe.QUERIES[0]
    handshake = build_packet(0xE7, 1, bytes(range(16)))
    before = build_packet(0xEE, 0x16, b"\x00")
    reply = build_packet(0xAA, 1, b"\x01")

    class Transport:
        def __init__(self):
            self.pending = [handshake, before, TimeoutError]
            self.responses = [ValueError("bad frame"), handshake, reply, TimeoutError]
            self.writes = []

        async def receive(self, *, timeout):
            source = self.responses if self.writes else self.pending
            value = source.pop(0)
            if isinstance(value, type) and issubclass(value, Exception):
                raise value
            if isinstance(value, Exception):
                raise value
            return value

        async def write(self, frame):
            self.writes.append(frame)

    transport = Transport()
    result = run_async(probe._capture_query(transport, query, timeout=0.1))
    assert result["pending_rx"] == [before.hex()]
    assert result["rx"] == [reply.hex()]
    assert handshake.hex() not in capsys.readouterr().out
    assert transport.writes == [query.frame]
