# govee-ble-client

An asynchronous Python library for discovering and communicating with Govee
devices over Bluetooth Low Energy (BLE). It is intended to be usable by regular
Python applications and integrations such as Home Assistant.

## Current scope

The package provides cross-platform BLE discovery, a protocol-neutral GATT
connection, Govee-style framing helpers, and device profiles for the H7126 air
purifier, H5179 thermo-hygrometer, and H7102 tower fan. Profiles expose
model-specific operations backed by supplied hardware traces and user device
checks.

See [Project status and device development](docs/PROJECT_STATUS.md) for the
current validation status, known gaps, and a workflow for adding profiles.

There is no single Govee BLE command protocol that can safely be assumed for
every product. A device may advertise no useful identity, use different GATT
characteristics, and require model-specific framing, handshakes, or pairing.
The library therefore keeps transport separate from protocol profiles. A
profile should be added only with captured or otherwise verified behavior for
the models and firmware it claims to support.

Source code and issue tracking: [jnstockley/govee_ble_client](https://github.com/jnstockley/govee_ble_client).

## Installation

```sh
pip install govee-ble-client
```

The PyPI distribution is `govee-ble-client`; the Python import is
`govee_ble_client`.

## Quick start

```python
import asyncio

from govee_ble_client import DeviceFilter, GoveeScanner


async def main() -> None:
    scanner = GoveeScanner()
    devices = await scanner.scan(
        timeout=8,
        device_filter=DeviceFilter(name_contains="Govee"),
    )
    for device in devices:
        print(device.name, device.identifier, device.rssi)

        # Inspect GATT after connecting. The characteristic UUID and bytes
        # must come from a verified protocol for the particular device.
        async with device.connect() as connection:
            for service in connection.services:
                print(service.uuid)


asyncio.run(main())
```

Without a filter, `scan()` returns every BLE advertisement discovered. The
name filter is a convenience only: names are optional and do not authenticate
or prove a device's manufacturer. Device identifiers can be randomized by the
platform and are not credentials.

## Design direction

- Keep BLE I/O asynchronous and delegate platform details to Bleak.
- Treat advertisements and GATT values as untrusted input; validate payloads
  and bounds in protocol parsers.
- Never infer a command from a device name or send undocumented writes by
  default.
- Add protocol profiles as independent implementations, keyed by verified
  advertisement/GATT evidence, and expose typed capabilities (power, color,
  brightness, sensors) only when a profile supports them.
- Avoid logging secrets, pairing material, or full raw traffic by default.

## H7126 investigation

The H7126 manual describes Bluetooth as part of Govee Home app pairing and
Wi-Fi setup. Although public integrations do not list it as a BLE control
device, direct hardware interaction and app traces have verified power,
Sleep/Low/High, Custom-mode configuration, and panel-display commands.

The first H7126 probe found the Govee custom service
`00010203-0405-0607-0809-0a0b0c0d1910`, with the `2b10` notify characteristic
and `2b11` write-without-response characteristic. Both characteristics
advertised `read`, but the purifier returned `Read Not Permitted`; subscribing
to `2b10` without sending a request produced no notifications. These UUIDs
also appear in other Govee products, but that alone does not establish the
H7126 command format or whether its operational controls are available over
BLE. Do not infer purifier fan or mode payloads from lighting controls.

The supplied PacketLogger trace contains writes to `2b11` and replies on the
connection, but no ATT read request for purifier state. Its repeated `AA 01`
queries receive power-on responses; the payload does not change across the
captured mode/display operations. Separate H7126 notifications supplied by the
user use prefix `ee 05 01` to report Sleep (`01`), Low (`02`), or High (`03`),
and `ee 05 02` to report Custom.
Display and filter-life readback were established in later H7126 captures.

A later app-startup trace shows an `AA 19` query and a response whose byte 6
is the filter-life percentage. The captured response had `00` at 0%; the
filter-reset trace had `64` (100%) at the same offset. The profile exposes
`request_filter_life()` and decodes that byte. The reset button sends
`33 17 01`, receives the observed `33 17 00` acknowledgement, and is followed
by an `AA 19` readback of 100%.

The initial mode query is two-stage. `AA 05 00` returns `AA 05 00 01` for the
gear-mode category or `AA 05 00 02` for Custom. For Sleep/Low/High, follow it
with `AA 05 01`; its final byte maps 1/2/3 to Sleep/Low/High. In Custom,
`AA 05 02` returns three mode/duration entries. The latest trace confirms the
selected schedule: Sleep for 10 minutes, Low for 1 hour 10 minutes, then High
with the app's “Continuous” duration. Finite durations encode total minutes as
two repeated big-endian 16-bit words: 10 minutes is `00 0A 00 0A`, and 70
minutes is `00 46 00 46`. `FF FF FF FF` encodes Continuous. The purifier echoes
`3A 05 02` schedule writes; the profile parses and can set all three entries.

After an opt-in `0xAA 0x01` query, one H7126 returned
`aa010000000000060000000000000000000000ad`. Its XOR checksum is valid. The
first response payload byte is `0x00`, which the common Govee response format
defines as power off; the remaining payload byte `0x06` is not decoded. This
matched the user's confirmation that the purifier was off. The common-format
`0x33 0x01 0x01` frame turned it on. A later capture verified `0x33 0x01 0x00`
(power off, checksum `32`); the purifier echoed the frame and the next `AA 01`
reply reported off.

The H7126 profile exposes `turn_on()`, `turn_off()`, `request_power_state()`,
`set_sleep_mode()`, `set_low_mode()`, `set_high_mode()`, `set_custom_mode()`,
`set_custom_schedule()`, and `set_panel_display()`. It also exposes
`request_filter_life()`, `reset_filter_life()`, `request_mode_hint()`, `request_gear_mode()`,
`request_custom_mode_settings()`, and `request_panel_display_state()`.

### Requested H7126 features

| Feature | Current evidence | BLE profile status |
| --- | --- | --- |
| Sleep, Low, High, Custom modes | `AA 05 00/01` reads the current mode; 1/2/3 map to Sleep/Low/High. `AA 05 02` reads three Custom mode/duration entries. The latest capture confirms Sleep/10 minutes, Low/70 minutes, and High/Continuous. `3A 05 02` updates are echoed by the device. | Mode and three-segment Custom schedule read/write implemented |
| Panel indicator LEDs on/off | Confirmed by the user: “Display” means the H7126 panel indicator LEDs. The trace shows `33 16 00 ff ff ff ff` followed by zero padding and checksum `25`, and `33 16 01 ff ff ff ff` followed by zero padding and checksum `24`. Notifications `ee 16 00` and `ee 16 01` report off and on; the startup `AA 16` query returned a recognizable state. | Commands, notifications, and experimental query implemented |
| Power | `AA 01` reports state; `33 01 01` turns on and `33 01 00` turns off. Both commands are observed, and status replies confirm the result. | Read/write implemented |
| Filter life percentage and reset | `AA 19` response byte 6 is 0 at 0% and `64` (100%) after reset. The reset-button capture shows write `33 17 01`, acknowledgement `33 17 00`, then 100% readback. | `request_filter_life()` and `reset_filter_life()` implemented |

References: [H7126 manual](https://govee-oss-public.oss-us-east-1.aliyuncs.com/prod/permanent-directory/skuDescriptionFile/H7126.pdf), [H7126 mode/API report](https://github.com/wez/govee2mqtt/issues/369), [purifier display support](https://github.com/homebridge-plugins/homebridge-govee/wiki/Supported-Devices), [similar Govee BLE lights query codes](https://github.com/Jaano/govee_lights/blob/master/GOVEE_BLE.md). The two-stage `AA 05` mode query is now observed in an H7126 PacketLogger trace.

### Capturing the missing BLE frames on iPhone

If you use an iPhone for Govee Home, Apple provides a Bluetooth logging profile
and PacketLogger. Install the Bluetooth logging profile on the iPhone, install
PacketLogger from Apple's Additional Tools for Xcode on the Mac, connect the
iPhone by USB, then choose **File → New iOS Trace** in PacketLogger. Capture
one normal app action at a time after the purifier is already configured:

1. Change mode through Sleep, Low, High, and Custom, recording the selected mode each time. The captured Low → High frame is `3a 05 01 03` with checksum `3d`.
2. Toggle Display. The captured `00` and `01` values are exposed as off and on.
3. Open the filter-life view or refresh it in the app.

In the trace, find ATT writes to characteristic `2b11` and notifications from
`2b10`. Share only those value bytes and which action produced each one. If
filter life changes only via app/cloud traffic and causes no BLE read or
notification, that is evidence it cannot be implemented through this BLE
profile. Do not share the full capture; it can contain unrelated device data.

The profile API is available directly to applications:

```python
from govee_ble_client import H7126Profile

profile = H7126Profile()
if profile.matches(discovered_device):
    async with discovered_device.connect() as connection:
        await connection.start_notify(profile.notify_uuid, handle_notification)
        await profile.request_power_state(connection)
        await profile.request_filter_life(connection)
        # Resets the maintenance counter; use after replacing the filter.
        await profile.reset_filter_life(connection)
        await profile.set_sleep_mode(connection)
        await profile.set_low_mode(connection)
        await profile.set_high_mode(connection)
        await profile.set_custom_mode(connection)
        await profile.set_panel_display(connection, enabled=False)
        # Call profile.turn_on(connection) only when you intend to start it.
```

`handle_notification` should parse bytes with `profile.parse_notification()` and
use `notification.power_state`, `notification.mode`, and
`notification.display_state` when they are not `None`.

## H5179 thermo-hygrometer

The H5179 profile uses the captured `INTELLI_ROCKS_HW` GATT service and its
`...2011` command/notification characteristic. It sends `AA 0A` for current
temperature and humidity and `AA 08` for battery percentage. The captured
measurement response stores temperature and humidity as little-endian
hundredths; temperature is represented in Celsius. `H5179Notification`
provides `temperature_c`, `temperature_f`, `humidity_percent`, and
`battery_percent`.

The profile uses the H5179-specific `AA 0A` and `AA 08` commands observed in
the supplied trace rather than substituting the `AA 01` combined-measurement
query documented for related thermometer models.

The current-measurement response contains no device timestamp. The watcher
reports when it received the latest BLE measurement, using the host's local
clock. Related Govee thermometer protocol notes describe a separate historical
data transfer with records marked by relative minute offsets. That could
support estimated sample times, but the historical transfer was not present in
the supplied H5179 trace and has not been verified against this device.

To monitor an H5179 directly over BLE:

```sh
PYTHONPATH=src python examples/watch_h5179_ble.py
```

The watcher queries temperature/humidity every five seconds and battery every
minute. It prints both Celsius and Fahrenheit conversions.

The related [GoveeBTTempLogger protocol notes](https://github.com/wcbonner/GoveeBTTempLogger#2026-05-21-encryption-details)
document the H5179 family’s command characteristic and battery query. The
[H5075 protocol notes](https://github.com/Heckie75/govee-h5075-thermo-hygrometer/blob/main/API.md)
show the measurement and battery response layouts for the related thermometer
protocol family.

## H7102 tower fan

The H7102 profile uses the Govee service `...1910`, notification
characteristic `...2b10`, and command characteristic `...2b11`. It establishes
the captured AES/RC4 V1 session and exposes power, mode, speed, display, and
Custom settings readback. The selector-2 reply contains a 16-byte block with a
leading marker and three speed/duration entries. The speed byte's `0x10` bit
encodes per-segment oscillation: the capture showed `0x15` for speed 5 with
oscillation on and `0x05` for speed 5 with it off. Durations use duplicated
big-endian minute values and `FF FF FF FF` for Continuous.
The observed work-mode codes are Normal `1`, Sleep `5`, Nature `6`, and Custom
`2`; speed readback has matched app values 5 through 8.
The display query has returned both on and off in user captures.

`H7102Client` provides encrypted state queries and the captured power,
work-mode, speed, panel-display, and oscillation commands. Custom schedules
can be read as speeds, durations, and per-segment oscillation, and written
with `set_custom_schedule()`.
Global oscillation is queried with `AA 1F 01`. The following response byte is
`00` for off and `01` for on. All 12 samples across two physical off/on cycles
matched in powered-on Normal mode at speed 5, with no disconnects. Other
speeds, modes, and powered-off behavior still need confirmation.
Use `request_oscillation_state()` and `receive()` to
update `fan.state.oscillation_state`. The regular state snapshot also queries it.
Speed writes use the captured
`33 05 01 <speed>` layout, confirmed in the capture at speed 8; values 1–4
still need verification. Mode writes for Sleep and Nature were captured;
Normal uses its observed state code. Custom mode requires a settings block.

For the current global oscillation query findings and a controlled off/on
comparison tool, see [H7102 oscillation investigation](docs/H7102_OSCILLATION.md).

Use the client inside the discovered device's connected GATT context. The
example below requests a snapshot and shows one optional control call:

```python
from govee_ble_client import H7102Client, H7102Profile, H7102WorkMode

async with discovered_device.connect() as connection:
    async with H7102Client(connection) as fan:
        await fan.request_state()
        for _ in H7102Profile.state_queries:
            await fan.receive(timeout=2)
        print(fan.state)

        # Commands change the physical device; call only the intended action.
        await fan.set_work_mode(H7102WorkMode.SLEEP)

        # Custom segment speed, duration, and oscillation.
        await fan.request_custom_schedule()
        await fan.receive(timeout=2)
        schedule = fan.state.custom_schedule
        if schedule is not None:
            await fan.set_custom_schedule(
                ((5, 50, True), (7, 60, False), (3, None, False)),
                marker=schedule.marker,
            )
```

The raw watcher remains available for inspecting decrypted frames:

```sh
PYTHONPATH=src python examples/watch_h7102_raw_ble.py
```

To step through readback and command checks with per-step confirmation against
Govee Home, run the verifier below. It powers the fan on before testing its
mode and speed controls, then offers to restore the state it read at startup.
Global oscillation writes now receive a query/readback check as well as the
operator's physical or app comparison.

```sh
PYTHONPATH=src python examples/verify_h7102_ble.py
```

The verifier logs plaintext application frames before encryption. It does not
print session keys or encrypted GATT payloads.

The frame encryption uses the `cryptography` package's AES and legacy ARC4
primitives. This compatibility protocol uses a publicly recovered app key and
an XOR checksum, which is not cryptographic authentication. A separate
AES-GCM V2 scheme is not implemented here. See the
[govee-ble-local protocol reference](https://github.com/Brady-Woods/govee-ble-local/blob/master/spec/docs/GOVEE_BLE_GATT_PROTOCOL.md)
and the negative
[H61F6 investigation](https://github.com/egold555/Govee-Reverse-Engineering/blob/master/Products/H61F6.md).

To watch the latest power notification and app mode/display commands in an
iPhone PacketLogger capture while Govee Home remains connected, run:

```sh
python examples/watch_h7126_trace.py govee.pklg
```

The script prints once per second. Power, mode, and display come from purifier
notifications when present. Filter life remains unavailable unless a BLE
value is captured. The
trace file must be updated on disk during capture for the display to be live;
otherwise stop/save the trace and the script will report the captured values.

For a direct BLE connection that polls the verified power query once per
second, run:

```sh
PYTHONPATH=src python examples/watch_h7126_ble.py
```

This holds a BLE connection, so disconnect Govee Home first. It also attempts
the two direct GATT reads once on startup; the H7126 has previously rejected
both. The script prints power from `AA 01`, mode from `EE 05` or the observed
`AA 05 00/01` queries, and the saved Custom schedule from an `AA 05 02` query
regardless of the currently active mode,
display from `EE 16` or the observed `AA 16` reply, and filter life from byte 6
of `AA 19` replies (confirmed at 0% and 100%).
Other notification bytes are printed without interpretation.

For an interactive hardware-verification pass of the H7126 profile, run:

```sh
PYTHONPATH=src python examples/verify_h7126_ble.py
```

It prints every BLE write and notification, checks device acknowledgements and
decoded state readbacks, and asks you to compare each result with Govee Home or
the cloud API. It does not call the cloud API or ask for API credentials. Each
state-changing test requires confirmation before sending. The filter-reset
step specifically warns that it resets the counter to 100%, then queries and
checks the readback. It reconnects if
the purifier drops its BLE connection during a comparison pause. If every test
runs, the purifier finishes on in Custom mode with the test schedule and panel
display off.

To inspect the purifier and optionally try the status/power probes, place it
near the BLE adapter, close the Govee app so it is not holding a connection,
then run:

```sh
PYTHONPATH=src python examples/inspect_h7126.py
```

The probe prints advertisements and GATT service/characteristic UUIDs and
properties, reads characteristics explicitly marked readable, and briefly
subscribes to notifications. It then offers an optional common-format power
status query (`0xAA 0x01`). It returned a valid off-state response on this
H7126 and a valid on-state response after power-on. The probe never sends the
power-on command unless you explicitly accept its separate prompt, which
appears only after an `off` status notification. Before sharing output, remove device
identifiers/addresses and inspect raw values for unique tokens. Do not share
an unfiltered Bluetooth snoop capture or Wi-Fi setup traffic, which may
contain credentials.

## Development

Install with `uv sync --locked --all-groups`, then run `uv run pytest`.
The pytest suite is entirely offline: real BLE discovery and client entry
points are blocked before collection and during tests. All command writes go
to in-memory fakes.

Run `uv run pytest --cov=govee_ble_client --cov-branch` for coverage, with a 95%
minimum required by the coverage configuration. See [Offline testing](docs/TESTING.md)
for scope, CI commands, and how to add tests. Interactive `examples/watch_*`,
`verify_*`, and `inspect_*` tools require explicit manual execution for hardware
verification.

## License

MIT
