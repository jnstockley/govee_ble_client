"""H5179 measurement/battery queries and partial notification decoding."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..exceptions import UnsupportedDeviceError
from ..protocol import build_query_packet, parse_notification

if TYPE_CHECKING:
    from ..device import DiscoveredDevice, GattConnection


@dataclass(frozen=True, slots=True)
class H5179Notification:
    """One reply's readings; absent fields are ``None``, not cached values.

    Temperature is Celsius with a derived Fahrenheit property; humidity and
    battery are percentages. ``raw`` preserves the original frame. No device
    timestamp is decoded; apps may record their own receipt time.
    """

    raw: bytes
    temperature_c: float | None = None
    humidity_percent: float | None = None
    battery_percent: int | None = None

    @property
    def temperature_f(self) -> float | None:
        """Return the Celsius reading converted to Fahrenheit, if present."""
        if self.temperature_c is None:
            return None
        return self.temperature_c * 9 / 5 + 32


class H5179Profile:
    """Stateless H5179 measurement/battery query and parsing helpers.

    Subscribe to ``io_uuid`` before issuing queries. Requests validate GATT
    layout and return ``None`` after writing, not after receiving values.
    Parse callbacks and merge only non-``None`` fields in your app. This
    profile provides no settings controls, history, or stored timestamp.
    """

    model = "H5179"
    service_uuid = "494e5445-4c4c-495f-524f-434b535f4857"
    io_uuid = "494e5445-4c4c-495f-524f-434b535f2011"

    def matches(self, device: DiscoveredDevice) -> bool:
        """Match H5179 in the advertisement name, not as identity proof."""
        return self.model.casefold() in (device.name or "").casefold()

    async def request_measurement(self, connection: GattConnection) -> None:
        """Request current temperature and humidity with observed ``AA 0A``."""
        self._validate_layout(connection)
        await connection.write(self.io_uuid, build_query_packet(0x0A))

    async def request_battery(self, connection: GattConnection) -> None:
        """Request battery percentage with observed ``AA 08``."""
        self._validate_layout(connection)
        await connection.write(self.io_uuid, build_query_packet(0x08))

    @staticmethod
    def parse_notification(data: bytes) -> H5179Notification:
        """Decode ``AA 0A`` measurements or ``AA 08`` battery into a partial record.

        Common prefix/checksum validation applies; invalid humidity/battery
        ranges raise ``ValueError``. Valid unknown query codes preserve ``raw``
        and leave decoded fields ``None``. No I/O or state retention occurs.
        """
        common = parse_notification(data)
        if common.code == 0x0A and len(common.payload) >= 4:
            temperature_raw = int.from_bytes(common.payload[:2], "little", signed=True)
            humidity_raw = int.from_bytes(common.payload[2:4], "little")
            if not 0 <= humidity_raw <= 10_000:
                raise ValueError("H5179 humidity is outside the percentage range")
            return H5179Notification(
                raw=data,
                temperature_c=temperature_raw / 100,
                humidity_percent=humidity_raw / 100,
            )
        if common.code == 0x08 and common.payload:
            battery = common.payload[0]
            if battery > 100:
                raise ValueError("H5179 battery value is outside the percentage range")
            return H5179Notification(raw=data, battery_percent=battery)
        return H5179Notification(raw=data)

    def _validate_layout(self, connection: GattConnection) -> None:
        for service in connection.services:
            if service.uuid.casefold() != self.service_uuid:
                continue
            for characteristic in service.characteristics:
                if characteristic.uuid.casefold() != self.io_uuid:
                    continue
                properties = set(characteristic.properties)
                if "notify" in properties and (
                    "write" in properties or "write-without-response" in properties
                ):
                    return
            break
        raise UnsupportedDeviceError(
            "connected device does not expose the expected H5179 GATT layout"
        )
