# Project Status and Device Development

This library provides reusable asynchronous BLE discovery and GATT transport, shared frame helpers, and model-specific Govee profiles. It is not a universal Govee command implementation: each profile is limited to behavior observed for that model. The package supplies Home Assistant-friendly connection hooks but does not include an installable Home Assistant/HACS integration. Start with the [application guide](APP_INTEGRATION.md) and [API reference](API.md).

## Device Status

“Manually verified” means a device or app comparison was reported during development. It does not imply support across all firmware revisions or hardware variants. Offline pytest tests cover every library module and all three profiles, including their queries, controls, parsing, validation, and state handling. Hardware verification remains a separate source of protocol evidence.

### H7126 Air Purifier

- **Implemented and manually verified:** Power read/write; Sleep, Low, and High modes; Custom mode and its three-segment schedule; panel display read/write; filter-life query and reset. The interactive verifier reported matching BLE readbacks and Govee Home/cloud state for its checks.
- **Evidence:** App PacketLogger captures plus direct BLE query/reply exchanges and control writes. Schedule modes and durations were checked, including finite minutes and the Continuous sentinel.
- **Remaining limits:** Validation covers the supplied purifier, not every H7126 firmware. The filter percentage comes from an observed response offset. The display query was verified on this device; other variants may differ.
- **Tools:** `examples/inspect_h7126.py`, `examples/watch_h7126_ble.py`, `examples/watch_h7126_trace.py`, and `examples/verify_h7126_ble.py`.

### H5179 Thermo-Hygrometer

- **Implemented:** Temperature and humidity query (`AA 0A`), battery query (`AA 08`), Celsius/Fahrenheit conversion, and a watcher-only host-side “last BLE measurement received” timestamp. The library notification type itself has no timestamp field.
- **Evidence:** Command and response layouts come from the supplied capture; the user reviewed the resulting readings and timestamp behavior.
- **Missing or unverified:** The current measurement response has no device timestamp. Historical records and their relative-time fields were not present in the H5179 capture. No state-changing controls are implemented; measurement and battery queries are GATT writes. Temperature-unit control is out of scope.
- **Tool:** `examples/watch_h5179_ble.py`.

### H7102 Tower Fan

- **Implemented and manually exercised:** AES/RC4 V1 session setup; power; Normal, Sleep, Nature, and Custom mode reads/writes; fan speed reads and writes; panel display on/off; global oscillation command; Custom schedule query and writer. The verifier supports reconnecting after an interactive pause.
- **Observed:** App mode codes are Normal `1`, Sleep `5`, Nature `6`, Custom `2`. Fan speeds 5–8 matched app readback. Custom selector `AA 05 02` returns three speed/duration entries. Durations use duplicated big-endian minute words and `FF FF FF FF` for Continuous. The speed byte's `0x10` bit indicates per-segment oscillation: speed 5 was observed as `0x15` with oscillation on and `0x05` with it off. Global `AA 1F 01` replies returned `00` in all six physically labeled off samples and `01` in all six on samples across two complete cycles, in powered-on Normal mode at speed 5, without disconnects. Global oscillation reading is implemented in the API and watcher, and the verifier checks writes against it.
- **Remaining limits:** Confirm a write/readback round trip for a changed Custom schedule, including per-segment oscillation. Check global oscillation at other speeds/modes and while powered off. Speeds 1–4 remain unverified. Selector 3 data and the schedule marker remain uninterpreted. AES/RC4 V1 is legacy compatibility transport, not modern authenticated encryption.
- **Tools:** `examples/watch_h7102_raw_ble.py` and `examples/verify_h7102_ble.py`.

Global oscillation research and the controlled off/on query probe are documented
in [H7102 oscillation investigation](H7102_OSCILLATION.md).

## Transport and Application Integration

Connection handles coordinate individual operations per peripheral and allow
independent devices to progress concurrently. New sessions create fresh clients;
optional native-device resolvers and connection strategies support host-managed
adapters. Apps own polling, reply matching, recovery/backoff, and availability.
H5179/H7126 are stateless; H7102 state updates only through its single receiver.
See the [application guide](APP_INTEGRATION.md) for read-only recipes and shutdown.

The implementation targets x86 and ARM64 without architecture-specific library
code. Offline tests cover concurrency and lifecycle behavior, not radio latency,
proxy capacity, firmware compatibility, or a running Home Assistant integration.
See [protocol evidence](PROTOCOL_EVIDENCE.md) for captures and manual tools.

## Adding and Verifying a Device

1. **Establish identity and GATT layout.** Record the advertised name as a discovery hint, then inspect the connected service and characteristic UUIDs and properties. A name or shared Govee UUID does not prove a model or protocol.
2. **Capture controlled behavior.** Record a baseline, then change one app setting at a time. Save the action, direction, state before and after, raw bytes, and firmware details when available. For encrypted devices, document the handshake separately from application frames. Redact device identifiers and unrelated traffic before sharing captures.
3. **Implement conservatively.** Add a model profile under `src/govee_ble_client/profiles/`. Validate frame lengths, checksums, fields, and ranges; preserve unknown payload bytes. Add queries only after observing replies. Add writes only after capturing the app command, and label remaining uncertainty in docstrings and this status file.
4. **Expose and document the API.** Export public types from both `profiles/__init__.py` and `govee_ble_client/__init__.py`. Add a read-only watcher and, when state-changing controls exist, a confirmation-based verifier. Keep protocol logic independent of Home Assistant.
5. **Verify in two layers.** Add offline pytest cases for framing, parsing, invalid input, and state updates using constructed or redacted frames. Then run the device verifier and compare BLE readback with Govee Home. A sent write alone is not proof the device applied it.

## Development Commands and Test Coverage

Run `uv sync --locked --all-groups` to install the environment, `uv build` to build distributions, and `uv run pytest` to run `tests/`. The suite blocks real BLE access, uses synthetic peripherals and packet fixtures, and tests all public profile commands without transmitting them. See [Offline testing](TESTING.md) for the coverage map and CI command. These checks validate code behavior; hardware captures and manual checks establish support for a particular device and firmware. Run hardware examples separately from the repository root with `PYTHONPATH=src`. Verifiers can change physical device state and ask before each action.
