"""Host logging ownership and privacy-safe diagnostics through offline transports."""

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_encryption import KEY, Peripheral, make_transport

from govee_ble_client import DeviceFilter, GattConnection, GoveeScanner, build_packet
from govee_ble_client.encryption import GoveeAesRc4V1


def test_import_preserves_host_configuration_and_silences_unconfigured_library():
    script = """
import io
import logging
root = logging.getLogger()
stream = io.StringIO()
handler = logging.StreamHandler(stream)
root.addHandler(handler)
root.setLevel(logging.ERROR)
package = logging.getLogger('govee_ble_client')
package.setLevel(logging.INFO)
import govee_ble_client
assert root.handlers == [handler]
assert root.level == logging.ERROR
assert package.level == logging.INFO
assert package.propagate
assert len(package.handlers) == 1
assert isinstance(package.handlers[0], logging.NullHandler)
logging.getLogger('govee_ble_client.device').warning('host-routed')
assert stream.getvalue() == 'host-routed\\n'
root.removeHandler(handler)
logging.getLogger('govee_ble_client.device').warning('must stay silent')
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""


def test_gatt_logs_correlate_connections_without_identifying_data(
    fake_bleak_factory, run_async, caplog
):
    caplog.set_level(logging.DEBUG, logger="govee_ble_client")

    async def check():
        first = GattConnection(
            SimpleNamespace(address="PRIVATE-ADDRESS", name="PRIVATE-NAME")
        )
        second = GattConnection(object())
        async with first, second:
            await first.write("PRIVATE-UUID", b"PRIVATE-PAYLOAD", response=False)
            await second.read("PRIVATE-UUID")
            await first.start_notify("PRIVATE-UUID", lambda *_: None)
            await first.stop_notify("PRIVATE-UUID")
        await first.reconnect()
        await first.disconnect()

    run_async(check())
    records = [r for r in caplog.records if r.name == "govee_ble_client.device"]
    writes = [r for r in records if "write completed" in r.getMessage()]
    reads = [r for r in records if "read completed" in r.getMessage()]
    assert len(writes) == len(reads) == 1
    assert writes[0].connection_id != reads[0].connection_id
    assert "elapsed=" in writes[0].getMessage()
    assert "bytes=15" in caplog.text
    assert "response=False" in caplog.text
    assert "reconnect completed" in caplog.text
    assert "start_notify completed" in caplog.text
    assert "stop_notify completed" in caplog.text
    assert all(r.levelno == logging.DEBUG and r.exc_info is None for r in records)
    assert "PRIVATE" not in str([r.__dict__ for r in records])


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("PRIVATE-ERROR"),
        TimeoutError("PRIVATE-ERROR"),
        asyncio.CancelledError("PRIVATE-ERROR"),
    ],
)
def test_operation_failures_preserve_exception_and_log_safe_type(
    failure, fake_bleak_factory, run_async, caplog
):
    caplog.set_level(logging.DEBUG, logger="govee_ble_client")
    _, clients = fake_bleak_factory

    async def check():
        connection = GattConnection(object())
        await connection.connect()
        clients[0].write_gatt_char.side_effect = failure
        with pytest.raises(type(failure)) as caught:
            await connection.write("PRIVATE-UUID", b"PRIVATE-PAYLOAD")
        assert caught.value is failure
        await connection.disconnect()

    run_async(check())
    assert "write failed" in caplog.text
    assert type(failure).__name__ in caplog.text
    assert "timeout=10" in caplog.text
    assert "PRIVATE" not in str([r.__dict__ for r in caplog.records])


def test_suppressed_callback_error_has_context_without_exception_text(
    fake_bleak_factory, run_async, caplog
):
    def broken():
        raise ValueError("PRIVATE-CALLBACK")

    async def check():
        connection = GattConnection(object())
        connection.add_disconnect_callback(broken)
        await connection.connect()
        await connection.disconnect()

    run_async(check())
    record = next(r for r in caplog.records if r.levelno == logging.WARNING)
    assert record.connection_id
    assert "ValueError" in record.getMessage()
    assert record.exc_info is None
    assert "PRIVATE" not in str(record.__dict__)


def test_handshake_logs_stages_without_key_material(run_async, caplog):
    caplog.set_level(logging.DEBUG, logger="govee_ble_client")

    async def check():
        transport, _ = make_transport()
        await transport.connect()
        await transport.close()

    run_async(check())
    records = [r for r in caplog.records if r.name == "govee_ble_client.encryption"]
    assert "handshake stage=01" in caplog.text
    assert "handshake stage=02" in caplog.text
    assert "handshake completed" in caplog.text
    assert len({r.connection_id for r in records}) == 1
    assert KEY.hex() not in caplog.text
    assert repr(KEY) not in caplog.text


def test_gatt_and_encryption_share_correlation(fake_bleak_factory, run_async, caplog):
    caplog.set_level(logging.DEBUG, logger="govee_ble_client")

    async def check():
        connection = GattConnection(object())
        await connection.connect()
        transport = GoveeAesRc4V1(connection, write_uuid="write", notify_uuid="notify")
        await transport.close()
        await connection.disconnect()

    run_async(check())
    records = [r for r in caplog.records if r.name.startswith("govee_ble_client.")]
    assert {r.name for r in records} == {
        "govee_ble_client.device",
        "govee_ble_client.encryption",
    }
    assert len({r.connection_id for r in records}) == 1


def test_handshake_write_failure_identifies_stage(run_async, caplog):
    caplog.set_level(logging.DEBUG, logger="govee_ble_client")

    async def check():
        peripheral = Peripheral()
        peripheral.write = AsyncMock(side_effect=RuntimeError("PRIVATE-WRITE"))
        transport, _ = make_transport(peripheral)
        with pytest.raises(RuntimeError, match="PRIVATE-WRITE"):
            await transport.connect()

    run_async(check())
    assert "handshake stage=01 sending request" in caplog.text
    assert "handshake failed error=RuntimeError" in caplog.text
    assert "PRIVATE" not in str([r.__dict__ for r in caplog.records])


def test_handshake_timeout_identifies_stage_and_preserves_cleanup_error(
    run_async, caplog
):
    caplog.set_level(logging.DEBUG, logger="govee_ble_client")

    async def check():
        peripheral = Peripheral(confirm=False)
        peripheral.stop_notify = AsyncMock(side_effect=ValueError("PRIVATE-CLEANUP"))
        transport, _ = make_transport(peripheral)
        with pytest.raises(TimeoutError):
            await transport.connect(timeout=0.001)
        peripheral.stop_notify = AsyncMock()
        await transport.close()

    run_async(check())
    assert "handshake failed" in caplog.text
    assert "stage=02" in caplog.text
    assert "TimeoutError" in caplog.text
    assert "cleanup failed" in caplog.text
    assert "ValueError" in caplog.text
    assert "PRIVATE" not in str([r.__dict__ for r in caplog.records])


def test_overflow_logs_capacity_once_without_notifications(run_async, caplog):
    caplog.set_level(logging.DEBUG, logger="govee_ble_client")

    async def check():
        peripheral = Peripheral()
        transport = GoveeAesRc4V1(
            peripheral,
            write_uuid="write",
            notify_uuid="notify",
            notification_buffer_size=2,
        )
        await transport.connect()
        for _ in range(5):
            peripheral.emit(build_packet(0xAA, 1, b"\x01"), KEY)
        await transport.close()

    run_async(check())
    overflow = [r for r in caplog.records if "buffer overflow" in r.getMessage()]
    assert len(overflow) == 1
    assert "capacity=2" in overflow[0].getMessage()
    assert "reconnect required" in overflow[0].getMessage()


def test_scan_reports_counts_without_advertisement_data(monkeypatch, run_async, caplog):
    caplog.set_level(logging.DEBUG, logger="govee_ble_client")
    advertisement = SimpleNamespace(
        local_name="PRIVATE",
        rssi=-20,
        manufacturer_data={1: b"PRIVATE"},
        service_data={},
        service_uuids=[],
    )
    discover = AsyncMock(
        return_value={
            "PRIVATE": (
                SimpleNamespace(address="PRIVATE", name="PRIVATE"),
                advertisement,
            )
        }
    )
    monkeypatch.setattr("govee_ble_client.scanner.BleakScanner.discover", discover)
    assert (
        run_async(
            GoveeScanner().scan(device_filter=DeviceFilter(name_contains="other"))
        )
        == []
    )
    assert "discovered=1 matched=0" in caplog.text
    assert "scan completed" in caplog.text
    assert "PRIVATE" not in str([r.__dict__ for r in caplog.records])
