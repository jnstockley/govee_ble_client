"""Compare H7102 query replies with physical oscillation off/on/off/on.

Run with PYTHONPATH=src python examples/investigate_h7102_oscillation.py.
Only the encryption handshake and AA query frames are sent. Experimental
queries do not have confirmed H7102 semantics. No fan-control frame is sent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bleak.exc import BleakError

from govee_ble import DeviceFilter, GoveeScanner, H7102Profile, build_packet
from govee_ble.encryption import GoveeAesRc4V1
from govee_ble.exceptions import DeviceNotConnectedError, GoveeBLEError

if TYPE_CHECKING:
    from govee_ble.device import GattConnection

PROFILE = H7102Profile()
APP_PREFIXES = (0xAA, 0xEE, 0x33, 0x3A)


@dataclass(frozen=True, slots=True)
class Query:
    name: str
    command: int
    payload: bytes = b""

    @property
    def frame(self) -> bytes:
        return build_packet(0xAA, self.command, self.payload)


QUERIES = (
    Query("power/status", 0x01),
    Query("work mode", 0x05, b"\x00"),
    Query("fan speed", 0x05, b"\x01"),
    Query("1F selector 0 (experimental)", 0x1F),
    Query("1F selector 1 (captured)", 0x1F, b"\x01"),
    Query("1F selector 2 (captured)", 0x1F, b"\x02"),
    Query("1D (related fan model; experimental)", 0x1D),
)
ROUND_STATES = ("off", "on", "off", "on")


class ProbeSession:
    """Keep the encrypted session current and retry an interrupted query once."""

    def __init__(self, connection: GattConnection) -> None:
        self.connection = connection
        self.encrypted = self._new_transport()
        self.reconnects = 0

    def _new_transport(self) -> GoveeAesRc4V1:
        return GoveeAesRc4V1(
            self.connection,
            write_uuid=PROFILE.write_uuid,
            notify_uuid=PROFILE.notify_uuid,
        )

    async def start(self) -> None:
        PROFILE.validate_connection(self.connection)
        await self.encrypted.connect(timeout=8)

    async def reconnect(self) -> None:
        print("Reconnecting GATT and encryption before resuming queries.")
        try:
            await self.encrypted.close()
        except (BleakError, GoveeBLEError):
            # The link may drop between the connection check and stop_notify.
            # Replacing the GATT client below discards that old subscription.
            pass
        await self.connection.reconnect()
        self.encrypted = self._new_transport()
        await self.start()
        self.reconnects += 1

    async def capture(self, query: Query, *, timeout: float) -> dict[str, Any]:
        if not self.connection.is_connected:
            await self.reconnect()
        try:
            entry = await _capture_query(self.encrypted, query, timeout=timeout)
        except (DeviceNotConnectedError, BleakError):
            if self.connection.is_connected:
                raise
            # Queries are safe to resend if the link disappears after checking
            # is_connected. Do not retry arbitrary state-changing commands.
            await self.reconnect()
            entry = await _capture_query(self.encrypted, query, timeout=timeout)
        entry["session"] = self.reconnects
        return entry

    async def close(self) -> None:
        await self.encrypted.close()


async def _ainput(prompt: str) -> str:
    return await asyncio.to_thread(input, prompt)


async def _capture_query(
    encrypted: GoveeAesRc4V1, query: Query, *, timeout: float
) -> dict[str, Any]:
    # Consume existing notifications before sending this query. Their arrival
    # alone does not establish which request or physical setting caused them.
    pending_frames: list[str] = []
    deadline = asyncio.get_running_loop().time() + 0.5
    while asyncio.get_running_loop().time() < deadline:
        try:
            pending = await encrypted.receive(timeout=0.05)
        except TimeoutError:
            break
        except ValueError as error:
            print(f"  Pending notification rejected: {error}")
        else:
            if pending[0] in APP_PREFIXES:
                pending_frames.append(pending.hex())
                print(f"  Pending RX (before this query): {pending.hex()}")

    await encrypted.write(query.frame)
    print(f"  TX {query.name}: {query.frame.hex()}")
    received: list[str] = []
    deadline = asyncio.get_running_loop().time() + timeout
    while (remaining := deadline - asyncio.get_running_loop().time()) > 0:
        try:
            raw = await encrypted.receive(timeout=remaining)
            PROFILE.parse_notification(raw)
        except TimeoutError:
            break
        except ValueError as error:
            print(f"  Notification rejected: {error}")
            continue
        if raw[0] not in APP_PREFIXES:
            continue
        received.append(raw.hex())
        echo = " (identical to query; ambiguous)" if raw == query.frame else ""
        print(f"  RX {raw.hex()}{echo}")
    if not received:
        print("  RX: none")
    return {
        "query": query.name,
        "tx": query.frame.hex(),
        "pending_rx": pending_frames,
        "rx": received,
    }


def find_candidates(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Find stable payload differences, requiring all four completed rounds.

    This reports correlations, not a proven oscillation decoder. Responses
    are grouped by query window and command; the protocol has no transaction
    identifiers to prove that a notification belongs to a given query.
    """
    rounds = report["rounds"]
    if tuple(item["state"] for item in rounds) != ROUND_STATES:
        return []
    candidates: list[dict[str, Any]] = []
    for query in QUERIES:
        grouped: dict[str, list[bytes]] = {"off": [], "on": []}
        complete = True
        for stage in rounds:
            samples = stage["samples"]
            if len(samples) != report["samples_per_round"]:
                complete = False
                break
            for sample in samples:
                entries = [item for item in sample if item["query"] == query.name]
                matches: list[bytes] = []
                for entry in entries:
                    matches.extend(
                        raw
                        for encoded in entry["rx"]
                        if len(raw := bytes.fromhex(encoded)) == 20
                        and raw[:2] == bytes((0xAA, query.command))
                    )
                if not matches:
                    complete = False
                    break
                grouped[stage["state"]].extend(matches)
            if not complete:
                break
        if not complete:
            continue
        # Exclude prefix, command, and XOR checksum. The checksum necessarily
        # changes with any payload change and is not an independent state field.
        for offset in range(2, 19):
            off = {frame[offset] for frame in grouped["off"]}
            on = {frame[offset] for frame in grouped["on"]}
            if len(off) == len(on) == 1 and off != on:
                off_value, on_value = next(iter(off)), next(iter(on))
                candidates.append(
                    {
                        "query": query.name,
                        "byte_offset_zero_based": offset,
                        "off": f"{off_value:02x}",
                        "on": f"{on_value:02x}",
                        "changed_bits": f"{off_value ^ on_value:02x}",
                        "off_all_query_identical": all(
                            frame == query.frame for frame in grouped["off"]
                        ),
                        "on_all_query_identical": all(
                            frame == query.frame for frame in grouped["on"]
                        ),
                    }
                )
    return candidates


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--samples", type=int, default=3, help="passes per state (default 3)"
    )
    parser.add_argument(
        "--timeout", type=float, default=0.75, help="reply window in seconds"
    )
    parser.add_argument(
        "--output", type=Path, help="save a new JSON report without device IDs"
    )
    args = parser.parse_args()
    if args.samples < 2 or not 0 < args.timeout <= 10:
        parser.error(
            "use at least two samples and a timeout greater than 0 and at most 10"
        )
    if args.output is not None and args.output.exists():
        parser.error("output already exists; choose a new report path")

    print("Keep the fan powered on in Normal mode at a fixed speed.")
    print("Disconnect Govee Home. Change only oscillation using the physical button.")
    print("The tool sends these queries after establishing the encrypted session:")
    for query in QUERIES:
        print(f"  {query.name}: {query.frame.hex()}")
    if (await _ainput("Continue with these queries? [y/N] ")).strip().casefold() != "y":
        return

    devices = await GoveeScanner().scan(
        timeout=10, device_filter=DeviceFilter(name_contains="H7102")
    )
    if not devices:
        print("No H7102 advertisements found.")
        return
    for index, device in enumerate(devices):
        print(f"[{index}] {device.name!r} RSSI={device.rssi}")
    try:
        selected = int(await _ainput("Index of the H7102: "))
        if not 0 <= selected < len(devices):
            raise ValueError
    except ValueError:
        print("Invalid device index.")
        return
    device = devices[selected]
    report: dict[str, Any] = {
        "model": "H7102",
        "samples_per_round": args.samples,
        "reply_window_seconds": args.timeout,
        "rounds": [],
    }
    async with device.connect(timeout=15) as connection:
        session = ProbeSession(connection)
        try:
            await session.start()
            for number, state in enumerate(ROUND_STATES, start=1):
                print(f"\n=== Round {number}: oscillation {state.upper()} ===")
                answer = await _ainput(
                    f"Set oscillation {state} physically, confirm the fan's motion, "
                    "then press Enter (q to finish): "
                )
                if answer.strip().casefold() == "q":
                    break
                await asyncio.sleep(2)
                stage: dict[str, Any] = {"state": state, "samples": []}
                report["rounds"].append(stage)
                for sample_number in range(1, args.samples + 1):
                    print(f"Sample {sample_number}/{args.samples}")
                    sample = []
                    stage["samples"].append(sample)
                    for query in QUERIES:
                        sample.append(
                            await session.capture(query, timeout=args.timeout)
                        )
        except (BleakError, GoveeBLEError, TimeoutError, ValueError) as error:
            report["error"] = type(error).__name__
            print(f"Capture stopped: {error}")
        finally:
            report["reconnects"] = session.reconnects
            try:
                await session.close()
            except (BleakError, GoveeBLEError) as error:
                report["cleanup_error"] = type(error).__name__
                print(f"Session cleanup failed: {error}")

    report["candidates"] = find_candidates(report)
    print("\n=== Stable off/on differences (provisional) ===")
    if not report["candidates"]:
        print("No stable candidate found, or a round lacked usable replies.")
    for candidate in report["candidates"]:
        print(
            f"{candidate['query']}: byte {candidate['byte_offset_zero_based']} "
            f"off={candidate['off']} on={candidate['on']} "
            f"changed bits={candidate['changed_bits']}"
        )
        if candidate["off_all_query_identical"] or candidate["on_all_query_identical"]:
            print("  One state is query-identical; it needs an independent check.")
    print("A correlation still needs independent confirmation before API decoding.")
    if args.output is not None:
        with args.output.open("x", encoding="utf-8") as target:
            json.dump(report, target, indent=2)
            target.write("\n")
        print(f"Saved {args.output}; scan metadata and handshake frames are omitted.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (BleakError, GoveeBLEError, TimeoutError, ValueError, OSError) as error:
        print(f"Investigation failed: {error}")
