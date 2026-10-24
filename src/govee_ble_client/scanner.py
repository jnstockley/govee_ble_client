"""BLE discovery helpers built on Bleak."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from bleak import BleakScanner

from .device import DiscoveredDevice


@dataclass(frozen=True, slots=True)
class DeviceFilter:
    """Optional advertisement filters. Empty filters return all nearby BLE devices.

    ``name_contains`` is only a convenience heuristic: device names are optional
    and are not proof that a device is made by Govee.
    """

    name_contains: str | None = None
    service_uuids: frozenset[str] = field(default_factory=frozenset)
    manufacturer_id: int | None = None
    predicate: Callable[[DiscoveredDevice], bool] | None = field(
        default=None, compare=False, repr=False
    )

    def matches(self, device: DiscoveredDevice) -> bool:
        if (
            self.name_contains
            and self.name_contains.casefold() not in (device.name or "").casefold()
        ):
            return False
        if self.service_uuids and not {
            value.casefold() for value in self.service_uuids
        }.intersection(value.casefold() for value in device.service_uuids):
            return False
        if (
            self.manufacturer_id is not None
            and self.manufacturer_id not in device.manufacturer_data
        ):
            return False
        return self.predicate(device) if self.predicate else True


class GoveeScanner:
    """Discover nearby BLE advertisements without connecting to devices."""

    async def scan(
        self, *, timeout: float = 5.0, device_filter: DeviceFilter | None = None
    ) -> list[DiscoveredDevice]:
        """Return devices seen during ``timeout`` seconds.

        The scan is passive with respect to GATT: it does not connect, pair, or
        transmit device commands. The platform BLE stack controls scan details.
        """
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")

        discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
        results: list[DiscoveredDevice] = []
        for ble_device, advertisement in discovered.values():
            record = DiscoveredDevice.from_advertisement(ble_device, advertisement)
            if device_filter is None or device_filter.matches(record):
                results.append(record)
        return sorted(
            results,
            key=lambda item: item.rssi if item.rssi is not None else -1000,
            reverse=True,
        )
