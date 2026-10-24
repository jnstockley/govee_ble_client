"""Display H7126 power, mode, schedule, panel display, and filter-life observations.

Run from the repository root with
``PYTHONPATH=src python examples/watch_h7126_ble.py``.
This standalone diagnostic opens a real link and sends state queries, including
ATT read probes that the supplied purifier rejected. It has no app-level
availability/reconnect manager; use docs/APP_INTEGRATION.md for app ownership.
"""

from __future__ import annotations

import asyncio
import time

from bleak.exc import BleakError

from govee_ble_client import (
    DeviceFilter,
    GoveeScanner,
    H7126CustomSchedule,
    H7126Mode,
    H7126Profile,
)

PROFILE = H7126Profile()
POLL_INTERVAL = 1.0
RESPONSE_TIMEOUT = 0.6
STATE_REFRESH_INTERVAL = 5.0


async def main() -> None:
    print("Scanning for nearby H7126 advertisements...")
    devices = await GoveeScanner().scan(
        timeout=10, device_filter=DeviceFilter(name_contains="H7126")
    )
    if not devices:
        print("No H7126 advertisements found. Move closer and try again.")
        return

    for index, device in enumerate(devices):
        print(f"[{index}] {device.name!r} RSSI={device.rssi}")
    selection = input("Index of the H7126 to monitor: ").strip()
    try:
        device = devices[int(selection)]
    except (ValueError, IndexError):
        print("Invalid device index.")
        return

    print(
        f"Connecting to {device.name!r}. Disconnect Govee Home first; "
        "this direct BLE connection may prevent the app from connecting."
    )
    notification_event = asyncio.Event()
    other_notifications: asyncio.Queue[bytes] = asyncio.Queue()
    state_holder: dict[
        str, bool | int | float | bytes | H7126CustomSchedule | H7126Mode | None
    ] = {
        "power": None,
        "reply_time": None,
        "mode": None,
        "custom_settings": None,
        "custom_schedule": None,
        "display": None,
        "filter_life": None,
    }

    async with device.connect(timeout=15) as connection:
        await connection.start_notify(
            PROFILE.notify_uuid,
            lambda _sender, data: _on_notification(
                bytes(data), notification_event, state_holder, other_notifications
            ),
        )

        for characteristic_uuid in (PROFILE.notify_uuid, PROFILE.write_uuid):
            try:
                value = await connection.read(characteristic_uuid)
            except BleakError as error:
                print(f"Direct GATT read {characteristic_uuid[-4:]} failed: {error}")
            else:
                print(f"Direct GATT read {characteristic_uuid[-4:]}: {value.hex()}")

        print(
            "Polling once per second. Power is read from AA 01 replies; "
            "mode is read with AA 05 00/01 queries and updated from EE 05 "
            "notifications. Panel display is queried with AA 16 and updated "
            "from EE 16 notifications; filter life is decoded "
            "from zero-based frame offset 7 (payload offset 5) of AA 19 replies (observed at 0% and 100%). Startup "
            "queries the current mode, panel display, filter life, and saved "
            "Custom schedule. The schedule is read regardless of the active "
            "mode. "
            "Press Ctrl+C to stop."
        )

        startup_queries = (
            ("filter life AA 19 (app-observed)", PROFILE.request_filter_life),
            (
                "mode-category AA 05 00 (observed in H7126 app trace)",
                PROFILE.request_mode_hint,
            ),
            (
                "panel display AA 16 (device-verified)",
                PROFILE.request_panel_display_state,
            ),
        )
        for label, request in startup_queries:
            print(f"Sending {label} query.")
            notification_event.clear()
            try:
                await request(connection)
                await asyncio.wait_for(
                    notification_event.wait(), timeout=RESPONSE_TIMEOUT
                )
            except TimeoutError:
                print(f"No notification received for {label}.")
            except BleakError as error:
                print(f"{label} query failed: {error}")
            while not other_notifications.empty():
                raw_notification = other_notifications.get_nowait()
                print(
                    f"Other BLE notification (not decoded): {raw_notification.hex()}",
                    flush=True,
                )

        # The category response must arrive first: AA 05 00 02 identifies
        # Custom, while AA 05 00 01 means the gear value needs a second query.
        if state_holder["mode"] != H7126Mode.CUSTOM:
            label = "gear-mode AA 05 01 (observed in H7126 app trace)"
            print(f"Sending {label} query.")
            notification_event.clear()
            try:
                await PROFILE.request_gear_mode(connection)
                await asyncio.wait_for(
                    notification_event.wait(), timeout=RESPONSE_TIMEOUT
                )
            except TimeoutError:
                print(f"No notification received for {label}.")
            except BleakError as error:
                print(f"{label} query failed: {error}")
            while not other_notifications.empty():
                raw_notification = other_notifications.get_nowait()
                print(
                    f"Other BLE notification (not decoded): {raw_notification.hex()}",
                    flush=True,
                )
        # AA 05 02 reports the saved Custom schedule, not the active mode.
        label = "saved Custom schedule AA 05 02 (app-observed)"
        print(f"Sending {label} query.")
        notification_event.clear()
        try:
            await PROFILE.request_custom_mode_settings(connection)
            await asyncio.wait_for(notification_event.wait(), timeout=RESPONSE_TIMEOUT)
        except TimeoutError:
            print(f"No notification received for {label}.")
        except BleakError as error:
            print(f"{label} query failed: {error}")
        while not other_notifications.empty():
            raw_notification = other_notifications.get_nowait()
            print(
                f"Other BLE notification (not decoded): {raw_notification.hex()}",
                flush=True,
            )

        last_state_refresh = time.monotonic()
        try:
            while True:
                cycle_start = time.monotonic()
                notification_event.clear()
                try:
                    await PROFILE.request_power_state(connection)
                except BleakError as error:
                    print(f"Power-status query failed: {error}", flush=True)
                else:
                    try:
                        await asyncio.wait_for(
                            notification_event.wait(), timeout=RESPONSE_TIMEOUT
                        )
                    except TimeoutError:
                        pass

                if cycle_start - last_state_refresh >= STATE_REFRESH_INTERVAL:
                    try:
                        await PROFILE.request_panel_display_state(connection)
                    except BleakError as error:
                        print(f"Display refresh query failed: {error}", flush=True)
                    last_state_refresh = cycle_start

                while not other_notifications.empty():
                    raw_notification = other_notifications.get_nowait()
                    print(
                        "Other BLE notification (not decoded): "
                        f"{raw_notification.hex()}",
                        flush=True,
                    )

                current_power = state_holder["power"]
                observed_at = state_holder["reply_time"]
                current_mode = state_holder["mode"]
                current_display = state_holder["display"]
                filter_life = state_holder["filter_life"]
                custom_schedule = state_holder["custom_schedule"]
                if current_power is None or observed_at is None:
                    power_text = "unknown (no status reply yet)"
                else:
                    age = time.monotonic() - observed_at
                    power_text = (
                        f"{'on' if current_power else 'off'} (reply {age:.1f}s ago)"
                    )
                mode_text = (
                    current_mode.value
                    if isinstance(current_mode, H7126Mode)
                    else "unknown (no mode notification yet)"
                )
                display_text = (
                    ("on" if current_display else "off")
                    if isinstance(current_display, bool)
                    else "unknown (no display notification yet)"
                )
                filter_text = (
                    f"{filter_life}%"
                    if isinstance(filter_life, int)
                    else "unknown (no AA 19 response yet)"
                )
                saved_schedule_text = (
                    _format_custom_schedule(custom_schedule)
                    if isinstance(custom_schedule, H7126CustomSchedule)
                    else "unknown (no AA 05 02 response yet)"
                )

                print(
                    f"power: {power_text} | mode: {mode_text} | "
                    f"panel display: {display_text} | "
                    f"filter life: {filter_text} | "
                    f"saved Custom schedule: {saved_schedule_text}",
                    flush=True,
                )
                remaining = POLL_INTERVAL - (time.monotonic() - cycle_start)
                await asyncio.sleep(max(0.0, remaining))
        except KeyboardInterrupt:
            print("Stopping monitor.")
        finally:
            await connection.stop_notify(PROFILE.notify_uuid)


