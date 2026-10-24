"""GATT lifecycle and advertisement records through fake Bleak clients."""

from types import SimpleNamespace

import bleak
import pytest

from govee_ble_client import DiscoveredDevice, GattConnection
from govee_ble_client.exceptions import DeviceNotConnectedError


def test_real_client_and_scanner_are_blocked(run_async):
    with pytest.raises(pytest.fail.Exception, match="forbidden"):
        bleak.BleakClient("unused")
    with pytest.raises(pytest.fail.Exception, match="forbidden"):
        run_async(bleak.BleakScanner.discover())
    with pytest.raises(pytest.fail.Exception, match="forbidden"):
        GattConnection(SimpleNamespace())


@pytest.mark.parametrize(
    ("local_name", "expected"), [("advertisement", "advertisement"), (None, "fallback")]
)
def test_advertisement_copy_and_name_fallback(local_name, expected):
    device = SimpleNamespace(address="synthetic-id", name="fallback")
    adv = SimpleNamespace(
        local_name=local_name,
        rssi=-45,
        manufacturer_data={1: b"one"},
        service_data={"service": b"two"},
        service_uuids=["uuid"],
    )
    found = DiscoveredDevice.from_advertisement(device, adv)
    adv.manufacturer_data.clear()
    adv.service_data.clear()
    adv.service_uuids.clear()
    assert found.name == expected
    assert found.identifier == "synthetic-id"
    assert found.rssi == -45
    assert found.manufacturer_data == {1: b"one"}
    assert found.service_data == {"service": b"two"}
    assert found.service_uuids == ("uuid",)
    assert found._ble_device is device
    assert "_ble_device" not in repr(found)


def test_discovered_device_connection_options(fake_bleak_factory):
    factory, _clients = fake_bleak_factory
    found = DiscoveredDevice("synthetic", None, None, _ble_device=object())
    connection = found.connect(timeout=12)
    assert isinstance(connection, GattConnection)
    factory.assert_called_once_with(found._ble_device, timeout=12)
    assert not connection.is_connected
    for timeout in (0, -1):
        with pytest.raises(ValueError):
            found.connect(timeout=timeout)


@pytest.mark.parametrize(
    "operation", ["read", "write", "start_notify", "stop_notify", "services"]
)
def test_disconnected_gatt_operations_fail_before_backend(
    operation, fake_bleak_factory, run_async
):
    _, clients = fake_bleak_factory
    connection = GattConnection(object())
    with pytest.raises(DeviceNotConnectedError):
        if operation == "services":
            _ = connection.services
        elif operation == "read":
            run_async(connection.read("uuid"))
        elif operation == "write":
            run_async(connection.write("uuid", b"x"))
        elif operation == "start_notify":
            run_async(connection.start_notify("uuid", lambda *_: None))
        else:
            run_async(connection.stop_notify("uuid"))
    assert not clients[0].read_gatt_char.called
    assert not clients[0].write_gatt_char.called
    assert not clients[0].start_notify.called
    assert not clients[0].stop_notify.called


def test_gatt_forwarding_and_context_cleanup(fake_bleak_factory, run_async):
    _, clients = fake_bleak_factory
    callback = lambda *_: None

    async def check():
        connection = GattConnection(object(), timeout=12)
        with pytest.raises(RuntimeError, match="caller failed"):
            async with connection as connected:
                assert connected is connection
                assert connection.is_connected
                assert connection.services == ["fake service"]
                assert await connection.read("read") == b"read"
                await connection.write("write", b"frame", response=False)
                await connection.start_notify("notify", callback)
                await connection.stop_notify("notify")
                with pytest.raises(TypeError):
                    await connection.write("write", "not bytes")
                raise RuntimeError("caller failed")
        await connection.disconnect()
        assert not connection.is_connected

    run_async(check())
    client = clients[0]
    client.connect.assert_awaited_once()
    client.read_gatt_char.assert_awaited_once_with("read")
    client.write_gatt_char.assert_awaited_once_with("write", b"frame", response=False)
    client.start_notify.assert_awaited_once_with("notify", callback)
    client.stop_notify.assert_awaited_once_with("notify")
    client.disconnect.assert_awaited_once()


def test_reconnect_replaces_client_and_preserves_parameters(
    fake_bleak_factory, run_async
):
    _, clients = fake_bleak_factory
    device = object()

    async def check():
        connection = GattConnection(device, timeout=17)
        await connection.connect()
        assert await connection.reconnect() is connection
        assert connection.is_connected
        await connection.disconnect()

    run_async(check())
    assert len(clients) == 2
    assert all(c.device is device and c.timeout == 17 for c in clients)
    clients[0].disconnect.assert_awaited_once()
    clients[1].connect.assert_awaited_once()


def test_connect_failure_propagates_without_claiming_connected(
    fake_bleak_factory, run_async
):
    _, clients = fake_bleak_factory
    connection = GattConnection(object())
    clients[0].connect.side_effect = RuntimeError("connect failed")
    with pytest.raises(RuntimeError, match="connect failed"):
        run_async(connection.connect())
    assert not connection.is_connected
