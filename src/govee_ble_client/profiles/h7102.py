"""Captured GATT layout for the Govee H7102 tower fan."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Self

from ..encryption import GoveeAesRc4V1, xor_checksum
from ..exceptions import UnsupportedDeviceError
from ..protocol import build_packet

if TYPE_CHECKING:
    from ..device import DiscoveredDevice, GattConnection


_LOGGER = logging.getLogger(__name__)


class H7102WorkMode(str, Enum):
    """H7102 work modes with observed AA 05 selector-0 values."""

    NORMAL = "normal"
    SLEEP = "sleep"
    NATURE = "nature"
    CUSTOM = "custom"


_MODE_CODES = {
    1: H7102WorkMode.NORMAL,
    5: H7102WorkMode.SLEEP,
    6: H7102WorkMode.NATURE,
    2: H7102WorkMode.CUSTOM,
}
_MODE_COMMAND_CODES = {mode: code for code, mode in _MODE_CODES.items()}


@dataclass(frozen=True, slots=True)
class H7102State:
    """Immutable observations merged by ``H7102Client.receive()``.

    Fields start unknown (``None``) and controls do not update them
    optimistically. Each recognized reply replaces the client's state record;
    older references remain snapshots. Reconnect resets all fields. Values
    have no automatic freshness timestamp. Global oscillation and saved Custom
    segment flags describe different settings.
    """

    power_state: bool | None = None
    work_mode: H7102WorkMode | None = None
    fan_speed: int | None = None
    display_state: bool | None = None
    oscillation_state: bool | None = None
    custom_settings: bytes | None = None
    custom_schedule: H7102CustomSchedule | None = None


@dataclass(frozen=True, slots=True)
class H7102CustomSegment:
    """One segment in the H7102 Custom schedule.

    Bit 0x10 in the raw value marks oscillation; the low nibble is the
    segment speed. This mapping is based on an app comparison of 0x15 (on)
    and 0x05 (off) at speed 5.
    """

    raw_value: int
    duration_raw: bytes

    @property
    def speed(self) -> int:
        """Return the segment speed encoded in the low nibble."""
        return self.raw_value & 0x0F

    @property
    def oscillate(self) -> bool:
        """Return the per-segment oscillation flag from bit 0x10."""
        return bool(self.raw_value & 0x10)

    @property
    def is_continuous(self) -> bool:
        """Whether the duration uses the observed Continuous sentinel."""
        return self.duration_raw == b"\xff\xff\xff\xff"

    @property
    def duration_words(self) -> tuple[int, int] | None:
        """Return the two 16-bit duration words when finite."""
        if len(self.duration_raw) != 4 or self.is_continuous:
            return None
        return (
            int.from_bytes(self.duration_raw[:2], "big"),
            int.from_bytes(self.duration_raw[2:], "big"),
        )

    @property
    def duration_minutes(self) -> int | None:
        """Return minutes when the observed duplicated duration words agree."""
        words = self.duration_words
        if words is None or words[0] != words[1]:
            return None
        return words[0]


@dataclass(frozen=True, slots=True)
class H7102CustomSchedule:
    """Three Custom segments and the preserved leading marker byte."""

    marker: int
    segments: tuple[H7102CustomSegment, H7102CustomSegment, H7102CustomSegment]


@dataclass(frozen=True, slots=True)
class H7102Notification:
    """Decrypted 20-byte application frame from the H7102 session."""

    raw: bytes
    prefix: int
    command: int
    payload: bytes

    @property
    def power_state(self) -> bool | None:
        """Return power from a recognized AA 01 status reply."""
        if self.prefix != 0xAA or self.command != 0x01 or not self.payload:
            return None
        if self.payload[0] not in (0, 1):
            return None
        return self.payload[0] == 1

    @property
    def display_state(self) -> bool | None:
        """Return the observed AA 16 binary display state, when present."""
        if self.prefix != 0xAA or self.command != 0x16 or not self.payload:
            return None
        if self.payload[0] not in (0, 1):
            return None
        return self.payload[0] == 1

    @property
    def oscillation_state(self) -> bool | None:
        """Read global oscillation from AA 1F selector 1, state 00/01.

        Six off and six on replies matched two physical off/on cycles in
        powered-on Normal mode at speed 5. Other modes and speeds are unverified.
        Command acknowledgements and saved Custom flags are not live state.
        """
        if (
            self.prefix != 0xAA
            or self.command != 0x1F
            or len(self.payload) < 2
            or self.payload[0] != 1
            or self.payload[1] not in (0, 1)
        ):
            return None
        return self.payload[1] == 1

    @property
    def mode_code(self) -> int | None:
        """Return the AA 05 selector-0 value for the active work mode."""
        if (
            self.prefix != 0xAA
            or self.command != 0x05
            or len(self.payload) < 2
            or self.payload[0] != 0
        ):
            return None
        return self.payload[1]

    @property
    def work_mode(self) -> H7102WorkMode | None:
        """Return the observed work-mode name when its code is recognized."""
        if self.mode_code is None:
            return None
        return _MODE_CODES.get(self.mode_code)

    @property
    def mode_selector_1_value(self) -> int | None:
        """Return selector 1's value, observed to match fan speeds 5–8."""
        if (
            self.prefix != 0xAA
            or self.command != 0x05
            or len(self.payload) < 2
            or self.payload[0] != 1
        ):
            return None
        return self.payload[1]

    @property
    def fan_speed(self) -> int | None:
        """Return the observed fan speed value from AA 05 selector 1.

        Values 5 through 8 matched the app's displayed speed. Lower values
        have not yet been verified.
        """
        return self.mode_selector_1_value

    @property
    def mode_selector(self) -> int | None:
        """Return which AA 05 selector this response carries."""
        if self.prefix != 0xAA or self.command != 0x05 or not self.payload:
            return None
        return self.payload[0]

    @property
    def mode_selector_data(self) -> bytes | None:
        """Return selector-specific AA 05 data after its selector byte."""
        if self.mode_selector is None:
            return None
        return self.payload[1:]

    @property
    def custom_schedule(self) -> H7102CustomSchedule | None:
        """Decode the observed selector-2 16-byte Custom settings block."""
        if self.mode_selector != 2 or self.mode_selector_data is None:
            return None
        return _parse_custom_schedule(self.mode_selector_data)