def _on_notification(
    data: bytes,
    event: asyncio.Event,
    state: dict[
        str, bool | int | float | bytes | H7126CustomSchedule | H7126Mode | None
    ],
    other_notifications: asyncio.Queue[bytes],
) -> None:
    try:
        notification = PROFILE.parse_notification(data)
    except ValueError:
        other_notifications.put_nowait(data)
        event.set()
        return
    if notification.power_state is not None:
        state["power"] = notification.power_state
        state["reply_time"] = time.monotonic()
    if notification.mode is not None:
        state["mode"] = notification.mode
    elif notification.mode_query_response:
        state["mode"] = None
    if notification.custom_settings is not None:
        state["custom_settings"] = notification.custom_settings
    if notification.custom_schedule is not None:
        state["custom_schedule"] = notification.custom_schedule
    if notification.display_state is not None:
        state["display"] = notification.display_state
    if notification.filter_life_percent is not None:
        state["filter_life"] = notification.filter_life_percent
    if (
        notification.power_state is None
        and notification.mode is None
        and notification.display_state is None
        and notification.filter_life_percent is None
        and notification.custom_schedule is None
    ):
        other_notifications.put_nowait(data)
    event.set()


def _format_custom_schedule(schedule: H7126CustomSchedule) -> str:
    """Format the three Custom segments and preserve the marker as unknown."""
    segments = []
    for index, segment in enumerate(schedule.segments, start=1):
        mode = (
            segment.mode.value
            if segment.mode is not None
            else f"mode-{segment.mode_code}"
        )
        if segment.is_continuous:
            duration = "Continuous"
        elif segment.duration_minutes is not None:
            hours, minutes = divmod(segment.duration_minutes, 60)
            duration = f"{hours}h{minutes:02d}m"
        else:
            duration = f"raw={segment.duration_raw.hex()}"
        segments.append(f"{index}:{mode}/{duration}")
    return (
        f"marker=0x{schedule.marker:02x} "
        + ", ".join(segments)
        + " (marker meaning unknown)"
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except BleakError as error:
        print(f"BLE operation failed: {error}")
