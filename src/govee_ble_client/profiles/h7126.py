"""H7126 query/control frames and notification parsing backed by device evidence."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from ..exceptions import UnsupportedDeviceError
from ..protocol import build_command_packet, build_packet, build_query_packet

if TYPE_CHECKING:
    from ..device import DiscoveredDevice, GattConnection


class H7126Mode(str, Enum):
    """H7126 modes with an observed notification value."""

    SLEEP = "sleep"
    LOW = "low"
    HIGH = "high"
    CUSTOM = "custom"


@dataclass(frozen=True, slots=True)
class H7126CustomSegment:
    """One observed mode/duration entry in the H7126 Custom schedule."""

    mode_code: int
    duration_raw: bytes

    @property
    def mode(self) -> H7126Mode | None:
        """Return the known Sleep/Low/High mapping for this segment."""
        return {
            0x01: H7126Mode.SLEEP,
            0x02: H7126Mode.LOW,
            0x03: H7126Mode.HIGH,
        }.get(self.mode_code)

    @property
    def is_continuous(self) -> bool:
        """Whether the duration uses the app's captured Continuous sentinel."""
        return self.duration_raw == b"\xff\xff\xff\xff"

    @property
    def duration_words(self) -> tuple[int, int] | None:
        """Return the two captured 16-bit words; equal words represent minutes."""
        if len(self.duration_raw) != 4 or self.is_continuous:
            return None
        return (
            int.from_bytes(self.duration_raw[:2], "big"),
            int.from_bytes(self.duration_raw[2:], "big"),
        )

    @property
    def duration_minutes(self) -> int | None:
        """Return the confirmed total-minute value when both words agree."""
        words = self.duration_words
        if words is None or words[0] != words[1]:
            return None
        return words[0]


@dataclass(frozen=True, slots=True)
class H7126CustomSchedule:
    """Three observed Custom schedule segments plus an unexplained marker."""

    marker: int
    segments: tuple[H7126CustomSegment, H7126CustomSegment, H7126CustomSegment]


@dataclass(frozen=True, slots=True)
class H7126Notification:
    """One parsed notification, not a complete or accumulated purifier state.

    Optional fields are ``None`` when absent/unrecognized; retain earlier app
    observations instead of clearing them, and test booleans with ``is not
    None``. ``mode_query_response`` identifies a mode query response, not
    proof of an applied control. ``raw`` preserves the original frame.
    """

    raw: bytes
    power_state: bool | None = None
    mode: H7126Mode | None = None
    mode_query_response: bool = False
    custom_settings: bytes | None = None
    custom_schedule: H7126CustomSchedule | None = None
    display_state: bool | None = None
    filter_life_percent: int | None = None


