"""Watch H7102 state using its captured encrypted BLE query protocol.

Run from the repository root with
``PYTHONPATH=src python examples/watch_h7102_raw_ble.py``.
"""

from __future__ import annotations

import asyncio
import time

from bleak.exc import BleakError

from govee_ble import (
    DeviceFilter,
    GoveeScanner,
    H7102CustomSegment,
    H7102Profile,
)
from govee_ble.encryption import GoveeAesRc4V1
from govee_ble.protocol import build_packet

PROFILE = H7102Profile()


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
    selected = input("Index of the H7102 to inspect: ").strip()
    try:
        device = devices[int(selected)]
    except (ValueError, IndexError):
        print("Invalid device index.")
        return
    if not PROFILE.matches(device):
        print("The selected advertisement name does not match H7102.")
        return

    print(
        f"Connecting to {device.name!r}. Disconnect Govee Home first. "
        "It performs the captured encryption handshake and sends only "
        "state queries, not fan-control commands."
    )

    async with device.connect(timeout=15) as connection:
        PROFILE.validate_connection(connection)
        encryption_info = None
        for service in connection.services:
            if service.uuid.casefold() != PROFILE.service_uuid:
                continue
            print(f"Service {service.uuid}")
            for characteristic in service.characteristics:
                print(
                    f"  Characteristic {characteristic.uuid} "
                    f"properties={','.join(characteristic.properties)}"
                )
                if characteristic.uuid.casefold() == PROFILE.encryption_info_uuid:
                    encryption_info = characteristic

        if encryption_info is None:
            print(
                "No 2b12 encryption-info characteristic was found. "
                "This does not prove that the device uses plaintext BLE."
            )
        elif "read" not in encryption_info.properties:
            print("The 2b12 encryption-info characteristic is not readable.")
        else:
            try:
                raw_info = await connection.read(encryption_info)
            except BleakError as error:
                print(f"Reading 2b12 encryption info failed: {error}")
            else:
                print(f"2b12 encryption-info value: {raw_info.hex()}")
                if len(raw_info) == 1 and raw_info[0] in (0, 1, 2):
                    version = {
                        0: "no app-layer encryption",
                        1: "AES/RC4 protocol (V1)",
                        2: "AES-GCM protocol (V2)",
                    }[raw_info[0]]
                    print(f"  Recognized single-byte version: {version}")
                else:
                    print(
                        "  Value shape is not a recognized single-byte version; "
                        "no field offset is assumed."
                    )

        encrypted = GoveeAesRc4V1(
            connection,
            write_uuid=PROFILE.write_uuid,
            notify_uuid=PROFILE.notify_uuid,
        )
        print(
            "Starting the captured AES/RC4 V1 handshake. "
            "No fan-control command will be sent."
        )
        try:
            await encrypted.connect(timeout=8)
            print("Encrypted session established.")
            query_interval = 5.0
            next_query = 0.0
            show_query_frames = True
            while True:
                if time.monotonic() >= next_query:
                    for name, command, payload in PROFILE.state_queries:
                        frame = build_packet(0xAA, command, payload)
                        await encrypted.write(frame)
                        if show_query_frames:
                            print(f"TX state query ({name}): {frame.hex()}")
                        await asyncio.sleep(0.08)
                    next_query = time.monotonic() + query_interval
                    if show_query_frames:
                        print(f"Polling captured state queries every {query_interval:g}s.")
                        show_query_frames = False

                try:
                    raw = await encrypted.receive(timeout=0.5)
                except TimeoutError:
                    continue
                notification = PROFILE.parse_notification(raw)
                if notification.power_state is not None:
                    state = "on" if notification.power_state else "off"
                    decoded = f"power={state}"
                elif notification.display_state is not None:
                    state = "on" if notification.display_state else "off"
                    decoded = f"display={state}"
                elif notification.oscillation_state is not None:
                    state = "on" if notification.oscillation_state else "off"
                    decoded = f"oscillation={state}"
                elif notification.mode_code is not None:
                    mode = notification.work_mode
                    decoded = (
                        f"work mode={mode.value} (code={notification.mode_code})"
                        if mode is not None
                        else f"work mode code={notification.mode_code} (unmapped)"
                    )
                elif notification.mode_selector_1_value is not None:
                    decoded = (
                        f"fan speed={notification.fan_speed}/8 "
                        "(app readback matched at speeds 5–8)"
                    )
                elif notification.mode_selector == 2:
                    schedule = notification.custom_schedule
                    if schedule is None:
                        decoded = (
                            "Custom settings (unrecognized layout): "
                            f"{(notification.mode_selector_data or b'').hex()}"
                        )
                    else:
                        segments = ", ".join(
                            _format_custom_segment(segment)
                            for segment in schedule.segments
                        )
                        decoded = (
                            f"Custom marker=0x{schedule.marker:02x}; {segments}"
                        )
                elif notification.mode_selector == 3:
                    decoded = (
                        f"mode selector {notification.mode_selector} data="
                        f"{notification.mode_selector_data.hex()}"
                    )
                else:
                    decoded = (
                        f"frame={notification.prefix:02x} "
                        f"command={notification.command:02x} "
                        f"payload={notification.payload.hex()}"
                    )
                print(f"RX {notification.raw.hex()} | {decoded}")
        except KeyboardInterrupt:
            print("Stopping monitor.")
        except BleakError as error:
            print(f"BLE operation failed: {error}")
        finally:
            await encrypted.close()


def _format_custom_segment(segment: H7102CustomSegment) -> str:
    """Format observed H7102 segment code and duration without guessing code meaning."""
    if segment.is_continuous:
        duration = "Continuous"
    elif segment.duration_minutes is not None:
        hours, minutes = divmod(segment.duration_minutes, 60)
        duration = f"{hours}h {minutes}m" if hours else f"{minutes}m"
    else:
        duration = f"raw duration {segment.duration_raw.hex()}"
    oscillation = "on" if segment.oscillate else "off"
    return f"speed {segment.speed}/8, oscillate {oscillation}: {duration}"


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (BleakError, TimeoutError, ValueError) as error:
        print(f"BLE/protocol operation failed: {error}")
