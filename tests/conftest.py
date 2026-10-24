"""Offline-only test fixtures: real Bluetooth entry points fail closed."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import bleak
import pytest

from govee_ble_client import device as device_module

_session_guard = None


def forbidden(*args, **kwargs):
    pytest.fail("Real Bluetooth access is forbidden in the offline test suite")


async def forbidden_async(*args, **kwargs):
    forbidden()


def protect_bluetooth(monkeypatch):
    for method in (
        "connect",
        "disconnect",
        "read_gatt_char",
        "write_gatt_char",
        "start_notify",
        "stop_notify",
    ):
        monkeypatch.setattr(bleak.BleakClient, method, forbidden_async)
    monkeypatch.setattr(bleak.BleakScanner, "__init__", forbidden)
    for method in ("discover", "start", "stop"):
        monkeypatch.setattr(bleak.BleakScanner, method, forbidden_async)
    monkeypatch.setattr(bleak.BleakClient, "__init__", forbidden)
    # Also replace the constructor alias already imported by the library.
    monkeypatch.setattr(device_module, "BleakClient", forbidden)


def pytest_sessionstart(session):
    """Block real BLE before pytest imports test modules."""
    global _session_guard
    _session_guard = pytest.MonkeyPatch()
    protect_bluetooth(_session_guard)


def pytest_unconfigure(config):
    if _session_guard is not None:
        _session_guard.undo()


@pytest.fixture(autouse=True)
def block_real_bluetooth(monkeypatch):
    """Restore the blocking defaults between tests that install their own fakes."""
    protect_bluetooth(monkeypatch)


@pytest.fixture
def run_async():
    return asyncio.run


@pytest.fixture
def connection_factory():
    """Make GATT-shaped objects whose writes only append to mock call lists."""

    def factory(profile=None, *, write_property="write-without-response"):
        services = []
        if profile is not None:
            if hasattr(profile, "io_uuid"):
                chars = [
                    SimpleNamespace(
                        uuid=profile.io_uuid, properties=["notify", write_property]
                    )
                ]
            else:
                chars = [
                    SimpleNamespace(uuid=profile.notify_uuid, properties=["notify"]),
                    SimpleNamespace(
                        uuid=profile.write_uuid, properties=[write_property]
                    ),
                ]
            services = [
                SimpleNamespace(uuid=profile.service_uuid, characteristics=chars)
            ]
        connection = SimpleNamespace(
            is_connected=True,
            services=services,
            write=AsyncMock(),
            read=AsyncMock(return_value=b"fake read"),
            start_notify=AsyncMock(),
            stop_notify=AsyncMock(),
            connect=AsyncMock(),
            disconnect=AsyncMock(),
            reconnect=AsyncMock(),
        )
        return connection

    return factory


@pytest.fixture
def fake_bleak_factory(monkeypatch):
    """Replace BleakClient before GattConnection can instantiate it."""
    clients = []

    class FakeBleakClient:
        def __init__(self, device, *, timeout):
            self.device = device
            self.timeout = timeout
            self.is_connected = False
            self.services = ["fake service"]
            self.connect = AsyncMock(side_effect=self._connect)
            self.disconnect = AsyncMock(side_effect=self._disconnect)
            self.read_gatt_char = AsyncMock(return_value=bytearray(b"read"))
            self.write_gatt_char = AsyncMock()
            self.start_notify = AsyncMock()
            self.stop_notify = AsyncMock()
            clients.append(self)

        async def _connect(self):
            self.is_connected = True

        async def _disconnect(self):
            self.is_connected = False

    factory = Mock(side_effect=FakeBleakClient)
    monkeypatch.setattr(device_module, "BleakClient", factory)
    return factory, clients
