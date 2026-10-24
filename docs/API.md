# Public API Reference

Import public records, profiles, clients, and framing helpers from
`govee_ble_client`. Import exceptions from `govee_ble_client.exceptions`.
Examples with response handling are in the [application guide](APP_INTEGRATION.md).
All durations and timeout arguments below use seconds unless otherwise stated.

## Discovery

`await GoveeScanner().scan(*, timeout=5.0, device_filter=None)` returns a
`list[DiscoveredDevice]` sorted by RSSI, strongest first. Unknown RSSI sorts
using a fallback of -1000. The timeout must be finite and positive. The scan
opens no GATT connections, but is not a promise of passive radio scan mode.
Backend discovery errors propagate.

`DeviceFilter(name_contains=None, service_uuids=frozenset(),
manufacturer_id=None, predicate=None)` applies all configured conditions after
discovery. Names use case-insensitive substring matching; service UUIDs require
**any** case-insensitive intersection, not every configured UUID. Manufacturer
ID tests for a key in manufacturer data. The optional synchronous predicate
receives the copied record; its errors propagate. No conditions means match all.

`DiscoveredDevice` exposes `identifier`, `name`, `rssi`, `manufacturer_data`,
`service_data`, and `service_uuids`. `from_advertisement(device, advertisement)`
copies advertisement containers and retains the native Bleak device privately.
The frozen dataclass does not make its dictionaries immutable. `identifier`
is a platform address/UUID and may be randomized; it is not authentication.

`record.connect(**options)` returns an unconnected `GattConnection`. It raises
`ValueError` if the record lacks a native device; use discovery or the public
advertisement factory instead of assigning private fields.

## Connection options

`GattConnection(native_device, *, timeout=10.0, operation_timeout=10.0,
services=None, connector=None, device_resolver=None)` uses a discovered native
Bleak `BLEDevice`. `DiscoveredDevice.connect()` accepts the same keyword options.

| Option | Contract |
| --- | --- |
| `timeout` | Finite positive deadline for each GATT connect/reconnect call, including lock wait and setup |
| `operation_timeout` | Finite positive deadline for each read/write/notify/disconnect call, including lock wait |
| `services` | Optional UUID iterable, copied to a tuple; limits service enumeration where the backend supports it; omitted by default |
| `device_resolver` | Optional nonblocking synchronous callable returning the latest native device before each setup, or `None` to fail as unreachable |
| `connector` | Optional async `ConnectionStrategy`; otherwise a fresh Bleak client is created and connected |

Cancellation of active I/O retires its session and attempts disconnection.
Cancellation while merely waiting for the lock does not cancel another task's
I/O. Failed setup cleanup has a separate `operation_timeout` budget, so elapsed
time may exceed `timeout`. A disconnect failure retains the client handle for
cleanup retry. Backend failures propagate; uncertain app writes are never replayed.

### GATT operations

| Member | Result and behavior |
| --- | --- |
| `await connect()` | Returns `self`; idempotent while connected; creates a fresh client for a new session |
| `await disconnect()` | Returns `None`; invalidates the session and releases the current client after active I/O |
| `await reconnect()` | Returns `self`; serializes disconnect/replace/connect and runs the resolver again |
| `is_connected` | Boolean for the current usable session; may be false while failed teardown still needs cleanup |
| `services` | Current Bleak service collection; raises `DeviceNotConnectedError` without a usable connection |
| `await read(characteristic)` | Fresh `bytes` from ATT; not a device-profile state request |
| `await write(characteristic, data, *, response=None)` | Writes immutable `bytes` once; `response=None` delegates selection to Bleak; no application reply wait |
| `await start_notify(characteristic, callback)` | Registers Bleak-style `(characteristic, bytearray)` callback; returns `None` |
| `await stop_notify(characteristic)` | Stops that subscription; returns `None` |
| `add_disconnect_callback(callback)` | Registers a synchronous no-argument observer; returns an idempotent unsubscribe callable |

Characteristic arguments accept UUID strings or Bleak characteristic objects.
Do not retain services/characteristic objects across reconnects. Re-establish
subscriptions after replacing the client. Notification callbacks can be
synchronous or async under Bleak's API; keep them short, copy retained buffers,
and explicitly manage any background tasks/queues your app creates. The raw
GATT wrapper does not bound app-created callback queues.

Disconnect observers run on intentional teardown, observed link loss, and
failed session setup; they are not an availability or successful-connect signal.
Their exceptions are isolated with a payload-free warning. Observers remain
registered across sessions until unsubscribed. Old-client disconnect callbacks
cannot invalidate the replacement session.

`async with connection` connects on entry and disconnects on exit. Context exit
can raise a teardown error. Queued operations fail if the session changes before
they acquire ownership. Serialization covers individual operations, not a
query/reply transaction or all handles for the same peripheral.

## H5179 profile