class H7102Profile:
    """Identify H7102 and validate its observed GATT characteristics.

    PacketLogger captures show AES/RC4 V1 encryption, standard 20-byte Govee
    framing, app-observed state queries, and commands for known controls.
    The AA 1F selector-1 query also returned labeled oscillation off/on states.
    """

    model = "H7102"
    service_uuid = "00010203-0405-0607-0809-0a0b0c0d1910"
    notify_uuid = "00010203-0405-0607-0809-0a0b0c0d2b10"
    write_uuid = "00010203-0405-0607-0809-0a0b0c0d2b11"
    encryption_info_uuid = "00010203-0405-0607-0809-0a0b0c0d2b12"

    # State-query writes observed in app captures or controlled device checks.
    # Notifications supply replies; these are not direct ATT read operations.
    state_queries = (
        ("power", 0x01, b""),
        ("mode selector 0", 0x05, b"\x00"),
        ("mode selector 1", 0x05, b"\x01"),
        ("mode selector 2", 0x05, b"\x02"),
        ("mode selector 3", 0x05, b"\x03"),
        ("display state", 0x16, b""),
        ("oscillation state", 0x1F, b"\x01"),
    )

    def matches(self, device: DiscoveredDevice) -> bool:
        """Match H7102 in the advertisement name, not as identity proof."""
        return self.model.casefold() in (device.name or "").casefold()

    def validate_connection(self, connection: GattConnection) -> None:
        """Raise when the connected peripheral lacks the captured H7102 layout."""
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
                and (
                    "write" in write.properties
                    or "write-without-response" in write.properties
                )
            ):
                return
            break
        raise UnsupportedDeviceError(
            "connected device does not expose the expected H7102 GATT layout"
        )

    @staticmethod
    def parse_notification(data: bytes) -> H7102Notification:
        """Parse the common frame envelope, preserving model-specific payload."""
        if len(data) != 20:
            raise ValueError("H7102 notification must contain 20 bytes")
        if xor_checksum(data) != 0:
            raise ValueError("H7102 frame has an invalid XOR checksum")
        return H7102Notification(
            raw=data,
            prefix=data[0],
            command=data[1],
            payload=data[2:-1],
        )


