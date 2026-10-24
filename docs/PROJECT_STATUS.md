# Project Status and Device Development

This library provides reusable asynchronous BLE discovery and GATT transport, shared frame helpers, and model-specific Govee profiles. It is not a universal Govee command implementation: each profile is limited to behavior observed for that model. The package does not yet include a Home Assistant or HACS integration.

## Device Status

“Manually verified” means a device or app comparison was reported during development. It does not imply support across all firmware revisions or hardware variants. There is currently no automated test suite in the repository.

### H7126 Air Purifier

- **Implemented and manually verified:** Power read/write; Sleep, Low, and High modes; Custom mode and its three-segment schedule; panel display read/write; filter-life query and reset. The interactive verifier reported matching BLE readbacks and Govee Home/cloud state for its checks.
- **Evidence:** App PacketLogger captures plus direct BLE reads and writes. Schedule modes and durations were checked, including finite minutes and the Continuous sentinel.
- **Remaining limits:** Validation covers the supplied purifier, not every H7126 firmware. The filter percentage comes from an observed response offset. The display query was verified on this device; other variants may differ.
- **Tools:** `examples/inspect_h7126.py`, `examples/watch_h7126_ble.py`, `examples/watch_h7126_trace.py`, and `examples/verify_h7126_ble.py`.

### H5179 Thermo-Hygrometer

- **Implemented:** Temperature and humidity query (`AA 0A`), battery query (`AA 08`), Celsius/Fahrenheit conversion, and a host-side “last BLE measurement received” timestamp.
- **Evidence:** Command and response layouts come from the supplied capture; the user reviewed the resulting readings and timestamp behavior.
- **Missing or unverified:** The current measurement response has no device timestamp. Historical records and their relative-time fields were not present in the H5179 capture. No write operations are implemented; temperature-unit control is out of scope.
- **Tool:** `examples/watch_h5179_ble.py`.

### H7102 Tower Fan

- **Implemented and manually exercised:** AES/RC4 V1 session setup; power; Normal, Sleep, Nature, and Custom mode reads/writes; fan speed reads and writes; panel display on/off; global oscillation command; Custom schedule query and writer. The verifier supports reconnecting after an interactive pause.
- **Observed:** App mode codes are Normal `1`, Sleep `5`, Nature `6`, Custom `2`. Fan speeds 5–8 matched app readback. Custom selector `AA 05 02` returns three speed/duration entries. Durations use duplicated big-endian minute words and `FF FF FF FF` for Continuous. The speed byte's `0x10` bit indicates per-segment oscillation: speed 5 was observed as `0x15` with oscillation on and `0x05` with it off.
- **Remaining limits:** Confirm a write/readback round trip for a changed Custom schedule, including per-segment oscillation. Speeds 1–4 and global oscillation state readback are unverified. Selector 3 data and the schedule marker remain uninterpreted. AES/RC4 V1 is legacy compatibility transport, not modern authenticated encryption.
- **Tools:** `examples/watch_h7102_raw_ble.py` and `examples/verify_h7102_ble.py`.

## Adding and Verifying a Device

1. **Establish identity and GATT layout.** Record the advertised name as a discovery hint, then inspect the connected service and characteristic UUIDs and properties. A name or shared Govee UUID does not prove a model or protocol.
2. **Capture controlled behavior.** Record a baseline, then change one app setting at a time. Save the action, direction, state before and after, raw bytes, and firmware details when available. For encrypted devices, document the handshake separately from application frames. Redact device identifiers and unrelated traffic before sharing captures.
3. **Implement conservatively.** Add a model profile under `src/govee_ble/profiles/`. Validate frame lengths, checksums, fields, and ranges; preserve unknown payload bytes. Add queries only after observing replies. Add writes only after capturing the app command, and label remaining uncertainty in docstrings and this status file.
4. **Expose and document the API.** Export public types from both `profiles/__init__.py` and `govee_ble/__init__.py`. Add a read-only watcher and, when state-changing controls exist, a confirmation-based verifier. Keep protocol logic independent of Home Assistant.
5. **Verify in two layers.** Add offline pytest cases for framing, parsing, invalid input, and state updates using constructed or redacted frames. Then run the device verifier and compare BLE readback with Govee Home. A sent write alone is not proof the device applied it.

## Development Commands and Test Coverage

Run `uv sync --all-groups` to install the locked environment, `uv build` to build distributions, and `uv run pytest` to run automated tests. The project currently has no `tests/` directory, so manual device verification is the existing validation method. Run examples from the repository root with `PYTHONPATH=src`; for example, `PYTHONPATH=src python examples/verify_h7102_ble.py`. Verifiers can change physical device state and ask before each action.
