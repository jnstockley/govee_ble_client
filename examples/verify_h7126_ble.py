"""Interactively exercise the public H7126 BLE profile operations.

Run with ``PYTHONPATH=src python examples/verify_h7126_ble.py``. This script
talks to the purifier over BLE. It does not call Govee's cloud API; after each
test it asks the operator to compare the BLE result with Govee Home or a cloud
API state response.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Self

from bleak.exc import BleakError

from govee_ble_client import (
    DeviceFilter,
    GoveeScanner,
    H7126CustomSchedule,
    H7126Mode,
    H7126Notification,
    H7126Profile,
)
from govee_ble_client.device import DiscoveredDevice, GattConnection
from govee_ble_client.exceptions import GoveeBLEError

PROFILE = H7126Profile()
RESPONSE_WAIT = 0.8


class RecordingConnection:
    """Log writes and reconnect after the user pauses to compare cloud state."""

    def __init__(
        self,
        device: DiscoveredDevice,
        notification_callback: Callable[[int, bytearray], None],
    ) -> None:
        self._device = device
        self._notification_callback = notification_callback
        self._connection: GattConnection | None = None
        self.writes: list[tuple[str, bytes]] = []

    @property
    def services(self) -> Any:
        if self._connection is None:
            raise GoveeBLEError("BLE connection is not open")
        return self._connection.services

    @property
    def is_connected(self) -> bool:
        return self._connection is not None and self._connection.is_connected

    async def __aenter__(self) -> Self:
        await self._connect()
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.disconnect()

    async def ensure_connected(self) -> None:
        if self.is_connected:
            return
        print("  BLE connection dropped; reconnecting before the next step.")
        await self.disconnect()
        await self._connect()

    async def _connect(self) -> None:
        connection = self._device.connect(timeout=15)
        self._connection = await connection.connect()
        try:
            await self._connection.start_notify(
                PROFILE.notify_uuid, self._notification_callback
            )
        except Exception:
            await self._connection.disconnect()
            self._connection = None
            raise

    async def disconnect(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        if connection.is_connected:
            try:
                await connection.stop_notify(PROFILE.notify_uuid)
            finally:
                await connection.disconnect()

    async def write(self, characteristic: str, data: bytes, **kwargs: Any) -> None:
        await self.ensure_connected()
        frame = bytes(data)
        self.writes.append((characteristic, frame))
        print(f"  TX {characteristic[-4:]}: {frame.hex()}", flush=True)
        if self._connection is None:
            raise GoveeBLEError("BLE connection is not open")
        await self._connection.write(characteristic, frame, **kwargs)


@dataclass(slots=True)
class TestResult:
    name: str
    ble_result: str
    cloud_match: str


async def main() -> None:
    print("Scanning for nearby H7126 advertisements...")
    devices = await GoveeScanner().scan(
        timeout=10, device_filter=DeviceFilter(name_contains="H7126")
    )
    if not devices:
        print("No H7126 advertisements found.")
        return

    for index, device in enumerate(devices):
        print(f"[{index}] {device.name!r} RSSI={device.rssi}")
    selected = (await _ainput("Index of the H7126 to verify: ")).strip()
    try:
        device = devices[int(selected)]
    except (ValueError, IndexError):
        print("Invalid device index.")
        return
    if not PROFILE.matches(device):
        print("The selected advertisement name does not match H7126.")
        return

    print(
        "This connects directly over BLE. Disconnect Govee Home first. "
        "The script will change power, mode, Custom schedule, and panel display. "
        "If every step runs, it finishes on in Custom mode, with the test "
        "schedule and panel display off."
    )
    if (await _ainput("Continue? [y/N] ")).strip().casefold() != "y":
        return

    results: list[TestResult] = []
    received: list[bytes] = []

    def on_notification(_sender: int, data: bytearray) -> None:
        received.append(bytes(data))

    async with RecordingConnection(device, on_notification) as recorder:

        async def run_step(
            name: str,
            operation: Callable[[], Awaitable[None]],
            *,
            changes_device: bool = False,
            verify: Callable[[Sequence[H7126Notification]], bool | None] | None = None,
            verification_success: str = "readback matched requested state",
            confirmation_prompt: str = "Press Enter to send, or type s to skip: ",
        ) -> bool:
            print(f"\n=== {name} ===")
            if changes_device:
                answer = (await _ainput(confirmation_prompt)).strip()
                if answer.casefold() == "s":
                    results.append(TestResult(name, "skipped", "skipped"))
                    print("Skipped.")
                    return False

            first_write = len(recorder.writes)
            first_notification = len(received)
            try:
                await recorder.ensure_connected()
                await operation()
            except (BleakError, GoveeBLEError, ValueError, TypeError) as error:
                message = f"write failed: {error}"
                print(message)
                results.append(TestResult(name, message, "not compared"))
                return False

            await asyncio.sleep(RESPONSE_WAIT)
            packets = received[first_notification:]
            notifications = _parse_packets(packets)
            writes = recorder.writes[first_write:]
            _print_packets(packets, notifications)
            _print_human_values(notifications)
            _print_echo_results(writes, packets, expect_echo=changes_device)

            if verify is None:
                ble_result = "sent; no automated state expectation"
            else:
                verified = verify(notifications)
                ble_result = (
                    verification_success
                    if verified is True
                    else "readback did not match requested state"
                    if verified is False
                    else "no decodable readback received"
                )
            print(f"BLE check: {ble_result}")

            cloud_answer = (
                (
                    await _ainput(
                        "Does this match Govee Home/cloud API state? [y/n/s=skip] "
                    )
                )
                .strip()
                .casefold()
            )
            cloud_match = {"y": "match", "n": "mismatch"}.get(cloud_answer, "skipped")
            if cloud_match == "mismatch":
                detail = (await _ainput("Briefly note the difference: ")).strip()
                if detail:
                    cloud_match += f" ({detail})"
            results.append(TestResult(name, ble_result, cloud_match))
            return True

        async def query_power() -> None:
            await PROFILE.request_power_state(recorder)

        async def query_filter() -> None:
            await PROFILE.request_filter_life(recorder)

        async def query_mode_category() -> None:
            await PROFILE.request_mode_hint(recorder)

        async def query_gear_mode() -> None:
            await PROFILE.request_gear_mode(recorder)

        async def query_custom_schedule() -> None:
            await PROFILE.request_custom_mode_settings(recorder)

        async def query_display() -> None:
            await PROFILE.request_panel_display_state(recorder)

        def decoded(
            predicate: Callable[[H7126Notification], bool],
        ) -> Callable[[Sequence[H7126Notification]], bool | None]:
            def check(messages: Sequence[H7126Notification]) -> bool | None:
                if not messages:
                    return None
                return any(predicate(item) for item in messages)

            return check

        await run_step(
            "request_power_state()",
            query_power,
            verify=decoded(lambda item: item.power_state is not None),
            verification_success="power state decoded",
        )
        await run_step(
            "request_filter_life()",
            query_filter,
            verify=decoded(lambda item: item.filter_life_percent is not None),
            verification_success="filter-life percentage decoded",
        )

        async def reset_filter_and_read() -> None:
            await PROFILE.reset_filter_life(recorder)
            await asyncio.sleep(RESPONSE_WAIT)
            await PROFILE.request_filter_life(recorder)

        await run_step(
            "reset_filter_life() + 100% readback",
            reset_filter_and_read,
            changes_device=True,
            confirmation_prompt=(
                "This resets the filter counter to 100%. Only proceed after "
                "replacing the filter or for an intentional test. Press Enter "
                "to send, or type s to skip: "
            ),
            verify=decoded(lambda item: item.filter_life_percent == 100),
            verification_success="filter counter read back as 100%",
        )
        await run_step(
            "request_mode_hint()",
            query_mode_category,
            verify=decoded(lambda item: item.mode_query_response),
            verification_success=(
                "mode-family reply received; selected Sleep/Low/High value "
                "is not included in this response"
            ),
        )
        await run_step(
            "request_gear_mode()",
            query_gear_mode,
            verify=decoded(
                lambda item: (
                    item.mode in (H7126Mode.SLEEP, H7126Mode.LOW, H7126Mode.HIGH)
                )
            ),
            verification_success="selected fan mode decoded",
        )
        await run_step(
            "request_panel_display_state()",
            query_display,
            verify=decoded(lambda item: item.display_state is not None),
            verification_success="panel-display state decoded",
        )

        async def change_power(turn_on: bool) -> None:
            if turn_on:
                await PROFILE.turn_on(recorder)
            else:
                await PROFILE.turn_off(recorder)
            await asyncio.sleep(RESPONSE_WAIT)
            await PROFILE.request_power_state(recorder)

        await run_step(
            "turn_on()",
            lambda: change_power(True),
            changes_device=True,
            verify=decoded(lambda item: item.power_state is True),
        )

        mode_actions = (
            (H7126Mode.SLEEP, PROFILE.set_sleep_mode),
            (H7126Mode.LOW, PROFILE.set_low_mode),
            (H7126Mode.HIGH, PROFILE.set_high_mode),
        )
        for expected_mode, setter in mode_actions:

            async def set_and_read_mode(
                setter=setter,
            ) -> None:
                await setter(recorder)
                await asyncio.sleep(RESPONSE_WAIT)
                await PROFILE.request_mode_hint(recorder)
                await asyncio.sleep(RESPONSE_WAIT)
                await PROFILE.request_gear_mode(recorder)

            await run_step(
                f"set_{expected_mode.value}_mode() + mode readback",
                set_and_read_mode,
                changes_device=True,
                verify=decoded(
                    lambda item, expected=expected_mode: item.mode is expected
                ),
            )

        async def set_custom_and_read() -> None:
            await PROFILE.set_custom_mode(recorder)
            await asyncio.sleep(RESPONSE_WAIT)
            await PROFILE.request_custom_mode_settings(recorder)

        default_schedule = (
            (H7126Mode.HIGH, 10),
            (H7126Mode.LOW, 10),
            (H7126Mode.SLEEP, None),
        )

        await run_step(
            "set_custom_mode() + Custom settings readback",
            set_custom_and_read,
            changes_device=True,
            verify=schedule_verifier(default_schedule),
        )

        schedule = (
            (H7126Mode.SLEEP, 10),
            (H7126Mode.LOW, 70),
            (H7126Mode.HIGH, None),
        )

        async def set_schedule_and_read() -> None:
            await PROFILE.set_custom_schedule(recorder, schedule)
            await asyncio.sleep(RESPONSE_WAIT)
            await PROFILE.request_custom_mode_settings(recorder)

        await run_step(
            "set_custom_schedule() + readback (Sleep/10m, Low/70m, High/Continuous)",
            set_schedule_and_read,
            changes_device=True,
            verify=schedule_verifier(schedule),
        )

        for enabled in (True, False):

            async def set_display_and_read(enabled=enabled) -> None:
                await PROFILE.set_panel_display(recorder, enabled)
                await asyncio.sleep(RESPONSE_WAIT)
                await PROFILE.request_panel_display_state(recorder)

            await run_step(
                f"set_panel_display({enabled}) + readback",
                set_display_and_read,
                changes_device=True,
                verify=decoded(
                    lambda item, expected=enabled: item.display_state is expected
                ),
            )

        await run_step(
            "turn_off()",
            lambda: change_power(False),
            changes_device=True,
            verify=decoded(lambda item: item.power_state is False),
        )
        await run_step(
            "turn_on() (restore powered-on state)",
            lambda: change_power(True),
            changes_device=True,
            verify=decoded(lambda item: item.power_state is True),
        )

        print("\n=== Verification summary ===")
        for result in results:
            print(
                f"{result.name}: BLE={result.ble_result}; "
                f"App/cloud={result.cloud_match}"
            )


def _parse_packets(packets: Sequence[bytes]) -> list[H7126Notification]:
    decoded: list[H7126Notification] = []
    for packet in packets:
        try:
            decoded.append(PROFILE.parse_notification(packet))
        except ValueError:
            pass
    return decoded


def schedule_verifier(
    expected_schedule: Sequence[tuple[H7126Mode, int | None]],
) -> Callable[[Sequence[H7126Notification]], bool | None]:
    """Return a readback verifier for the specified three-segment schedule."""

    def verify(messages: Sequence[H7126Notification]) -> bool | None:
        for item in messages:
            observed = item.custom_schedule
            if observed is None:
                continue
            actual = tuple(
                (segment.mode, segment.duration_minutes, segment.is_continuous)
                for segment in observed.segments
            )
            expected = tuple(
                (mode, minutes, minutes is None) for mode, minutes in expected_schedule
            )
            return actual == expected
        return None

    return verify


def _print_packets(
    packets: Sequence[bytes], notifications: Sequence[H7126Notification]
) -> None:
    if not packets:
        print("  RX: no notification received before timeout")
        return
    by_raw = {item.raw: item for item in notifications}
    for packet in packets:
        notification = by_raw.get(packet)
        if notification:
            description = _describe(notification)
        elif packet.startswith(bytes.fromhex("33 17 00")):
            description = "filter-reset acknowledgement"
        else:
            description = "unparsed"
        print(f"  RX: {packet.hex()} ({description})")


def _print_human_values(notifications: Sequence[H7126Notification]) -> None:
    """Print the decoded values separately from the raw packet trace."""
    values: list[str] = []
    for item in notifications:
        if item.power_state is not None:
            values.append(f"Power: {'On' if item.power_state else 'Off'}")
        if item.mode is not None:
            values.append(f"Mode: {item.mode.value.replace('_', ' ').title()}")
        elif item.mode_query_response and item.raw.startswith(b"\xaa\x05"):
            if item.raw[2:4] == b"\x00\x01":
                values.append(
                    "Mode family: Sleep/Low/High (exact fan mode not included)"
                )
            elif item.raw[2:4] == b"\x00\x02":
                values.append("Mode family: Custom")
        if item.custom_schedule is not None:
            values.append("Custom schedule:")
            for index, segment in enumerate(item.custom_schedule.segments, start=1):
                mode = (
                    segment.mode.value.replace("_", " ").title()
                    if segment.mode is not None
                    else f"Unknown mode ({segment.mode_code})"
                )
                if segment.is_continuous:
                    duration = "Continuous"
                elif segment.duration_minutes is not None:
                    hours, minutes = divmod(segment.duration_minutes, 60)
                    if hours:
                        duration = f"{hours} hr {minutes} min"
                    else:
                        duration = f"{minutes} min"
                else:
                    duration = f"unrecognized duration ({segment.duration_raw.hex()})"
                values.append(f"  Segment {index}: {mode} for {duration}")
        if item.display_state is not None:
            values.append(f"Panel display: {'On' if item.display_state else 'Off'}")
        if item.filter_life_percent is not None:
            values.append(f"Filter life: {item.filter_life_percent}%")

    if not values:
        print("  Decoded values: none received")
        return
    print("  Decoded values:")
    for value in values:
        print(f"    {value}")


def _print_echo_results(
    writes: Sequence[tuple[str, bytes]],
    packets: Sequence[bytes],
    *,
    expect_echo: bool,
) -> None:
    if not writes:
        return
    for _characteristic, frame in writes:
        if frame in packets:
            print(f"  Exact command echo: MATCH ({frame.hex()})")
        elif frame.startswith(bytes.fromhex("33 17 01")) and any(
            packet.startswith(bytes.fromhex("33 17 00")) for packet in packets
        ):
            print("  Filter-reset acknowledgement: received (33 17 00)")
        elif not expect_echo:
            print("  Exact command echo: not expected for a query frame")
        else:
            print(
                "  Exact command echo: not seen; checking the decoded state "
                "reply instead."
            )


def _describe(notification: H7126Notification) -> str:
    values: list[str] = []
    if notification.power_state is not None:
        values.append(f"power={'on' if notification.power_state else 'off'}")
    if notification.mode is not None:
        values.append(f"mode={notification.mode.value}")
    elif notification.mode_query_response and notification.raw.startswith(b"\xaa\x05"):
        if notification.raw[2:4] == b"\x00\x01":
            values.append("mode family=Sleep/Low/High; exact fan mode not included")
        elif notification.raw[2:4] == b"\x00\x02":
            values.append("mode family=Custom")
    if notification.custom_schedule is not None:
        values.append(
            f"Custom schedule={_format_schedule(notification.custom_schedule)}"
        )
    if notification.display_state is not None:
        values.append(f"panel display={'on' if notification.display_state else 'off'}")
    if notification.filter_life_percent is not None:
        values.append(f"filter life={notification.filter_life_percent}%")
    return ", ".join(values) if values else "no decoded fields"


def _format_schedule(schedule: H7126CustomSchedule) -> str:
    parts = []
    for segment in schedule.segments:
        mode = segment.mode.value if segment.mode else f"mode-{segment.mode_code}"
        if segment.is_continuous:
            duration = "Continuous"
        elif segment.duration_minutes is not None:
            hours, minutes = divmod(segment.duration_minutes, 60)
            duration = f"{hours}h{minutes:02d}m"
        else:
            duration = segment.duration_raw.hex()
        parts.append(f"{mode}/{duration}")
    return " -> ".join(parts)


async def _ainput(prompt: str) -> str:
    """Read terminal input without blocking BLE callback processing."""
    return await asyncio.to_thread(input, prompt)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except BleakError as error:
        print(f"BLE operation failed: {error}")
