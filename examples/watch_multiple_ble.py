"""Poll several supported devices concurrently using only verified state queries.

Run manually with PYTHONPATH=src python examples/watch_multiple_ble.py.
This opens physical BLE connections and performs the H7102 session handshake,
but sends no application control commands. Ctrl-C disconnects all workers.
Home Assistant integrations should use HA discovery instead of this scanner.
Workers stop independently on failure; no reconnect or availability manager is
provided. See docs/APP_INTEGRATION.md before adapting this diagnostic for an app.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from govee_ble_client import (
    DiscoveredDevice,
    GattConnection,
    GoveeScanner,
    H5179Profile,
    H7102Client,
    H7102Profile,
    H7126Profile,
)

PROFILES = (H5179Profile(), H7126Profile(), H7102Profile())


def _profile(
    device: DiscoveredDevice,
) -> H5179Profile | H7126Profile | H7102Profile | None:
    return next((profile for profile in PROFILES if profile.matches(device)), None)


async def _fan_receive(fan: H7102Client) -> None:
    while True:
        await fan.receive()
        print(f"H7102: {fan.state}", flush=True)


async def _fan_poll(fan: H7102Client, interval: float) -> None:
    while True:
        await fan.request_state()
        await asyncio.sleep(interval)


async def _poll_plain(
    connection: GattConnection,
    profile: H5179Profile | H7126Profile,
    interval: float,
) -> None:
    def notification(_sender: object, data: bytearray) -> None:
        try:
            parsed = profile.parse_notification(bytes(data))
        except ValueError:
            print(f"{profile.model}: invalid notification", flush=True)
            return
        if isinstance(profile, H5179Profile):
            print(
                f"H5179: temperature={parsed.temperature_c} C, "
                f"humidity={parsed.humidity_percent}%, battery={parsed.battery_percent}%",
                flush=True,
            )
        else:
            print(
                f"H7126: power={parsed.power_state}, mode={parsed.mode}, "
                f"display={parsed.display_state}, filter={parsed.filter_life_percent}%",
                flush=True,
            )

    notify = (
        profile.io_uuid if isinstance(profile, H5179Profile) else profile.notify_uuid
    )
    await connection.start_notify(notify, notification)
    if isinstance(profile, H5179Profile):
        queries = (profile.request_measurement, profile.request_battery)
    else:
        queries = (
            profile.request_power_state,
            profile.request_mode_hint,
            profile.request_gear_mode,
            profile.request_custom_mode_settings,
            profile.request_panel_display_state,
            profile.request_filter_life,
        )
    while True:
        for query in queries:
            await query(connection)
            await asyncio.sleep(0.1)
        await asyncio.sleep(interval)


async def monitor(device: DiscoveredDevice, *, interval: float = 5.0) -> None:
    """Own one persistent connection, including subscription cleanup on exit."""
    profile = _profile(device)
    if profile is None:
        return
    async with device.connect(services=(profile.service_uuid,)) as connection:
        if isinstance(profile, H7102Profile):
            async with H7102Client(connection) as fan, asyncio.TaskGroup() as tasks:
                tasks.create_task(_fan_receive(fan))
                tasks.create_task(_fan_poll(fan, interval))
        else:
            await _poll_plain(connection, profile, interval)


async def run_devices(
    devices: Sequence[DiscoveredDevice], *, interval: float = 5.0
) -> None:
    """Run independent workers; a failed device does not cancel its neighbors."""

    async def isolated(device: DiscoveredDevice) -> None:
        try:
            await monitor(device, interval=interval)
        except Exception as error:  # noqa: BLE001 - isolate each device worker
            # Avoid backend exception messages that may contain identifiers.
            print(f"Device worker stopped: {type(error).__name__}", flush=True)

    async with asyncio.TaskGroup() as tasks:
        for device in devices:
            tasks.create_task(isolated(device))


async def main() -> None:
    """Scan once and monitor up to five matching devices until interrupted."""
    devices = await GoveeScanner().scan(timeout=8)
    supported = [device for device in devices if _profile(device) is not None][:5]
    if not supported:
        print("No supported devices found.")
        return
    print(f"Monitoring {len(supported)} device(s); Ctrl-C to stop.", flush=True)
    await run_devices(supported)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
