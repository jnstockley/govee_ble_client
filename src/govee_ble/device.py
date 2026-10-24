"""Discovered-device records and a small, protocol-neutral GATT client."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from .exceptions import DeviceNotConnectedError


@dataclass(frozen=True, slots=True)
class DiscoveredDevice:
    """A device observed in an advertisement scan.

    ``identifier`` is the platform-provided identifier (often, but not always,
    a MAC address). It may be private/randomized and should not be treated as a
    permanent identity or authentication credential.
    """

    identifier: str
    name: str | None
    rssi: int | None
    manufacturer_data: dict[int, bytes] = field(default_factory=dict)
    service_data: dict[str, bytes] = field(default_factory=dict)
    service_uuids: tuple[str, ...] = ()
    _ble_device: BLEDevice = field(repr=False, compare=False, default=None)  # type: ignore[assignment]

    @classmethod
    def from_advertisement(
        cls, device: BLEDevice, advertisement: AdvertisementData
    ) -> DiscoveredDevice:
        return cls(
            identifier=device.address,
            name=advertisement.local_name or device.name,
            rssi=advertisement.rssi,
            manufacturer_data=dict(advertisement.manufacturer_data),
            service_data=dict(advertisement.service_data),
            service_uuids=tuple(advertisement.service_uuids),
            _ble_device=device,
        )

    def connect(self, *, timeout: float = 10.0) -> GattConnection:
        """Create a connection handle for ``async with`` or explicit connect."""
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        return GattConnection(self._ble_device, timeout=timeout)


class GattConnection:
    """Async context manager for protocol-neutral GATT operations.

    This exposes the discovered GATT table and raw characteristic operations.
    Application code should use a verified protocol profile to construct
    command payloads rather than guessing packet formats.
    """

    def __init__(self, device: BLEDevice, *, timeout: float = 10.0) -> None:
        self._device = device
        self._timeout = timeout
        self._client = BleakClient(device, timeout=timeout)

    @property
    def is_connected(self) -> bool:
        return self._client.is_connected

    @property
    def services(self) -> Any:
        """Bleak GATT services, available after connecting."""
        self._require_connected()
        return self._client.services

    async def connect(self) -> GattConnection:
        await self._client.connect()
        return self

    async def disconnect(self) -> None:
        if self._client.is_connected:
            await self._client.disconnect()

    async def reconnect(self) -> GattConnection:
        """Replace the Bleak client and establish a fresh GATT connection."""
        await self.disconnect()
        self._client = BleakClient(self._device, timeout=self._timeout)
        return await self.connect()

    async def read(self, characteristic: str | BleakGATTCharacteristic) -> bytes:
        self._require_connected()
        return bytes(await self._client.read_gatt_char(characteristic))

    async def write(
        self,
        characteristic: str | BleakGATTCharacteristic,
        data: bytes,
        *,
        response: bool | None = None,
    ) -> None:
        """Write bytes to a characteristic; caller owns protocol correctness."""
        self._require_connected()
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        await self._client.write_gatt_char(characteristic, data, response=response)

    async def start_notify(self, characteristic: str | BleakGATTCharacteristic, callback: Any) -> None:
        self._require_connected()
        await self._client.start_notify(characteristic, callback)

    async def stop_notify(self, characteristic: str | BleakGATTCharacteristic) -> None:
        self._require_connected()
        await self._client.stop_notify(characteristic)

    async def __aenter__(self) -> GattConnection:
        return await self.connect()

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.disconnect()

    def _require_connected(self) -> None:
        if not self._client.is_connected:
            raise DeviceNotConnectedError("device is not connected")