class H7102Client:
    """Encrypted H7102 API for state queries and captured control commands.

    The caller owns the underlying GATT connection. Use this client as a
    nested async context manager after connecting the discovered device.
    Run exactly one ``receive()`` task to parse and merge replies; queries and
    controls return after writing and never wait for applied state. ``on_send``
    is a synchronous plaintext application-frame callback invoked after a write
    returns; if it raises, delivery has already been attempted. Keep it short
    and avoid logging sensitive traffic. GATT options belong to the supplied
    connection; encrypted receive capacity is fixed at the transport default.
    """

    def __init__(
        self,
        connection: GattConnection,
        *,
        on_send: Callable[[bytes], None] | None = None,
    ) -> None:
        self._connection = connection
        self._lifecycle = asyncio.Lock()
        self._connected = False
        self._on_send = on_send
        self._profile = H7102Profile()
        self._transport = GoveeAesRc4V1(
            connection,
            write_uuid=self._profile.write_uuid,
            notify_uuid=self._profile.notify_uuid,
        )
        self.state = H7102State()

    async def connect(self, *, timeout: float = 8.0) -> Self:
        """Validate GATT, establish encryption, and return this client.

        GATT must already be connected. ``timeout`` (finite positive seconds)
        covers the handshake after lifecycle ownership is acquired, not GATT
        setup or cleanup. Successful concurrent calls share the session. For
        recovery after link loss use ``reconnect()``, not this method alone.
        """
        async with self._lifecycle:
            if not (self._connected and self._transport.established):
                await self._connect_locked(timeout=timeout)
        return self

    async def _connect_locked(self, *, timeout: float) -> None:
        self._profile.validate_connection(self._connection)
        try:
            await self._transport.connect(timeout=timeout)
        except BaseException:
            try:
                await self._transport.close()
            except Exception:  # noqa: BLE001 - preserve errors without logging payloads
                _LOGGER.debug("H7102 handshake cleanup failed")
            raise
        self._connected = True

    async def reconnect(self, *, timeout: float = 8.0) -> Self:
        """Replace GATT and encryption, reset state, and return this client.

        End the previous receiver and start one for the new session. GATT
        setup uses the original handle's timeout/resolver/connector; this
        method's ``timeout`` controls encryption only. No command is replayed.
        """
        async with self._lifecycle:
            await self._close_locked()
            await self._connection.reconnect()
            self._transport = GoveeAesRc4V1(
                self._connection,
                write_uuid=self._profile.write_uuid,
                notify_uuid=self._profile.notify_uuid,
            )
            self.state = H7102State()
            await self._connect_locked(timeout=timeout)
        return self

    async def close(self) -> None:
        """Close encryption and wake receivers; leave caller-owned GATT open.

        This can raise if notification cleanup fails; retain the client and
        retry cleanup before reuse. Successful repeated close is safe.
        """
        async with self._lifecycle:
            await self._close_locked()

    async def _close_locked(self) -> None:
        self._connected = False
        await self._transport.close()

    async def __aenter__(self) -> Self:
        return await self.connect()

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.close()

    async def request_state(self, *, interval: float = 0.08) -> None:
        """Send seven state queries; replies are consumed separately.

        Default ``interval`` adds six 80 ms gaps; zero disables pacing. This
        method returns ``None`` and does not refresh state itself. Run one
        receiver and use required fields plus an overall deadline, rather
        than assuming exactly seven notifications complete a snapshot.
        """
        self._require_established()
        if interval < 0:
            raise ValueError("interval cannot be negative")
        for index, (_name, command, payload) in enumerate(self._profile.state_queries):
            await self._send(0xAA, command, payload)
            if interval and index < len(self._profile.state_queries) - 1:
                await asyncio.sleep(interval)

    async def request_custom_schedule(self) -> None:
        """Request the saved three-segment Custom settings with AA 05 02."""
        self._require_established()
        await self._send(0xAA, 0x05, bytes((0x02,)))

    async def request_oscillation_state(self) -> None:
        """Query global oscillation with the observed AA 1F 01 frame."""
        await self._send(0xAA, 0x1F, b"\x01")

    async def receive(self, *, timeout: float | None = None) -> H7102Notification:
        """Return one notification and replace state with recognized updates.

        One consumer per session is required. ``timeout=None`` waits without
        a deadline; otherwise pass finite positive seconds. A receive timeout
        alone leaves the session open. Malformed data raises ``ValueError``;
        link loss, overflow, or replacement invalidates the old stream. No
        automatic receiver task, polling loop, or freshness tracking is started.
        """
        self._require_established()
        transport = self._transport
        raw = await transport.receive(timeout=timeout)
        if transport is not self._transport or not transport.established:
            raise RuntimeError("H7102 session changed while receiving")
        notification = self._profile.parse_notification(raw)
        updates: dict[str, object] = {}
        if notification.power_state is not None:
            updates["power_state"] = notification.power_state
        if notification.mode_code is not None:
            updates["work_mode"] = notification.work_mode
        if notification.fan_speed is not None:
            updates["fan_speed"] = notification.fan_speed
        if notification.display_state is not None:
            updates["display_state"] = notification.display_state
        if notification.oscillation_state is not None:
            updates["oscillation_state"] = notification.oscillation_state
        if (
            notification.mode_selector == 2
            and notification.mode_selector_data is not None
        ):
            updates["custom_settings"] = notification.mode_selector_data
            updates["custom_schedule"] = notification.custom_schedule
        if updates:
            self.state = replace(self.state, **updates)
        return notification

    async def turn_on(self) -> None:
        """Turn the fan on with the captured ``33 01 01`` command."""
        await self._send(0x33, 0x01, b"\x01")

    async def turn_off(self) -> None:
        """Turn the fan off with the captured ``33 01 00`` command."""
        await self._send(0x33, 0x01, b"\x00")

    async def set_work_mode(self, mode: H7102WorkMode) -> None:
        """Set Normal, Sleep, or Nature using ``33 05 <mode-code>``.

        Sleep and Nature writes were captured directly. Normal uses its
        observed status code. Custom requires the full settings block; use
        :meth:`set_custom_settings` for that command.
        """
        if not isinstance(mode, H7102WorkMode):
            raise TypeError("mode must be an H7102WorkMode")
        if mode is H7102WorkMode.CUSTOM:
            raise ValueError("Custom mode requires settings; call set_custom_settings")
        await self._send(0x33, 0x05, bytes((_MODE_COMMAND_CODES[mode],)))

    async def set_fan_speed(self, speed: int) -> None:
        """Set speed 1–8 with captured ``33 05 01 <speed>`` framing.

        The frame shape is captured at speed 8. Readback has matched app
        settings 5–8; values 1–4 still need device verification.
        """
        if isinstance(speed, bool) or not isinstance(speed, int) or not 1 <= speed <= 8:
            raise ValueError("speed must be an integer from 1 to 8")
        await self._send(0x33, 0x05, bytes((0x01, speed)))

    async def set_custom_settings(self, settings: bytes) -> None:
        """Select Custom and write its raw, captured 16-byte settings block.

        The block follows selector ``0x02``. Speed, duration, and per-segment
        oscillation are decoded; the marker and reserved bits remain uncertain.
        Prefer ``set_custom_schedule`` for typed entries or use bytes read from
        this device. Changed schedules still need hardware round-trip checks.
        """
        if not isinstance(settings, bytes):
            raise TypeError("settings must be bytes")
        if len(settings) != 16:
            raise ValueError("H7102 Custom settings must contain exactly 16 bytes")
        await self._send(0x33, 0x05, b"\x02" + settings)

    async def set_custom_schedule(
        self,
        segments: Sequence[tuple[int, int | None] | tuple[int, int | None, bool]],
        *,
        marker: int = 0,
    ) -> None:
        """Write three H7102 Custom value/duration segments.

        Each entry is ``(speed, duration_minutes, oscillate)``. Two-item
        entries remain accepted and default oscillation to off. The oscillation
        flag is encoded as bit 0x10 in the speed byte. ``None`` selects
        Continuous and is allowed on segment three only. Finite durations are
        integers 1–65534 minutes in duplicated big-endian words. The integer
        marker is 0–255; preserve readback's marker when retaining settings.
        This method does not wait for a readback or establish hardware support
        for unverified speed/duration combinations.
        """
        if len(segments) != 3:
            raise ValueError("a Custom schedule must contain exactly three segments")
        if (
            isinstance(marker, bool)
            or not isinstance(marker, int)
            or not 0 <= marker <= 0xFF
        ):
            raise ValueError("marker must be an integer from 0 to 255")

        settings = bytearray((marker,))
        for index, segment in enumerate(segments):
            if len(segment) not in (2, 3):
                raise ValueError(
                    "each segment needs speed, duration, and optional oscillation"
                )
            speed, duration_minutes = segment[:2]
            oscillate = segment[2] if len(segment) == 3 else False
            if (
                isinstance(speed, bool)
                or not isinstance(speed, int)
                or not 1 <= speed <= 8
            ):
                raise ValueError("each speed must be an integer from 1 to 8")
            if not isinstance(oscillate, bool):
                raise TypeError("segment oscillation must be a bool")
            settings.append(speed | (0x10 if oscillate else 0))
            if duration_minutes is None:
                if index != 2:
                    raise ValueError(
                        "Continuous duration is only supported for segment 3"
                    )
                settings.extend(b"\xff\xff\xff\xff")
                continue
            if (
                isinstance(duration_minutes, bool)
                or not isinstance(duration_minutes, int)
                or not 1 <= duration_minutes <= 0xFFFE
            ):
                raise ValueError(
                    "finite duration must be an integer from 1 to 65534 minutes"
                )
            encoded = duration_minutes.to_bytes(2, "big")
            settings.extend(encoded * 2)

        await self.set_custom_settings(bytes(settings))

    async def set_panel_display(self, enabled: bool) -> None:
        """Set panel display using captured ``33 16`` on/off commands."""
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a bool")
        await self._send(0x33, 0x16, bytes((int(enabled),)) + b"\xff" * 4)

    async def set_oscillation(self, enabled: bool) -> None:
        """Set oscillation with captured ``33 1F 01`` on/off commands.

        Follow with request_oscillation_state and receive for device readback.
        """
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a bool")
        await self._send(0x33, 0x1F, bytes((0x01, int(enabled))))

    async def _send(self, prefix: int, command: int, payload: bytes) -> None:
        transport = self._transport
        async with self._lifecycle:
            if transport is not self._transport:
                raise RuntimeError("H7102 session changed while send was queued")
            self._require_established()
            frame = build_packet(prefix, command, payload)
            await transport.write(frame)
            if self._on_send is not None:
                self._on_send(frame)

    def _require_established(self) -> None:
        if not self._transport.established:
            raise RuntimeError("H7102 encrypted session is not connected")


def _parse_custom_schedule(data: bytes) -> H7102CustomSchedule | None:
    """Decode the observed marker + three speed/duration groups."""
    if len(data) != 16:
        return None
    segments = tuple(
        H7102CustomSegment(
            raw_value=data[offset],
            duration_raw=data[offset + 1 : offset + 5],
        )
        for offset in (1, 6, 11)
    )
    return H7102CustomSchedule(marker=data[0], segments=segments)  # type: ignore[arg-type]
