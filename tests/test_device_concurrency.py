"""Connection ownership and scheduling tests using an in-memory BLE backend."""

import asyncio

import pytest

from govee_ble_client import GattConnection
from govee_ble_client.exceptions import DeviceNotConnectedError


class Client:
    """Minimal backend with observable I/O and explicit disconnect delivery."""

    def __init__(self, device, **options):
        self.device = device
        self.options = options
        self.is_connected = False
        self.services = [device]
        self.writes = []
        self.disconnects = 0
        self.block = None
        self.entered = asyncio.Event()

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.disconnects += 1
        self.is_connected = False
        callback = self.options.get("disconnected_callback")
        if callback is not None:
            callback(self)

    async def read_gatt_char(self, characteristic):
        self.entered.set()
        if self.block is not None:
            await self.block.wait()
        return b"value"

    async def write_gatt_char(self, characteristic, data, **options):
        self.writes.append(data)


@pytest.fixture
def backend(monkeypatch):
    clients = []

    def factory(device, **options):
        client = Client(device, **options)
        clients.append(client)
        return client

    monkeypatch.setattr("govee_ble_client.device.BleakClient", factory)
    return clients


def test_one_device_serializes_io_while_other_devices_progress(backend, run_async):
    async def check():
        first, second = GattConnection(object()), GattConnection(object())
        await asyncio.gather(first.connect(), second.connect())
        client = backend[0]
        client.block = asyncio.Event()
        read = asyncio.create_task(first.read("read"))
        await client.entered.wait()
        write = asyncio.create_task(first.write("write", b"first"))
        await second.write("write", b"second")
        await asyncio.sleep(0)
        try:
            assert client.writes == []
            assert backend[1].writes == [b"second"]
        finally:
            client.block.set()
            await asyncio.gather(read, write)
            await asyncio.gather(first.disconnect(), second.disconnect())
        assert client.writes == [b"first"]

    run_async(check())


def test_concurrent_connect_is_idempotent_and_new_session_uses_fresh_client(
    backend, run_async
):
    async def check():
        connection = GattConnection(object())
        await asyncio.gather(connection.connect(), connection.connect())
        assert len(backend) == 1
        await connection.disconnect()
        await connection.connect()
        assert len(backend) == 2
        assert connection.is_connected
        await connection.disconnect()

    run_async(check())


def test_disconnect_waits_for_active_io(backend, run_async):
    async def check():
        connection = GattConnection(object())
        await connection.connect()
        client = backend[0]
        client.block = asyncio.Event()
        read = asyncio.create_task(connection.read("read"))
        await client.entered.wait()
        disconnect = asyncio.create_task(connection.disconnect())
        await asyncio.sleep(0)
        try:
            assert client.disconnects == 0
        finally:
            client.block.set()
            await asyncio.gather(read, disconnect)
        assert client.disconnects == 1

    run_async(check())


def test_injected_connector_resolves_device_for_every_session(run_async):
    async def check():
        devices = iter([object(), object()])
        clients = []

        async def connector(device, **options):
            client = Client(device, **options)
            await client.connect()
            clients.append(client)
            return client

        connection = GattConnection(
            object(),
            connector=connector,
            device_resolver=lambda: next(devices),
            services=["service"],
            timeout=15,
        )
        await connection.connect()
        first = connection.services
        await connection.reconnect()
        assert connection.services != first
        assert all(c.options["services"] == ("service",) for c in clients)
        assert all(c.options["timeout"] == 15 for c in clients)
        await connection.disconnect()

    run_async(check())


def test_stale_disconnect_callback_does_not_invalidate_new_session(backend, run_async):
    async def check():
        connection = GattConnection(object())
        losses = []
        unsubscribe = connection.add_disconnect_callback(lambda: losses.append(True))
        await connection.connect()
        old = backend[0]
        await connection.reconnect()
        assert losses == [True]
        old.options["disconnected_callback"](old)
        assert connection.is_connected
        assert losses == [True]
        unsubscribe()
        await connection.disconnect()
        assert losses == [True]

    run_async(check())


@pytest.mark.parametrize("parameter", ["timeout", "operation_timeout"])
@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_invalid_deadlines_fail_before_backend(parameter, value, backend):
    with pytest.raises(ValueError):
        GattConnection(object(), **{parameter: value})
    assert backend == []