`H5179Profile` is stateless. `model`, `service_uuid`, and `io_uuid` describe its
observed layout. `matches(record)` checks the name as a discovery hint only.
Each request validates connected services and sends a query; all return `None`.
Subscribe to `io_uuid` before requesting values.

| Method | Observation obtained through notifications |
| --- | --- |
| `request_measurement(connection)` | `AA 0A`: temperature and humidity |
| `request_battery(connection)` | `AA 08`: battery percentage |
| `parse_notification(data)` | Synchronous `H5179Notification`; no I/O or state retention |

`H5179Notification` has `raw`, optional `temperature_c`, computed
`temperature_f`, `humidity_percent`, and `battery_percent`. Temperatures are
Celsius on the wire, signed hundredths; humidity is hundredths of a percent.
Out-of-range humidity/battery values raise `ValueError`. Valid unrelated query
codes retain `raw` and leave fields `None`. There is no decoded device timestamp
or stored history API. No settings controls are implemented.

## H7126 profile

`H7126Profile` is stateless. It exposes `model`, `service_uuid`, `notify_uuid`,
and `write_uuid`. `matches(record)` is a name hint. Requests and controls
validate layout, write one frame, and return `None`. Subscribe to `notify_uuid`
and parse with `parse_notification(bytes(data))`.

| Queries | Decoded fields |
| --- | --- |
| `request_power_state(connection)` | `power_state` |
| `request_filter_life(connection)` | `filter_life_percent` (0–100) |
| `request_mode_hint(connection)` | Gear category or `mode=CUSTOM`; `mode_query_response` signals the mode-query family |
| `request_gear_mode(connection)` | `mode` for Sleep/Low/High |
| `request_custom_mode_settings(connection)` | Saved `custom_settings` and `custom_schedule`; does not by itself establish the active mode |
| `request_panel_display_state(connection)` | `display_state`; verified on the supplied purifier |

Controls: `turn_on`, `turn_off`, `set_sleep_mode`, `set_low_mode`,
`set_high_mode`, and `reset_filter_life` each take `connection`.
`set_panel_display(connection, enabled)` requires a boolean.
`set_custom_mode(connection)` applies the captured High/10 min, Low/10 min,
Sleep/Continuous schedule. `set_custom_schedule(connection, segments)` takes
exactly three `(H7126Mode, minutes_or_None)` entries. Segment modes must be
`SLEEP`, `LOW`, or `HIGH`; only the last duration may be `None` (Continuous).
Finite durations are integers 1–65534 minutes; 65535 would collide with the
Continuous wire sentinel and is rejected.

`H7126Notification` fields are `raw`, `power_state`, `mode`,
`mode_query_response`, `custom_settings`, `custom_schedule`, `display_state`,
and `filter_life_percent`. Optional fields start as `None`; booleans can be
`False`, so merge with `is not None`. The parser handles recognized `AA`, `EE`,
and Custom `3A` frames. Some command acknowledgements (for example `33`)
are not accepted as status; a `ValueError` need not imply a transport failure.

`H7126CustomSchedule` contains a marker and three `H7126CustomSegment` values.
Each exposes raw mode/duration fields plus `mode`, `is_continuous`,
`duration_words`, and `duration_minutes`. Unknown mode codes and inconsistent
words remain inspectable rather than being guessed.

## H7102 client and state

`H7102Profile` exposes layout UUIDs, name-hint `matches(record)`,
`validate_connection(connection)`, `state_queries`, and
`parse_notification(plaintext)`. The parser validates an exact 20-byte frame
and XOR checksum; it does not decrypt ciphertext.

`H7102Client(connection, *, on_send=None)` takes an already-connected GATT
handle. Its context establishes encryption on entry and closes encryption on
exit; it does not disconnect caller-owned GATT. `on_send` is a synchronous
callback given plaintext application bytes after the write returns. If it
raises, the command has already been submitted: never interpret that as proof
of non-delivery. Do not log sensitive raw frames.

| Method | Contract |
| --- | --- |
| `connect(*, timeout=8.0)` | Returns `self`; establishes one encrypted session; coalesces successful concurrent calls |
| `reconnect(*, timeout=8.0)` | Returns `self`; replaces both GATT and encryption and resets cached state |
| `close()` | Returns `None`; closes encryption, leaving GATT ownership with the caller |
| `request_state(*, interval=0.08)` | Sends seven queries, with six default gaps; does not wait for replies or refresh state itself; zero disables gaps |
| `request_custom_schedule()` | Queries saved selector-2 settings |
| `request_oscillation_state()` | Queries global oscillation |
| `receive(*, timeout=None)` | Returns one `H7102Notification`, merging recognized values into `state`; one consumer per session |

`connect`/`reconnect` timeout controls the encryption handshake after lifecycle
ownership is acquired. It is not an overall GATT-plus-encryption deadline;
GATT uses its own connection options and cleanup has separate budgets. Wrap a
complete app operation with `asyncio.timeout(...)` when needed.

