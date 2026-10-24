"""Connect to an H5179 and display its current BLE readings.

Run from the repository root with
``PYTHONPATH=src python examples/watch_h5179_ble.py``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING

from bleak.exc import BleakError

from govee_ble_client import DeviceFilter, GoveeScanner, H5179Notification, H5179Profile

if TYPE_CHECKING:
    from govee_ble_client.device import GattConnection

PROFILE = H5179Profile()
MEASUREMENT_INTERVAL = 5.0
BATTERY_INTERVAL = 60.0
RESPONSE_TIMEOUT = 1.0


async def main() -> None:
    print("Scanning for nearby H5179 advertisements...")
    devices = await GoveeScanner().scan(
        timeout=10, device_filter=DeviceFilter(name_contains="H5179")
    )
    if not devices:
        print("No H5179 advertisements found. Move closer and try again.")
        return

    for index, device in enumerate(devices):
        print(f"[{index}] {device.name!r} RSSI={device.rssi}")
    selected = input("Index of the H5179 to monitor: ").strip()
    try:
        device = devices[int(selected)]
    except (ValueError, IndexError):
        print("Invalid device index.")
        return
    if not PROFILE.matches(device):
        print("The selected advertisement name does not match H5179.")
        return

    print(
        f"Connecting to {device.name!r}. Disconnect Govee Home first if the "
        "device rejects this BLE connection. Readings are displayed in Celsius "
        "and Fahrenheit."
    )
    readings: dict[str, float | int | None] = {
        "temperature_c": None,
        "humidity_percent": None,
        "battery_percent": None,
    }
    response_event = asyncio.Event()
    unparsed: asyncio.Queue[bytes] = asyncio.Queue()
    last_measurement_received: datetime | None = None

    def on_notification(_sender: int, data: bytearray) -> None:
        nonlocal last_measurement_received
        raw = bytes(data)
        try:
            notification = PROFILE.parse_notification(raw)
        except ValueError:
            unparsed.put_nowait(raw)
            response_event.set()
            return
        _update_readings(notification, readings)
        if notification.temperature_c is not None:
            last_measurement_received = datetime.now().astimezone()
        if any(
            value is not None
            for value in (
                notification.temperature_c,
                notification.humidity_percent,
                notification.battery_percent,
            )
        ):
            response_event.set()
        else:
            unparsed.put_nowait(raw)

    async with device.connect(timeout=15) as connection:
        await connection.start_notify(PROFILE.io_uuid, on_notification)
        try:
            await _request_and_wait(
                connection, PROFILE.request_measurement, response_event
            )
            await _request_and_wait(connection, PROFILE.request_battery, response_event)
            next_measurement = time.monotonic() + MEASUREMENT_INTERVAL
            next_battery = time.monotonic() + BATTERY_INTERVAL
            print("Monitoring once per second. Press Ctrl+C to stop.")

            while True:
                now = time.monotonic()
                if now >= next_measurement:
                    await _request_and_wait(
                        connection, PROFILE.request_measurement, response_event
                    )
                    next_measurement = now + MEASUREMENT_INTERVAL
                if now >= next_battery:
                    await _request_and_wait(
                        connection, PROFILE.request_battery, response_event
                    )
                    next_battery = now + BATTERY_INTERVAL

                while not unparsed.empty():
                    print(f"Unparsed notification: {unparsed.get_nowait().hex()}")
                _print_readings(readings, last_measurement_received)
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            print("Stopping monitor.")
        except BleakError as error:
            print(f"BLE operation failed: {error}")
        finally:
            await connection.stop_notify(PROFILE.io_uuid)


async def _request_and_wait(
    connection: GattConnection,
    request: Callable[[GattConnection], Awaitable[None]],
    response_event: asyncio.Event,
) -> None:
    response_event.clear()
    await request(connection)
    try:
        await asyncio.wait_for(response_event.wait(), timeout=RESPONSE_TIMEOUT)
    except TimeoutError:
        print("No H5179 notification received before timeout.", flush=True)


def _update_readings(
    notification: H5179Notification, readings: dict[str, float | int | None]
) -> None:
    if notification.temperature_c is not None:
        readings["temperature_c"] = notification.temperature_c
    if notification.humidity_percent is not None:
        readings["humidity_percent"] = notification.humidity_percent
    if notification.battery_percent is not None:
        readings["battery_percent"] = notification.battery_percent


def _print_readings(
    readings: dict[str, float | int | None],
    last_measurement_received: datetime | None,
) -> None:
    celsius = readings["temperature_c"]
    humidity = readings["humidity_percent"]
    battery = readings["battery_percent"]
    fahrenheit = celsius * 9 / 5 + 32 if isinstance(celsius, (float, int)) else None
    print(
        "temperature: "
        + (
            f"{celsius:.2f} °C / {fahrenheit:.2f} °F"
            if fahrenheit is not None
            else "unknown"
        )
        + " | humidity: "
        + (f"{humidity:.2f}%" if isinstance(humidity, (float, int)) else "unknown")
        + " | battery: "
        + (f"{battery}%" if isinstance(battery, int) else "unknown"),
        flush=True,
    )
    updated = (
        last_measurement_received.isoformat(timespec="seconds")
        if last_measurement_received is not None
        else "unknown"
    )
    print(f"last BLE measurement received: {updated}", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except BleakError as error:
        print(f"BLE operation failed: {error}")