class H7126Profile:
    """Observed BLE operations for the Govee H7126.

    Power on/off, power-state responses, Sleep/Low/High mode selection, panel
    display, and the three-segment Custom schedule have been observed in H7126
    app traffic. Custom durations are total minutes; the schedule marker is
    preserved but its meaning remains unknown.

    The profile is stateless. Subscribe to ``notify_uuid`` before requesting
    values, parse callbacks with ``parse_notification``, and retain app state
    from non-``None`` fields. Request/control methods validate GATT layout and
    return ``None`` after writing, without waiting for confirmation. Their
    backend errors propagate. Support is verified on the supplied purifier;
    firmware variants still need independent checks.
    """

    model = "H7126"
    service_uuid = "00010203-0405-0607-0809-0a0b0c0d1910"
    notify_uuid = "00010203-0405-0607-0809-0a0b0c0d2b10"
    write_uuid = "00010203-0405-0607-0809-0a0b0c0d2b11"

    def matches(self, device: DiscoveredDevice) -> bool:
        """Match the observed SKU in the advertisement name (not identity proof)."""
        return self.model.casefold() in (device.name or "").casefold()

    async def request_power_state(self, connection: GattConnection) -> None:
        """Request a power-state notification on the H7126 notify characteristic."""
        self._validate_layout(connection)
        await connection.write(self.write_uuid, build_query_packet(0x01))

    async def request_filter_life(self, connection: GattConnection) -> None:
        """Request filter life using the app-observed H7126 ``AA 19`` query.

        The percentage is response byte 6 (payload byte 4), observed at both
        0% and 100% on the H7126.
        """
        self._validate_layout(connection)
        await connection.write(self.write_uuid, build_query_packet(0x19))

    async def reset_filter_life(self, connection: GattConnection) -> None:
        """Reset the H7126 filter-life counter to 100% using ``33 17 01``.

        This changes the device's maintenance counter and should only be sent
        after a filter replacement or when intentionally testing the reset.
        """
        self._validate_layout(connection)
        await connection.write(self.write_uuid, build_command_packet(0x17, b"\x01"))

    async def request_mode_hint(self, connection: GattConnection) -> None:
        """Query the H7126 mode category with the app-observed ``AA 05 00``.

        A reply ending in ``00 01`` indicates a gear mode, not its selected
        speed; ``00 02`` was observed for Custom. Follow this with
        :meth:`request_gear_mode` to read Sleep/Low/High.
        """
        self._validate_layout(connection)
        await connection.write(self.write_uuid, build_query_packet(0x05))

    async def request_gear_mode(self, connection: GattConnection) -> None:
        """Query the selected Sleep/Low/High value using the observed ``AA 05 01``.

        A High-mode PacketLogger trace shows ``AA 05 01`` followed by the
        notification ``AA 05 01 03``. The 1/2/3 mapping follows the already
        observed H7126 mode writes and ``EE 05`` notifications.
        """
        self._validate_layout(connection)
        await connection.write(self.write_uuid, build_packet(0xAA, 0x05, b"\x01"))

    async def request_custom_mode_settings(self, connection: GattConnection) -> None:
        """Query the observed Custom-mode settings block with ``AA 05 02``.

        The H7126 response mirrors the app's Custom-mode configuration frame.
        Segment modes and durations are decoded; raw fields and the unexplained
        marker remain available for callers.
        """
        self._validate_layout(connection)
        await connection.write(self.write_uuid, build_packet(0xAA, 0x05, b"\x02"))

    async def request_panel_display_state(self, connection: GattConnection) -> None:
        """Request panel LED state with ``AA 16``, verified on the supplied H7126.

        Parse ``display_state`` from a notification; returning from this method
        confirms only that the query write completed, not that a reply arrived.
        """
        self._validate_layout(connection)
        await connection.write(self.write_uuid, build_query_packet(0x16))

    async def turn_on(self, connection: GattConnection) -> None:
        """Send the H7126 power-on frame verified against one device."""
        self._validate_layout(connection)
        await connection.write(self.write_uuid, build_command_packet(0x01, b"\x01"))

    async def turn_off(self, connection: GattConnection) -> None:
        """Turn the purifier off with the app-observed ``33 01 00`` frame."""
        self._validate_layout(connection)
        await connection.write(self.write_uuid, build_command_packet(0x01, b"\x00"))

    async def set_high_mode(self, connection: GattConnection) -> None:
        """Set High mode using the frame captured for Low → High in Govee Home."""
        await self._set_fan_mode(connection, 0x03)

    async def set_sleep_mode(self, connection: GattConnection) -> None:
        """Set Sleep mode using the frame captured in Govee Home."""
        await self._set_fan_mode(connection, 0x01)

    async def set_low_mode(self, connection: GattConnection) -> None:
        """Set Low mode using the frame captured in Govee Home."""
        await self._set_fan_mode(connection, 0x02)

    async def set_custom_mode(self, connection: GattConnection) -> None:
        """Apply the captured three-segment Custom schedule.

        The captured schedule is High for 10 minutes, Low for 10 minutes, then
        Sleep in Continuous mode. This replaces schedule settings; it does not
        restore the device's saved Custom schedule. To preserve chosen values,
        read them first and pass them to ``set_custom_schedule``.
        """
        await self.set_custom_schedule(
            connection,
            (
                (H7126Mode.HIGH, 10),
                (H7126Mode.LOW, 10),
                (H7126Mode.SLEEP, None),
            ),
        )

    async def set_custom_schedule(
        self,
        connection: GattConnection,
        segments: Sequence[tuple[H7126Mode, int | None]],
    ) -> None:
        """Set three Custom segments as ``(mode, duration_minutes)`` pairs.

        Exactly three entries are required, each using Sleep, Low, or High.
        ``None`` selects Continuous on the final segment only. Finite durations
        are integers 1–65534 minutes encoded as duplicated big-endian words;
        65535 is reserved by the Continuous sentinel. Read the settings back
        to confirm application; this method only submits the control write.
        """
        if len(segments) != 3:
            raise ValueError("a Custom schedule must contain exactly three segments")
        mode_codes = {
            H7126Mode.SLEEP: 0x01,
            H7126Mode.LOW: 0x02,
            H7126Mode.HIGH: 0x03,
        }
        payload = bytearray((0x02, 0x00))
        for index, (mode, duration_minutes) in enumerate(segments):
            try:
                payload.append(mode_codes[mode])
            except (KeyError, TypeError) as error:
                raise ValueError(
                    "Custom segment mode must be Sleep, Low, or High"
                ) from error
            if duration_minutes is None:
                if index != 2:
                    raise ValueError(
                        "Continuous duration is only supported for segment 3"
                    )
                payload.extend(b"\xff\xff\xff\xff")
                continue
            if (
                isinstance(duration_minutes, bool)
                or not isinstance(duration_minutes, int)
                or not 1 <= duration_minutes <= 0xFFFE
            ):
                raise ValueError(
                    "finite duration must be an integer from 1 to 65534 minutes"
                )
            encoded_duration = duration_minutes.to_bytes(2, "big")
            payload.extend(encoded_duration * 2)

        self._validate_layout(connection)
        await connection.write(
            self.write_uuid, build_packet(0x3A, 0x05, bytes(payload))
        )

    async def set_panel_display(
        self, connection: GattConnection, enabled: bool
    ) -> None:
        """Turn the panel display LEDs on or off using captured 0x33/0x16 frames.

        The trace captured parameter ``0x00`` and ``0x01`` while the user
        toggled the display. These are interpreted as off and on respectively.
        """
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a bool")
        self._validate_layout(connection)
        await connection.write(
            self.write_uuid,
            build_packet(0x33, 0x16, bytes([int(enabled), 0xFF, 0xFF, 0xFF, 0xFF])),
        )

    async def _set_fan_mode(self, connection: GattConnection, mode: int) -> None:
        self._validate_layout(connection)
        await connection.write(
            self.write_uuid, build_packet(0x3A, 0x05, bytes([0x01, mode]))
        )

    @staticmethod
    def parse_notification(data: bytes) -> H7126Notification:
        """Parse power, mode, Custom schedule, display, and filter-life fields.

        Return a partial ``H7126Notification`` preserving raw bytes. ``AA``
        frames use common length/checksum validation; non-``AA`` frames require
        20 bytes. Unsupported prefixes (including some command acknowledgements)
        or malformed frames raise ``ValueError``. A saved selector-2 schedule
        reply does not establish the active mode.
        """
        from ..protocol import parse_notification

        if data[:1] == b"\xaa":
            common = parse_notification(data)
            filter_life = None
            mode = None
            mode_query_response = False
            custom_settings = None
            custom_schedule = None
            display_state = None
            if (
                common.code == 0x19
                and len(common.payload) >= 5
                and common.payload[4] <= 100
            ):
                filter_life = common.payload[4]
            elif common.code == 0x05:
                mode_query_response = True
                if common.payload[:2] == b"\x00\x02":
                    mode = H7126Mode.CUSTOM
                elif common.payload[:2] == b"\x01\x01":
                    mode = H7126Mode.SLEEP
                elif common.payload[:2] == b"\x01\x02":
                    mode = H7126Mode.LOW
                elif common.payload[:2] == b"\x01\x03":
                    mode = H7126Mode.HIGH
                elif common.payload[:1] == b"\x02":
                    # This is a saved schedule response, not the active mode.
                    mode_query_response = False
                    custom_settings = common.payload[1:]
                    custom_schedule = _parse_custom_schedule(custom_settings)
            elif common.code == 0x16 and common.payload and common.payload[0] in (0, 1):
                display_state = common.payload[0] == 1
            return H7126Notification(
                raw=data,
                power_state=common.power_state,
                mode=mode,
                mode_query_response=mode_query_response,
                custom_settings=custom_settings,
                custom_schedule=custom_schedule,
                display_state=display_state,
                filter_life_percent=filter_life,
            )

        if len(data) != 20 or _xor_checksum(data) != 0:
            raise ValueError("H7126 notification length or checksum is invalid")
        if data[0] == 0x3A and data[1:3] == b"\x05\x02":
            settings = data[3:19]
            return H7126Notification(
                raw=data,
                mode=H7126Mode.CUSTOM,
                custom_settings=settings,
                custom_schedule=_parse_custom_schedule(settings),
            )
        if data[0] != 0xEE:
            raise ValueError("notification has an unexpected H7126 prefix")

        mode = None
        custom_settings = None
        custom_schedule = None
        display_state = None
        if data[1:3] == b"\x05\x01":
            mode = {
                0x01: H7126Mode.SLEEP,
                0x02: H7126Mode.LOW,
                0x03: H7126Mode.HIGH,
            }.get(data[3])
        elif data[1:3] == b"\x05\x02":
            mode = H7126Mode.CUSTOM
            custom_settings = data[3:19]
            custom_schedule = _parse_custom_schedule(custom_settings)
        elif data[1] == 0x16 and data[2] in (0x00, 0x01):
            display_state = data[2] == 0x01
        return H7126Notification(
            raw=data,
            mode=mode,
            custom_settings=custom_settings,
            custom_schedule=custom_schedule,
            display_state=display_state,
        )

    def _validate_layout(self, connection: GattConnection) -> None:
        for service in connection.services:
            if service.uuid.casefold() != self.service_uuid:
                continue
            characteristics = {
                characteristic.uuid.casefold(): characteristic
                for characteristic in service.characteristics
            }
            notify = characteristics.get(self.notify_uuid)
            write = characteristics.get(self.write_uuid)
            if (
                notify is not None
                and "notify" in notify.properties
                and write is not None
                and "write-without-response" in write.properties
            ):
                return
            break
        raise UnsupportedDeviceError(
            "connected device does not expose the expected H7126 GATT layout"
        )


def _xor_checksum(data: bytes) -> int:
    checksum = 0
    for value in data:
        checksum ^= value
    return checksum


def _parse_custom_schedule(data: bytes) -> H7126CustomSchedule | None:
    """Decode the observed marker plus three ``mode + duration`` entries."""
    if len(data) != 16:
        return None
    segments = tuple(
        H7126CustomSegment(
            mode_code=data[offset], duration_raw=data[offset + 1 : offset + 5]
        )
        for offset in (1, 6, 11)
    )
    return H7126CustomSchedule(marker=data[0], segments=segments)  # type: ignore[arg-type]
