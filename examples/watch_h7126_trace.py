"""Print the latest H7126 state observed in a PacketLogger ``.pklg`` trace.

This watches the iPhone's PacketLogger file so Govee Home can remain connected.
It reports mode/display commands observed in the trace, not device readback.
The H7126 trace currently exposes only power state through a status reply; BLE
filter-life readback has not been observed.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

ATT_SEND = bytes.fromhex("02 05")
ATT_RECEIVE = bytes.fromhex("03 05")
ATT_WRITE_COMMAND = 0x52
ATT_HANDLE_NOTIFICATION = 0x1B
TARGET_WRITE_HANDLE = 0x0011


@dataclass(slots=True)
class ObservedState:
    power: str = "unknown"
    mode: str = "not observed"
    display: str = "not observed"
    filter_life: str = "not observed"
    last_event: str = "waiting for H7126 traffic"


def iter_pklg_records(data: bytes) -> Iterator[tuple[bytes, bytes]]:
    """Yield complete records from the PacketLogger record stream."""
    offset = 0
    while offset + 4 <= len(data):
        body_length = int.from_bytes(data[offset : offset + 4], "little")
        record_length = 4 + body_length
        if body_length < 10 or offset + record_length > len(data):
            return
        record = data[offset : offset + record_length]
        yield record[12:14], record[14:]
        offset += record_length


def update_state(data: bytes, state: ObservedState) -> None:
    """Update the latest observed values from complete ATT records."""
    for record_type, packet in iter_pklg_records(data):
        if len(packet) < 10:
            continue

        att_opcode = packet[7]
        handle = int.from_bytes(packet[8:10], "little")
        value = packet[10:]

        if record_type == ATT_SEND and att_opcode == ATT_WRITE_COMMAND:
            if handle != TARGET_WRITE_HANDLE or len(value) != 20:
                continue
            _update_from_command(value, state)
        elif record_type == ATT_RECEIVE and att_opcode == ATT_HANDLE_NOTIFICATION:
            _update_from_notification(value, state)


def _update_from_command(value: bytes, state: ObservedState) -> None:
    if value[:2] == b"\x3a\x05":
        if value[2:4] == b"\x01\x01":
            state.mode = "Sleep (last app command)"
            state.last_event = "Sleep mode command observed"
        elif value[2:4] == b"\x01\x02":
            state.mode = "Low (last app command)"
            state.last_event = "Low mode command observed"
        elif value[2:4] == b"\x01\x03":
            state.mode = "High (last app command)"
            state.last_event = "High mode command observed"
        elif value[2:3] == b"\x02":
            state.mode = "Custom (captured settings; last app command)"
            state.last_event = "Custom mode command observed"
    elif value[:2] == b"\x33\x16" and value[2] in (0, 1):
        state.display = (
            "on (last app command)" if value[2] else "off (last app command)"
        )
        state.last_event = "Panel display command observed"


def _update_from_notification(value: bytes, state: ObservedState) -> None:
    if len(value) >= 3 and value[:2] == b"\xaa\x01" and value[2] in (0, 1):
        state.power = "on" if value[2] else "off"
        state.last_event = "power-state notification received"
    elif len(value) >= 7 and value[:2] == b"\xaa\x19" and value[6] <= 100:
        state.filter_life = f"{value[6]}%"
        state.last_event = "filter-life value received"
    elif len(value) >= 4 and value[:2] == b"\xaa\x05":
        mode = {b"\x00\x01": "High", b"\x00\x02": "Custom"}.get(value[2:4])
        state.mode = mode or f"unknown (AA 05 {value[2:4].hex()})"
        state.last_event = "mode query notification received"
    elif len(value) == 20 and value[:2] == b"\xee\x05":
        if value[2] == 0x02:
            mode = "Custom"
        elif value[2] == 0x01:
            mode = {1: "Sleep", 2: "Low", 3: "High"}.get(value[3])
        else:
            mode = None
        if mode is not None:
            state.mode = f"{mode} (BLE notification)"
            state.last_event = f"{mode} mode notification received"
    elif len(value) == 20 and value[:2] == b"\xee\x16" and value[2] in (0, 1):
        display = "on" if value[2] else "off"
        state.display = f"{display} (BLE notification)"
        state.last_event = f"Panel display {display} notification received"


def print_state(state: ObservedState) -> None:
    now = datetime.now().astimezone().strftime("%H:%M:%S")
    print(
        f"{now} | power: {state.power} | mode: {state.mode} | "
        f"panel display: {state.display} | filter life: {state.filter_life} "
        f"| latest: {state.last_event}",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Watch H7126 state traffic in a PacketLogger .pklg file."
    )
    parser.add_argument("trace", type=Path, help="PacketLogger .pklg file path")
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="seconds between status lines (default: 1)",
    )
    args = parser.parse_args()
    if args.interval <= 0:
        parser.error("--interval must be greater than zero")
    if not args.trace.is_file():
        parser.error(f"trace file does not exist: {args.trace}")

    print(
        "Watching PacketLogger data. Mode/display use notifications when "
        "present and otherwise show the last app command. Press Ctrl+C to stop."
    )
    state = ObservedState()
    try:
        while True:
            try:
                data = args.trace.read_bytes()
            except OSError as error:
                print(f"Could not read trace: {error}", flush=True)
            else:
                update_state(data, state)
                print_state(state)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
