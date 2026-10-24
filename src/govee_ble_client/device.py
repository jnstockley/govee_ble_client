"""Discovered-device records and a small, protocol-neutral GATT client."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from math import isfinite
from typing import Any, Protocol, Self

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from ._logging import connection_logger, log_operation, new_connection_id
from .exceptions import DeviceNotConnectedError

_LOGGER = logging.getLogger(__name__)


class ConnectionStrategy(Protocol):
    """Async factory for host-managed BLE connection setup.

    Return a fresh connected Bleak-compatible client and retain the supplied
    disconnect callback. Clean up partially created clients on errors and
    cancellation; the connection handle owns a successfully returned client.
    ``services`` is passed only when filtering was requested. ``timeout`` is
    the configured overall setup budget; the caller also enforces a deadline.
    """

    async def __call__(
        self,
        device: BLEDevice,
        *,
        timeout: float,
        disconnected_callback: Callable[[BleakClient], None],
        services: tuple[str, ...] | None = None,
    ) -> BleakClient: ...


def _validate_timeout(value: float, name: str) -> None:
    if not isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number")


@dataclass(frozen=True, slots=True)
class DiscoveredDevice:
    """A device observed in an advertisement scan.

    ``identifier`` is the platform-provided identifier (often, but not always,
    a MAC address). It may be private/randomized and should not be treated as a
    permanent identity or authentication credential. Advertisement dictionaries
    are copied but remain mutable despite the frozen record. Use
    :meth:`from_advertisement` with a host scanner's native device/advertisement
    rather than assigning the private backend field.
    """

    identifier: str
    name: str | None
    rssi: int | None
    manufacturer_data: dict[int, bytes] = field(default_factory=dict)
    service_data: dict[str, bytes] = field(default_factory=dict)
    service_uuids: tuple[str, ...] = ()
    _ble_device: BLEDevice | None = field(repr=False, compare=False, default=None)

    @classmethod
    def from_advertisement(
        cls, device: BLEDevice, advertisement: AdvertisementData
    ) -> DiscoveredDevice:
        """Copy advertisement fields into a discovery record."""
        return cls(
            identifier=device.address,
            name=advertisement.local_name or device.name,
            rssi=advertisement.rssi,
            manufacturer_data=dict(advertisement.manufacturer_data),
            service_data=dict(advertisement.service_data),
            service_uuids=tuple(advertisement.service_uuids),
            _ble_device=device,
        )

    def connect(
        self,
        *,
        timeout: float = 10.0,
        operation_timeout: float = 10.0,
        services: Iterable[str] | None = None,
        connector: ConnectionStrategy | None = None,
        device_resolver: Callable[[], BLEDevice | None] | None = None,
    ) -> GattConnection:
        """Create an unconnected handle; this method performs no BLE I/O.

        Enter ``async with record.connect(...)`` or await the returned handle's
        ``connect()``. Options have the same meanings as :class:`GattConnection`.
        A manually constructed record without a native device raises
        ``ValueError``; use discovery or ``from_advertisement`` instead.
        """
        if not isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a finite positive number")
        if self._ble_device is None:
            raise ValueError("cannot connect without a BLE device from discovery")
        return GattConnection(
            self._ble_device,
            timeout=timeout,
            operation_timeout=operation_timeout,
            services=services,
            connector=connector,
            device_resolver=device_resolver,
        )


class GattConnection:
    """GATT operations serialized per device, independent across connections.

    Clients are created lazily and never reused for a new session. Optional
    injected setup and device resolution support host-managed BLE adapters.
    Deadlines include lock contention; failure cleanup has a separate bounded
    operation budget. Use each instance on a single asyncio event loop and
    create only one handle per peripheral.

    ``device`` must be a native discovered Bleak device. ``timeout`` bounds
    connect/reconnect and ``operation_timeout`` bounds individual GATT calls,
    both including coordination waits. Values are finite positive seconds.
    ``services`` optionally narrows enumeration where supported. A synchronous
    ``device_resolver`` refreshes the native device before setup; returning
    ``None`` raises ``DeviceNotConnectedError``. ``connector`` replaces default
    Bleak setup with a :class:`ConnectionStrategy`.

    The handle does not own app polling or query/reply matching. Backend errors
    propagate; application writes are never replayed. A failed disconnect keeps
    its client for cleanup retry. ``is_connected`` describes the usable session,
    so it can be false even while that retained client still needs teardown.
    """

    def __init__(
        self,
        device: BLEDevice,
        *,
        timeout: float = 10.0,
        operation_timeout: float = 10.0,
        services: Iterable[str] | None = None,
        connector: ConnectionStrategy | None = None,
        device_resolver: Callable[[], BLEDevice | None] | None = None,
    ) -> None:
        _validate_timeout(timeout, "timeout")
        _validate_timeout(operation_timeout, "operation_timeout")
        self._device = device
        self._log_id = new_connection_id()
        self._logger = connection_logger(_LOGGER, self)
        self._timeout = timeout
        self._operation_timeout = operation_timeout
        self._services = tuple(services) if services is not None else None
        self._connector = connector
        self._device_resolver = device_resolver
        self._client: BleakClient | None = None
        self._lock = asyncio.Lock()
        self._session: object | None = None
        self._disconnect_callbacks: set[Callable[[], None]] = set()

    @property
    def is_connected(self) -> bool:
        """Whether the current session has an active GATT link."""
        return (
            self._session is not None
            and self._client is not None
            and self._client.is_connected
        )

    @property
    def services(self) -> Any:
        """Current GATT table; do not retain characteristics across reconnects."""
        return self._require_connected().services

    def add_disconnect_callback(
        self, callback: Callable[[], None]
    ) -> Callable[[], None]:
        """Register a no-argument observer; return an idempotent unsubscribe.

        Observers run on teardown, link loss, and failed setup, and stay
        registered across sessions until removed. Keep callbacks nonblocking.
        Their exceptions are isolated; the notification is not an app-level
        availability decision or proof a connection had been established.
        """
        self._disconnect_callbacks.add(callback)
        return lambda: self._disconnect_callbacks.discard(callback)

    def _disconnected(self, session: object) -> None:
        if self._session is not session:
            return
        self._session = None
        self._logger.debug("GATT session invalidated; disconnect observers notified")
        for callback in tuple(self._disconnect_callbacks):
            try:
                callback()
            except Exception as error:  # noqa: BLE001
                self._logger.warning(
                    "Disconnect callback failed error=%s", type(error).__name__
                )

    async def connect(self) -> Self:
        """Connect and return this handle; reuse a currently connected session.

        A new session refreshes the device and creates a fresh client. Failed
        setup retires the session and attempts cleanup before propagating the
        initiating error. Cleanup can extend the configured setup deadline.
        """
        with log_operation(self._logger, "connect", self._timeout):
            async with asyncio.timeout(self._timeout):
                async with self._lock:
                    if not self.is_connected:
                        await self._disconnect_locked()
                        await self._connect_locked()
            return self

    async def _connect_locked(self) -> None:
        device = self._device_resolver() if self._device_resolver else self._device
        if device is None:
            raise DeviceNotConnectedError("device resolver found no connectable device")
        session = object()
        self._session = session
        options: dict[str, Any] = {
            "timeout": self._timeout,
            "disconnected_callback": lambda _client: self._disconnected(session),
        }
        if self._services is not None:
            options["services"] = self._services
        try:
            if self._connector is None:
                self._client = BleakClient(device, **options)
                await self._client.connect()
            else:
                self._client = await self._connector(device, **options)
            if not self.is_connected:
                raise DeviceNotConnectedError(
                    "device disconnected during connection setup"
                )
        except BaseException:
            try:
                await self._disconnect_locked()
            except Exception as error:  # noqa: BLE001
                self._logger.debug(
                    "Connection cleanup failed error=%s", type(error).__name__
                )
            raise

    async def disconnect(self) -> None:
        """Wait for active I/O and disconnect, returning ``None``.

        Invalidate the session before backend teardown. Notifications end with
        the link. If teardown fails or is cancelled, retain the client so a
        later ``disconnect()`` can retry releasing it. Context exit may raise
        this same cleanup error; handle failures around the entire context.
        """
        with log_operation(self._logger, "disconnect", self._operation_timeout):
            async with asyncio.timeout(self._operation_timeout):
                async with self._lock:
                    await self._disconnect_locked()

    async def _disconnect_locked(self) -> None:
        client = self._client
        if self._session is not None:
            self._disconnected(self._session)
        if client is not None:
            try:
                async with asyncio.timeout(self._operation_timeout):
                    await client.disconnect()
            finally:
                # Retain a still-connected client when teardown fails so the
                # next disconnect/connect can retry releasing its adapter slot.
                if not client.is_connected:
                    self._client = None

    async def reconnect(self) -> Self:
        """Replace GATT and return this handle, refreshing the native device.

        Re-register notifications and discard old characteristic objects after
        replacement. Encrypted profiles also need a fresh handshake; H7102
        apps should call ``fan.reconnect()`` instead of this method directly.
        Queued I/O belonging to the old session fails rather than crossing over.
        """
        with log_operation(self._logger, "reconnect", self._timeout):
            async with asyncio.timeout(self._timeout):
                async with self._lock:
                    await self._disconnect_locked()
                    await self._connect_locked()
            return self

    @asynccontextmanager
    async def _operation(self, operation: str) -> AsyncIterator[BleakClient]:
        with log_operation(self._logger, operation, self._operation_timeout):
            session = self._session
            async with asyncio.timeout(self._operation_timeout):
                async with self._lock:
                    client = self._require_connected()
                    if self._session is not session:
                        raise DeviceNotConnectedError(
                            "GATT session changed while operation was queued"
                        )
                    try:
                        yield client
                    except asyncio.CancelledError:
                        # Delivery may be ambiguous. Retire this session rather
                        # than allowing queued commands to use uncertain state.
                        try:
                            await self._disconnect_locked()
                        except Exception as error:  # noqa: BLE001
                            self._logger.debug(
                                "Cancelled I/O cleanup failed error=%s",
                                type(error).__name__,
                            )
                        raise

    async def read(self, characteristic: str | BleakGATTCharacteristic) -> bytes:
        """Read ATT bytes from a UUID or current-session characteristic.

        Return immutable ``bytes``. Profile state queries usually use writes
        and notifications instead; a characteristic may reject ATT reads even
        if it advertises the read property. Backend failures propagate.
        """
        async with self._operation("read") as client:
            data = bytes(await client.read_gatt_char(characteristic))
            self._logger.debug("GATT read bytes=%d", len(data))
            return data

    async def write(
        self,
        characteristic: str | BleakGATTCharacteristic,
        data: bytes,
        *,
        response: bool | None = None,
    ) -> None:
        """Write immutable bytes once to a UUID or current characteristic.

        ``response=None`` delegates ATT response selection to Bleak. A write
        response does not confirm an applied device setting, and this method
        does not wait for an application notification. A cancelled/timed-out
        write may already have reached the device and is never replayed.
        """
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        async with self._operation("write") as client:
            self._logger.debug("GATT write bytes=%d response=%s", len(data), response)
            await client.write_gatt_char(characteristic, data, response=response)

    async def start_notify(
        self,
        characteristic: str | BleakGATTCharacteristic,
        callback: Any,
    ) -> None:
        """Subscribe before queries using a Bleak ``(characteristic, data)`` callback.

        The callback may be synchronous or async; copy retained ``bytearray``
        data and keep processing short. Your app owns callback queues/tasks.
        This raw wrapper does not bound them. Re-subscribe after reconnect.
        """
        async with self._operation("start_notify") as client:
            await client.start_notify(characteristic, callback)

    async def stop_notify(self, characteristic: str | BleakGATTCharacteristic) -> None:
        """Stop notifications on the current session."""
        async with self._operation("stop_notify") as client:
            await client.stop_notify(characteristic)

    async def __aenter__(self) -> Self:
        return await self.connect()

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.disconnect()

    def _require_connected(self) -> BleakClient:
        if not self.is_connected:
            raise DeviceNotConnectedError("device is not connected")
        assert self._client is not None
        return self._client
