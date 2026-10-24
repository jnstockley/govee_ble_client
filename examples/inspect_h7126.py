"""Scan and inspect a nearby H7126 with explicit opt-in probes.

Run from the repository root with ``PYTHONPATH=src python examples/inspect_h7126.py``.
This script reads characteristics that advertise the ``read`` property and
subscribes to notifications. It can optionally query status and separately
asks before attempting a power-on command.
"""

from __future__ import annotations

import asyncio

from bleak.exc import BleakError

from govee_ble import GoveeScanner, H7126Profile

PROFILE = H7126Profile()
NOTIFY_UUID = PROFILE.notify_uuid
WRITE_UUID = PROFILE.write_uuid


async def main() -> None:
    devices = await GoveeScanner().scan(timeout=12)
    if not devices:
        print("No BLE advertisements found.")
        return

    print("Nearby BLE advertisements:")
    for index, device in enumerate(devices):
        print(
            f"[{index}] name={device.name!r} rssi={device.rssi} "
            f"manufacturer_data={_hex_map(device.manufacturer_data)} "
            f"service_data={_hex_map(device.service_data)} "
            f"service_uuids={list(device.service_uuids)}"
        )

    selection = input("Index of the H7126 (or blank to stop): ").strip()
    if not selection:
        return
    try:
        device = devices[int(selection)]
    except (ValueError, IndexError):
        print("Invalid device index.")
        return
    if not PROFILE.matches(device):
        print("Selected advertisement name does not identify an H7126.")
        return

    print(f"Connecting to {device.name!r} to inspect its GATT table...")
    print("If the Govee app is connected, disconnect it before continuing.")
    async with device.connect(timeout=15) as connection:
        notifications: dict[str, list[bytes]] = {}
        notifiable: list[str] = []
        for service in connection.services:
            print(f"Service {service.uuid} ({service.description})")
            for characteristic in service.characteristics:
                print(
                    f"  Characteristic {characteristic.uuid} "
                    f"({characteristic.description}) "
                    f"properties={','.join(characteristic.properties)}"
                )
                if "notify" in characteristic.properties:
                    notifications[characteristic.uuid] = []
                    notifiable.append(characteristic.uuid)

        for characteristic_uuid in notifiable:
            await connection.start_notify(
                characteristic_uuid,
                lambda _sender, data, uuid=characteristic_uuid: notifications[
                    uuid
                ].append(bytes(data)),
            )

        for service in connection.services:
            for characteristic in service.characteristics:
                if "read" not in characteristic.properties:
                    continue
                try:
                    value = await connection.read(characteristic)
                except BleakError as error:
                    print(f"Read {characteristic.uuid} failed: {error}")
                else:
                    print(f"Read {characteristic.uuid}: {value.hex()}")

        known_characteristics = {
            characteristic.uuid
            for service in connection.services
            for characteristic in service.characteristics
        }
        if WRITE_UUID in known_characteristics and NOTIFY_UUID in notifiable:
            answer = (
                input(
                    "Send common-format 0xAA/0x01 power-status query? "
                    "It reported off on this H7126 when off; other-state "
                    "behavior is unverified [y/N]: "
                )
                .strip()
                .casefold()
            )
            if answer == "y":
                try:
                    await PROFILE.request_power_state(connection)
                except BleakError as error:
                    print(f"Status query write failed: {error}")
                else:
                    await asyncio.sleep(2)

        if (
            "H7126" in (device.name or "").upper()
            and WRITE_UUID in known_characteristics
            and NOTIFY_UUID in notifiable
            and _latest_power_state(notifications[NOTIFY_UUID]) is False
        ):
            answer = (
                input(
                    "Attempt common-format power ON (0x33/0x01/0x01)? "
                    "This may start the purifier; H7126 command support is "
                    "unverified [y/N]: "
                )
                .strip()
                .casefold()
            )
            if answer == "y":
                try:
                    await PROFILE.turn_on(connection)
                except BleakError as error:
                    print(f"Power-on write failed: {error}")
                else:
                    await asyncio.sleep(0.5)
                    try:
                        await PROFILE.request_power_state(connection)
                    except BleakError as error:
                        print(f"Power-state verification query failed: {error}")
                    else:
                        await asyncio.sleep(2)

        for characteristic_uuid in notifiable:
            await connection.stop_notify(characteristic_uuid)
            packets = notifications[characteristic_uuid]
            print(
                f"Notifications {characteristic_uuid}: "
                f"{[packet.hex() for packet in packets]}"
            )
            for packet in packets:
                try:
                    notification = PROFILE.parse_notification(packet)
                except ValueError as error:
                    print(f"  Unparsed notification: {error}")
                    continue
                if notification.power_state is not None:
                    state = "on" if notification.power_state else "off"
                    print(
                        f"  Common-format power state: {state}; "
                        f"remaining payload={notification.payload[1:].hex()}"
                    )


def _hex_map(values: dict[object, bytes]) -> dict[str, str]:
    return {str(key): value.hex() for key, value in values.items()}


def _latest_power_state(packets: list[bytes]) -> bool | None:
    for packet in reversed(packets):
        try:
            notification = PROFILE.parse_notification(packet)
        except ValueError:
            continue
        if notification.power_state is not None:
            return notification.power_state
    return None


if __name__ == "__main__":
    asyncio.run(main())
