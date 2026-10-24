"""Interactively verify H7102 BLE reads and captured control commands.

Run with ``PYTHONPATH=src python examples/verify_h7102_ble.py``. This script
connects directly over BLE, sends commands only after confirmation, checks BLE
readback where available, and asks the operator to compare with Govee Home or
the cloud API. Global oscillation readback uses the observed AA 1F 01 query.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from bleak.exc import BleakError

from govee_ble_client import (
    DeviceFilter,
    GoveeScanner,
    H7102Client,
    H7102Notification,
    H7102WorkMode,
)
from govee_ble_client.exceptions import GoveeBLEError

RESPONSE_TIMEOUT = 5.0
STATE_FIELDS = {"power", "mode", "speed", "custom", "display", "oscillation"}


@dataclass(slots=True)
class TestResult:
    name: str
    ble_result: str
    app_match: str


async def main() -> None:
    print("Scanning for nearby H7102 advertisements...")
    devices = await GoveeScanner().scan(
        timeout=10, device_filter=DeviceFilter(name_contains="H7102")
    )
    if not devices:
        print("No H7102 advertisements found. Move closer and try again.")
        return

    for index, device in enumerate(devices):
        print(f"[{index}] {device.name!r} RSSI={device.rssi}")
    selected = (await _ainput("Index of the H7102 to verify: ")).strip()
    try:
        device = devices[int(selected)]
    except (ValueError, IndexError):
        print("Invalid device index.")
        return
    if "H7102" not in (device.name or "").upper():
        print("The selected advertisement name does not match H7102.")
        return

    print(
        "This connects directly over BLE. Disconnect Govee Home first. "
        "The script can change power, mode, speed, Custom settings, display, "
        "and oscillation. Each command requires confirmation. It offers to "
        "restore the initial readable state at the end."
    )
    if (await _ainput("Continue? [y/N] ")).strip().casefold() != "y":
        return

    results: list[TestResult] = []

    async with device.connect(timeout=15) as connection:
        client = H7102Client(connection, on_send=_print_tx)
        async with client:
            print("Encrypted session established.")
            print("Requesting initial state...")
            await _read_snapshot(client)
            await _compare_with_app(results, "request_state() initial snapshot")
            initial_state = client.state
            initial_oscillation = initial_state.oscillation_state
            if initial_oscillation is None:
                print("No initial oscillation reply; its state cannot be restored.")

            async def run_step(
                name: str,
                operation: Callable[[], Awaitable[None]],
                *,
                field: str | None = None,
                expected: object = None,
                expected_fields: dict[str, object] | None = None,
                readback: bool = True,
                confirmation: str = "Press Enter to send, or type s to skip: ",
            ) -> None:
                print(f"\n=== {name} ===")
                if (await _ainput(confirmation)).strip().casefold() == "s":
                    results.append(TestResult(name, "skipped", "skipped"))
                    print("Skipped.")
                    return

                try:
                    if not connection.is_connected:
                        print(
                            "BLE disconnected while waiting for input; "
                            "reconnecting and refreshing state."
                        )
                        await client.reconnect(timeout=15)
                        await _read_snapshot(client)
                    await operation()
                    notifications: list[H7102Notification] = []
                    if readback:
                        # Let the fan apply a control command before asking for
                        # state. A few H7102 writes do not notify on their own.
                        await asyncio.sleep(0.75)
                        notifications = await _read_snapshot(client)
                    else:
                        notifications = await _collect_notifications(
                            client, timeout=0.8, require_fields=set()
                        )
                except (
                    BleakError,
                    GoveeBLEError,
                    TimeoutError,
                    ValueError,
                    TypeError,
                ) as error:
                    message = f"BLE operation failed: {error}"
                    print(message)
                    results.append(TestResult(name, message, "not compared"))
                    return

                checks = expected_fields or (
                    {field: expected} if field is not None else {}
                )
                if not checks:
                    ble_result = "command sent; no automated state readback available"
                else:
                    actual_values = {
                        check_field: _last_field(notifications, check_field)
                        for check_field in checks
                    }
                    if any(value is None for value in actual_values.values()):
                        ble_result = "no decodable readback received"
                    elif all(
                        actual_values[key] == expected_value
                        for key, expected_value in checks.items()
                    ):
                        printable = ", ".join(
                            f"{key}={_format_value(value)}"
                            for key, value in actual_values.items()
                        )
                        ble_result = f"readback matched ({printable})"
                    else:
                        differences = "; ".join(
                            f"{key}: expected {_format_value(expected_value)}, "
                            f"received {_format_value(actual_values[key])}"
                            for key, expected_value in checks.items()
                            if actual_values[key] != expected_value
                        )
                        ble_result = f"readback mismatch: {differences}"
                print(f"BLE check: {ble_result}")
                await _compare_with_app(results, name, ble_result=ble_result)

            # H7102 may ignore mode and speed changes while powered off. Put
            # power on before testing those controls; the final restore step
            # returns it to the state captured above.
            await run_step(
                "turn_on() before control checks + power readback",
                lambda: client.turn_on(),
                field="power",
                expected=True,
            )

            mode_steps = (
                (H7102WorkMode.NORMAL, "Normal"),
                (H7102WorkMode.SLEEP, "Sleep"),
                (H7102WorkMode.NATURE, "Nature"),
            )
            for mode, label in mode_steps:
                await run_step(
                    f"set_work_mode({label}) + readback",
                    lambda mode=mode: client.set_work_mode(mode),
                    field="mode",
                    expected=mode,
                )

            await run_step(
                "set_work_mode(Normal) before speed checks",
                lambda: client.set_work_mode(H7102WorkMode.NORMAL),
                field="mode",
                expected=H7102WorkMode.NORMAL,
            )

            for speed in (5, 6, 7, 8):
                await run_step(
                    f"set_fan_speed({speed}) + readback",
                    lambda speed=speed: client.set_fan_speed(speed),
                    field="speed",
                    expected=speed,
                )

            if initial_state.custom_settings is not None:

                async def restore_custom_settings_for_test() -> None:
                    await client.set_custom_settings(
                        initial_state.custom_settings or b""
                    )

                await run_step(
                    "set_custom_settings(current block) + readback",
                    restore_custom_settings_for_test,
                    expected_fields={
                        "mode": H7102WorkMode.CUSTOM,
                        "custom": initial_state.custom_settings,
                    },
                )
            else:
                results.append(
                    TestResult(
                        "set_custom_settings() + readback",
                        "skipped: no Custom settings block was read",
                        "skipped",
                    )
                )

            if initial_state.custom_schedule is not None:
                print(
                    "\nEnter three speed / duration / oscillation entries. "
                    "Durations are minutes; c means Continuous. Use on/off. "
                    "Example: 5,50,on;7,60,off;3,c,off."
                )
                custom_input = (
                    await _ainput("Custom segments [Enter to skip]: ")
                ).strip()
                if custom_input:
                    try:
                        custom_segments = _parse_custom_segments(custom_input)
                        custom_block = _encode_custom_settings(
                            custom_segments,
                            marker=initial_state.custom_schedule.marker,
                        )
                    except ValueError as error:
                        print(f"Invalid Custom schedule: {error}")
                        results.append(
                            TestResult(
                                "set_custom_schedule() + readback",
                                f"invalid input: {error}",
                                "not compared",
                            )
                        )
                    else:
                        await run_step(
                            "set_custom_schedule() + readback",
                            lambda: client.set_custom_schedule(
                                custom_segments,
                                marker=initial_state.custom_schedule.marker,
                            ),
                            expected_fields={
                                "mode": H7102WorkMode.CUSTOM,
                                "custom": custom_block,
                            },
                        )
                else:
                    results.append(
                        TestResult(
                            "set_custom_schedule() + readback",
                            "skipped: no values entered",
                            "skipped",
                        )
                    )

            for enabled in (True, False):
                await run_step(
                    f"set_panel_display({enabled}) + readback",
                    lambda enabled=enabled: client.set_panel_display(enabled),
                    field="display",
                    expected=enabled,
                )

            for enabled in (True, False):
                await run_step(
                    f"set_oscillation({enabled}) + readback",
                    lambda enabled=enabled: client.set_oscillation(enabled),
                    field="oscillation",
                    expected=enabled,
                )

            await run_step(
                "turn_off() + power readback",
                lambda: client.turn_off(),
                field="power",
                expected=False,
            )
            await run_step(
                "turn_on() + power readback",
                lambda: client.turn_on(),
                field="power",
                expected=True,
            )

            async def restore_initial_state() -> None:
                initial_was_custom = initial_state.work_mode is H7102WorkMode.CUSTOM
                if initial_state.custom_settings is not None and not initial_was_custom:
                    await client.set_custom_settings(initial_state.custom_settings)
                if (
                    initial_state.fan_speed is not None
                    and 1 <= initial_state.fan_speed <= 8
                ):
                    await client.set_fan_speed(initial_state.fan_speed)
                if initial_state.custom_settings is not None and initial_was_custom:
                    await client.set_custom_settings(initial_state.custom_settings)
                if (
                    initial_state.work_mode is not None
                    and initial_state.work_mode is not H7102WorkMode.CUSTOM
                ):
                    await client.set_work_mode(initial_state.work_mode)
                if initial_state.display_state is not None:
                    await client.set_panel_display(initial_state.display_state)
                if initial_oscillation is not None:
                    await client.set_oscillation(initial_oscillation)
                if initial_state.power_state is False:
                    await client.turn_off()
                else:
                    await client.turn_on()

            restore_expectations: dict[str, object] = {}
            if initial_state.power_state is not None:
                restore_expectations["power"] = initial_state.power_state
            if initial_state.work_mode is not None and (
                initial_state.work_mode is not H7102WorkMode.CUSTOM
                or initial_state.custom_settings is not None
            ):
                restore_expectations["mode"] = initial_state.work_mode
            if (
                initial_state.fan_speed is not None
                and 1 <= initial_state.fan_speed <= 8
            ):
                restore_expectations["speed"] = initial_state.fan_speed
            if initial_state.display_state is not None:
                restore_expectations["display"] = initial_state.display_state
            if initial_state.custom_settings is not None:
                restore_expectations["custom"] = initial_state.custom_settings
            if initial_oscillation is not None and initial_state.power_state is True:
                restore_expectations["oscillation"] = initial_oscillation

            await run_step(
                "restore initial readable state",
                restore_initial_state,
                expected_fields=restore_expectations,
                confirmation=(
                    "Restore the initial power/mode/speed/display state and the "
                    "read oscillation state? [Enter/s=skip]: "
                ),
            )

    print("\n=== Verification summary ===")
    for result in results:
        print(f"{result.name}: BLE={result.ble_result}; App/cloud={result.app_match}")


async def _read_snapshot(client: H7102Client) -> list[H7102Notification]:
    # A write may already have produced notifications before this snapshot is
    # requested. Drain those first so old values cannot satisfy the readback.
    while True:
        try:
            notification = await client.receive(timeout=0.05)
        except TimeoutError:
            break
        print(
            "  Ignoring notification queued before fresh snapshot: "
            f"{notification.raw.hex()} | {_describe(notification)}"
        )
    await client.request_state()
    return await _collect_notifications(
        client, timeout=RESPONSE_TIMEOUT, require_fields=set(STATE_FIELDS)
    )


async def _collect_notifications(
    client: H7102Client,
    *,
    timeout: float,
    require_fields: set[str],
) -> list[H7102Notification]:
    deadline = asyncio.get_running_loop().time() + timeout
    received: list[H7102Notification] = []
    seen: set[str] = set()
    while asyncio.get_running_loop().time() < deadline and (
        not require_fields <= seen or not received
    ):
        remaining = deadline - asyncio.get_running_loop().time()
        try:
            notification = await client.receive(timeout=min(0.5, remaining))
        except TimeoutError:
            continue
        received.append(notification)
        print(f"  RX: {notification.raw.hex()} | {_describe(notification)}")
        seen.update(_fields_in(notification))
    if not received:
        print("  RX: no notification received before timeout")
    missing = require_fields - seen
    if missing:
        print(f"  Missing readbacks: {', '.join(sorted(missing))}")
    return received


def _fields_in(notification: H7102Notification) -> set[str]:
    fields: set[str] = set()
    if notification.power_state is not None:
        fields.add("power")
    if notification.mode_code is not None:
        fields.add("mode")
    if notification.fan_speed is not None:
        fields.add("speed")
    if notification.mode_selector == 2 and notification.mode_selector_data is not None:
        fields.add("custom")
    if notification.display_state is not None:
        fields.add("display")
    if notification.oscillation_state is not None:
        fields.add("oscillation")
    return fields


def _last_field(notifications: Sequence[H7102Notification], field: str) -> object:
    for item in reversed(notifications):
        if field == "power" and item.power_state is not None:
            return item.power_state
        if field == "mode" and item.mode_code is not None:
            return item.work_mode
        if field == "speed" and item.fan_speed is not None:
            return item.fan_speed
        if field == "custom" and item.mode_selector == 2:
            return item.mode_selector_data
        if field == "display" and item.display_state is not None:
            return item.display_state
        if field == "oscillation" and item.oscillation_state is not None:
            return item.oscillation_state
    return None


def _describe(notification: H7102Notification) -> str:
    if notification.power_state is not None:
        return f"power={'on' if notification.power_state else 'off'}"
    if notification.mode_code is not None:
        mode = notification.work_mode
        mode_name = mode.value if mode else "unmapped"
        return f"work mode={mode_name} (code={notification.mode_code})"
    if notification.fan_speed is not None:
        return f"fan speed={notification.fan_speed}/8"
    if notification.display_state is not None:
        state = "on" if notification.display_state else "off"
        return f"panel display={state}"
    if notification.oscillation_state is not None:
        state = "on" if notification.oscillation_state else "off"
        return f"global oscillation={state}"
    if notification.mode_selector == 2:
        data = notification.mode_selector_data or b""
        schedule = notification.custom_schedule
        if schedule is not None:
            segments = ", ".join(
                _format_custom_segment(
                    segment.speed,
                    segment.oscillate,
                    segment.duration_minutes,
                    segment.is_continuous,
                    segment.duration_raw,
                )
                for segment in schedule.segments
            )
            return (
                f"Custom settings marker=0x{schedule.marker:02x}; {segments}; "
                f"raw={data.hex()}"
            )
        return f"Custom settings={data.hex()} (unrecognized layout)"
    if notification.mode_selector is not None:
        return (
            f"mode selector {notification.mode_selector} data="
            f"{(notification.mode_selector_data or b'').hex()}"
        )
    return f"frame {notification.prefix:02x}/{notification.command:02x}"


def _print_tx(frame: bytes) -> None:
    print(
        f"  TX app frame {frame[0]:02x}/{frame[1]:02x}: {frame.hex()}",
        flush=True,
    )


def _format_value(value: object) -> str:
    if isinstance(value, H7102WorkMode):
        return value.value
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def _format_custom_segment(
    speed: int,
    oscillate: bool,
    duration_minutes: int | None,
    continuous: bool,
    duration_raw: bytes,
) -> str:
    if continuous:
        duration = "Continuous"
    elif duration_minutes is not None:
        hours, minutes = divmod(duration_minutes, 60)
        duration = f"{hours}h {minutes}m" if hours else f"{minutes}m"
    else:
        duration = f"unrecognized duration {duration_raw.hex()}"
    oscillation = "on" if oscillate else "off"
    return f"speed {speed}/8, oscillate {oscillation} / {duration}"


def _parse_custom_segments(
    value: str,
) -> tuple[tuple[int, int | None, bool], ...]:
    parts = [part.strip() for part in value.split(";")]
    if len(parts) != 3:
        raise ValueError("enter exactly three entries separated by semicolons")
    segments: list[tuple[int, int | None, bool]] = []
    for part in parts:
        fields = [field.strip() for field in part.split(",")]
        if len(fields) not in (2, 3):
            raise ValueError("each entry must be speed,duration[,oscillate]")
        speed_text, duration_text = fields[:2]
        try:
            speed = int(speed_text, 0)
        except ValueError as error:
            raise ValueError(f"invalid speed {speed_text!r}") from error
        if duration_text.casefold() in ("c", "continuous"):
            duration = None
        else:
            try:
                duration = int(duration_text)
            except ValueError as error:
                raise ValueError(f"invalid duration {duration_text!r}") from error
        if len(fields) == 3:
            oscillate_text = fields[2].casefold()
            if oscillate_text not in ("on", "off", "true", "false", "1", "0"):
                raise ValueError(
                    f"invalid oscillation state {fields[2]!r}; use on or off"
                )
            oscillate = oscillate_text in ("on", "true", "1")
        else:
            oscillate = False
        segments.append((speed, duration, oscillate))
    return tuple(segments)


def _encode_custom_settings(
    segments: Sequence[tuple[int, int | None, bool]], *, marker: int
) -> bytes:
    if len(segments) != 3:
        raise ValueError("enter exactly three segments")
    if not 0 <= marker <= 0xFF:
        raise ValueError("schedule marker must be between 0 and 255")
    settings = bytearray((marker,))
    for index, (speed, duration_minutes, oscillate) in enumerate(segments):
        if not 1 <= speed <= 8:
            raise ValueError("speeds must be between 1 and 8")
        if not isinstance(oscillate, bool):
            raise TypeError("oscillation values must be on or off")
        settings.append(speed | (0x10 if oscillate else 0))
        if duration_minutes is None:
            if index != 2:
                raise ValueError("Continuous is allowed only for segment 3")
            settings.extend(b"\xff\xff\xff\xff")
        else:
            if not 1 <= duration_minutes <= 0xFFFE:
                raise ValueError("durations must be between 1 and 65534 minutes")
            encoded = duration_minutes.to_bytes(2, "big")
            settings.extend(encoded * 2)
    return bytes(settings)


async def _compare_with_app(
    results: list[TestResult],
    name: str,
    *,
    ble_result: str = "state values decoded",
) -> None:
    answer = (
        (await _ainput("Does this match Govee Home/cloud API state? [y/n/s=skip] "))
        .strip()
        .casefold()
    )
    app_match = {"y": "match", "n": "mismatch"}.get(answer, "skipped")
    if app_match == "mismatch":
        detail = (await _ainput("Briefly note the difference: ")).strip()
        if detail:
            app_match += f" ({detail})"
    results.append(TestResult(name, ble_result, app_match))


async def _ainput(prompt: str) -> str:
    """Read terminal input without blocking BLE event processing."""
    return await asyncio.to_thread(input, prompt)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (BleakError, GoveeBLEError, TimeoutError, ValueError) as error:
        print(f"BLE/protocol operation failed: {error}")