def test_operation_timeout_includes_time_queued_for_connection(backend, run_async):
    async def check():
        entered, release = asyncio.Event(), asyncio.Event()

        async def connector(device, **options):
            client = Client(device, **options)
            entered.set()
            await release.wait()
            await client.connect()
            return client

        connection = GattConnection(
            object(), connector=connector, timeout=1, operation_timeout=0.01
        )
        setup = asyncio.create_task(connection.connect())
        await entered.wait()
        try:
            with pytest.raises(TimeoutError):
                await connection.write("write", b"late")
        finally:
            release.set()
            await setup
            await connection.disconnect()

    run_async(check())


def test_active_operation_timeout_retires_session_without_replaying(backend, run_async):
    async def check():
        connection = GattConnection(object(), operation_timeout=0.01)
        await connection.connect()
        client = backend[0]
        client.block = asyncio.Event()
        with pytest.raises(TimeoutError):
            await connection.read("read")
        assert not connection.is_connected
        assert client.disconnects == 1
        assert client.writes == []

    run_async(check())


def test_connection_timeout_cleans_partial_connection_and_allows_retry(
    backend, run_async
):
    async def check():
        connection = GattConnection(object(), timeout=0.01)
        # Supply a backend which connects physically, then stalls during setup.
        from govee_ble_client import device as module

        factory = module.BleakClient

        def stalled(device, **options):
            client = factory(device, **options)

            async def connect():
                client.is_connected = True
                await asyncio.Event().wait()

            client.connect = connect
            return client

        module.BleakClient = stalled
        with pytest.raises(TimeoutError):
            await connection.connect()
        assert not connection.is_connected
        assert backend[0].disconnects == 1
        module.BleakClient = factory
        await connection.connect()
        assert connection.is_connected
        await connection.disconnect()

    run_async(check())


def test_missing_resolved_device_does_not_use_stale_backend(backend, run_async):
    async def check():
        connection = GattConnection(object(), device_resolver=lambda: None)
        with pytest.raises(DeviceNotConnectedError):
            await connection.connect()
        assert backend == []

    run_async(check())


def test_cancelled_setup_cleans_client(backend, run_async):
    async def check():
        from govee_ble_client import device as module

        factory = module.BleakClient
        entered = asyncio.Event()

        def stalled(device, **options):
            client = factory(device, **options)

            async def connect():
                client.is_connected = True
                entered.set()
                await asyncio.Event().wait()

            client.connect = connect
            return client

        module.BleakClient = stalled
        connection = GattConnection(object())
        task = asyncio.create_task(connection.connect())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert backend[0].disconnects == 1
        assert not connection.is_connected

    run_async(check())


def test_queued_write_cannot_cross_reconnect_boundary(backend, run_async):
    async def check():
        connection = GattConnection(object())
        await connection.connect()
        old = backend[0]
        old.block = asyncio.Event()
        read = asyncio.create_task(connection.read("read"))
        await old.entered.wait()
        reconnect = asyncio.create_task(connection.reconnect())
        await asyncio.sleep(0)
        write = asyncio.create_task(connection.write("write", b"old session"))
        await asyncio.sleep(0)
        old.block.set()
        await asyncio.gather(read, reconnect)
        try:
            with pytest.raises(DeviceNotConnectedError, match="session"):
                await write
            assert backend[1].writes == []
        finally:
            await connection.disconnect()

    run_async(check())


def test_cancelled_disconnect_retains_client_for_cleanup_retry(backend, run_async):
    async def check():
        connection = GattConnection(object())
        await connection.connect()
        client = backend[0]
        entered = asyncio.Event()
        original = client.disconnect

        async def stalled():
            entered.set()
            await asyncio.Event().wait()

        client.disconnect = stalled
        task = asyncio.create_task(connection.disconnect())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not connection.is_connected
        assert client.is_connected
        client.disconnect = original
        await connection.disconnect()
        assert not client.is_connected
        assert client.disconnects == 1

    run_async(check())


def test_disconnect_observer_failure_is_isolated_without_logging_payload(
    backend, run_async, caplog
):
    async def check():
        connection = GattConnection(object())
        callbacks = []

        def broken():
            raise ValueError("synthetic-sensitive-payload")

        connection.add_disconnect_callback(broken)
        connection.add_disconnect_callback(lambda: callbacks.append(True))
        await connection.connect()
        await connection.disconnect()
        assert callbacks == [True]
        assert "synthetic-sensitive-payload" not in caplog.text

    run_async(check())