Controls returning `None`: `turn_on()`, `turn_off()`,
`set_work_mode(H7102WorkMode)`, `set_fan_speed(speed)`,
`set_panel_display(enabled)`, `set_oscillation(enabled)`,
`set_custom_settings(settings)`, and `set_custom_schedule(segments, *, marker=0)`.
Speed accepts integers 1–8; 1–4 still need hardware verification. Work modes
are `NORMAL`, `SLEEP`, `NATURE`, `CUSTOM`, but `set_work_mode(CUSTOM)` is rejected;
Custom requires its settings block. `set_custom_settings` accepts exactly
16 bytes. Typed schedule entries are `(speed, minutes)` or
`(speed, minutes, oscillate)`; two-item entries default oscillation to false.
Exactly three entries are required, with the same finite-duration and
last-segment Continuous rules as H7126. `marker` is an integer byte; preserve
readback's unexplained marker when appropriate.

`H7102State` is an immutable latest-observation record: optional `power_state`,
`work_mode`, `fan_speed`, `display_state`, `oscillation_state`, `custom_settings`,
and `custom_schedule`. New/reconnected clients start unknown. An unknown
reported work-mode code clears the cached work mode to `None`. Controls do not
optimistically update state. `receive()` replaces the state record, so refresh
references in your UI/store. Global oscillation is separate from saved Custom
segment flags. No field carries a device timestamp or automatic freshness.

`H7102Notification` retains `raw`, `prefix`, `command`, and `payload`; derived
properties expose power/display/oscillation, mode code/name, speed, selector,
selector bytes, and Custom schedule. Schedules contain a marker and three
`H7102CustomSegment` records with `raw_value`, `duration_raw`, `speed`,
`oscillate`, `is_continuous`, `duration_words`, and `duration_minutes`.

## Raw encrypted transport and framing

`GoveeAesRc4V1(connection, *, write_uuid, notify_uuid,
notification_buffer_size=256)` is a low-level transport. Capacity must be a
positive integer, excluding booleans. The limit covers both raw notifications
and plaintext retained during setup. It is configured here, not through the
`H7102Client` constructor.

`await connect(timeout=5.0)` subscribes and performs both handshake stages.
The finite positive handshake deadline includes subscription and writes after
lifecycle ownership is acquired. Cleanup has a separate five-second stop-notify
budget. If cleanup fails, retry `close()` before reuse. `established` reflects
a healthy encrypted session and active GATT link, not freshness of device state.
`await write(frame)` accepts one valid 20-byte XOR-checksummed plaintext frame.
`await receive(timeout=None)` returns one decrypted frame; `None` waits without
a deadline, otherwise the timeout must be finite and positive. A receive
timeout alone does not close the session. Malformed frames raise `ValueError`.
`await close()` invalidates keys, wakes receivers, waits for in-flight writes,
and unsubscribes; it leaves GATT connected. Link loss/overflow invalidate the
session. Closed/replaced sessions and competing consumers raise lifecycle errors.

`build_packet(prefix, command_code, payload=b"")` builds a 20-byte XOR frame;
byte codes must be 0–255 and immutable payload bytes must fit 17 bytes.
`build_command_packet(command_code, payload=b"")` selects prefix `33`;
`build_query_packet(query_code)` selects `AA` with zero padding. These builders
validate framing, not device capability.

`parse_notification(data)` is the common **AA-prefix** parser, returning
`GoveeNotification(code, payload, raw)`. It requires at least three bytes and a
valid XOR checksum, not necessarily 20 bytes. Profile-specific length/prefix
rules differ: H5179 and H7126 AA parsing reuse this common parser; H7126 non-AA
and H7102 frames enforce 20 bytes. Use the selected profile's parser, not the
common parser for arbitrary Govee traffic.

## Exceptions

| Exception | Meaning |
| --- | --- |
| `GoveeBLEError` | Base for the three library-specific errors below |
| `DeviceNotConnectedError` | No usable connection, unreachable resolver result, or stale queued GATT operation |
| `UnsupportedDeviceError` | Connected GATT layout does not match the selected profile |
| `NotificationBufferOverflowError` | Encrypted buffer exhausted; also a `BufferError`; session requires recovery |
| `ValueError` / `TypeError` | Invalid arguments or unsupported/malformed notification data |
| `RuntimeError` | Lifecycle or single-consumer misuse, or closed encrypted session |
| `TimeoutError` | Setup, I/O, or receive deadline expired |
| `bleak.exc.BleakError` subclasses | Backend failures propagate without being wrapped in `GoveeBLEError` |

Cancellation uses `asyncio.CancelledError` and propagates. See the
[recovery guide](APP_INTEGRATION.md#timeouts-errors-and-recovery) for decisions
about freshness, retrying setup, and ambiguous control delivery.
