# Using govee-ble-client in an Application

Use this guide for application code. The [API reference](API.md) describes the
full surface; [device status](PROJECT_STATUS.md) records hardware limits.
The code blocks below define helpers for your existing async app. They open
connections only when called; they do not choose a device or scan implicitly.

## Choose the right layer

| Layer | App responsibilities |
| --- | --- |
| `GoveeScanner` | Standalone advertisement discovery; choose the intended device |
| `GattConnection` | Own one handle per peripheral; connect, subscribe, disconnect |
| `H5179Profile`, `H7126Profile` | Subscribe before queries, parse callbacks, merge recognized fields, match replies |
| `H7102Client` | Own a connected GATT handle, establish encryption, run one receiver, publish `fan.state` |
| `GoveeAesRc4V1` | Advanced raw encrypted frames; prefer `H7102Client` for the fan |

Keep all objects for a connection on one running asyncio event loop. Do not
call `asyncio.run()` for each poll or share a connection across threads/loops.
Use your app's loop; standalone programs normally call `asyncio.run(main())`
once. Avoid blocking work or synchronous sleeps on that loop.

## Discovery and device selection

Standalone apps can use the README's `GoveeScanner.scan()` recipe. It returns
records sorted by signal strength, strongest first, not stable identity.
`DeviceFilter` is a post-scan filter; it does not narrow the platform scan.
Name/service filters do not authenticate a model. Verify the profile's GATT
layout through its operations, and let the user choose the intended device.

A discovered record retains the native Bleak device. `record.connect()` creates
a handle **without connecting**; enter it with `async with` or await its
`connect()`. If another framework owns discovery, construct
`GattConnection(native_ble_device)` directly, or use
`DiscoveredDevice.from_advertisement(native_device, advertisement)` for a record.
Do not fill in the private `_ble_device` field or resolve an address by launching
a competing scanner. Refresh stale native objects with `device_resolver`.

## Read H7126 power through notifications

H7126 state is obtained by writing a query and waiting for a notification.
Advertised characteristic `read` properties do not imply ATT reads of state
are permitted. This complete helper handles both `False` (off) and `True`
(on), ignores unrelated/invalid frames, and bounds the response wait:

```python
import asyncio

from govee_ble_client import DiscoveredDevice, H7126Profile
from govee_ble_client.exceptions import DeviceNotConnectedError


async def read_h7126_power(device: DiscoveredDevice) -> bool:
    profile = H7126Profile()
    async with device.connect(services=(profile.service_uuid,)) as connection:
        ready = asyncio.Event()
        value: bool | None = None

        def notification(_sender: object, data: bytearray) -> None:
            nonlocal value
            try:
                parsed = profile.parse_notification(bytes(data))
            except ValueError:
                return  # Count malformed frames if useful; avoid logging raw data.
            if parsed.power_state is not None:
                value = parsed.power_state
                ready.set()

        unsubscribe = connection.add_disconnect_callback(ready.set)
        try:
            await connection.start_notify(profile.notify_uuid, notification)
            async with asyncio.timeout(5.0):
                await profile.request_power_state(connection)
                await ready.wait()
            if value is None:
                raise DeviceNotConnectedError("Disconnected before power was received")
            return value
        finally:
            unsubscribe()
```

The GATT context disconnects on normal return, errors, or cancellation. It also
removes backend notifications. An existing persistent connection can subscribe
once and use the same callback pattern for many polls; use a fresh event/result
for each application request. Correlate the expected command and selector,
not just the first notification after a write. These protocols have no exposed
transaction IDs, so concurrent same-device query/reply exchanges need
application-level coordination even though individual GATT operations are
serialized. A matching unsolicited status is an observation, not proof that a
particular write caused it.

H5179 uses the same pattern with `profile.io_uuid` for notifications,
`request_measurement()` / `request_battery()`, and `parse_notification()`.
A measurement notification contains temperature/humidity; a battery reply
contains battery percentage. Merge only non-`None` fields. No device timestamp
is decoded: record your own receipt time if your app needs freshness.

## Read H7102 state without counting replies

The client owns encryption and notification parsing; your app owns the GATT
link. This helper waits for the fields it needs within one response budget.
It does not assume one reply for every query or interpret unrelated frames as
a completed snapshot:

```python
import asyncio

from govee_ble_client import DiscoveredDevice, H7102Client, H7102Profile, H7102State


async def read_h7102_state(device: DiscoveredDevice) -> H7102State:
    async with device.connect(services=(H7102Profile.service_uuid,)) as connection:
        async with H7102Client(connection) as fan:
            async with asyncio.timeout(5.0):
                await fan.request_state()
                while any(
                    value is None
                    for value in (
                        fan.state.power_state,
                        fan.state.work_mode,
                        fan.state.fan_speed,
                        fan.state.display_state,
                    )
                ):
                    await fan.receive()
            return fan.state
```

The five-second response budget begins **after** GATT setup and encryption.
If firmware does not report a required field, this example times out; choose
required fields for your app and the verified device. Oscillation and Custom
settings remain optional in this helper. `H7102State` is immutable: each recognized
reply replaces `fan.state`. An old reference stays a historical snapshot.
State is initially unknown, is not optimistically changed by controls, and is
reset on `fan.reconnect()`.

For a persistent app, run one receive task continuously and a separate polling
task. `receive()` is the only path that merges incoming state; querying alone
does not refresh it. Use `asyncio.TaskGroup` or retain task handles and await
them during shutdown. Never run two receivers for one encrypted session or
subscribe separately to its notification characteristic. See the
[multi-device monitor](../examples/watch_multiple_ble.py) for task ownership.

## Controls and readback

Control methods return `None` after writing; they do not wait for confirmation.
Send a control only for an explicit app action, then query its corresponding
state and wait for the relevant observed field. ATT write responses acknowledge
transport delivery, not the device's applied setting. A command echo or
acknowledgement may carry no resulting state.

| Desired change | API | Readback |
| --- | --- | --- |
| H7126 power | `turn_on(connection)` / `turn_off(connection)` | `request_power_state(connection)` -> `power_state` |
| H7126 mode | `set_sleep_mode`, `set_low_mode`, `set_high_mode`, `set_custom_schedule` | Mode hint, then gear mode or Custom settings query |
| H7126 display | `set_panel_display(connection, enabled)` | `request_panel_display_state(connection)` -> `display_state` |
| H7126 filter counter | `reset_filter_life(connection)` | `request_filter_life(connection)` -> `filter_life_percent`; reset only after maintenance |
| H7102 power/mode/speed/display | Client control methods | `request_state()` plus the single receiver |
| H7102 global oscillation | `set_oscillation(enabled)` | `request_oscillation_state()` -> `oscillation_state` |
| H7102 saved Custom schedule | `set_custom_schedule(segments, marker=...)` | `request_custom_schedule()` -> `custom_schedule`; changed schedules need further hardware checks |

H7126 `set_custom_mode()` applies a captured **fixed schedule**; it does not
restore the device's saved schedule. Read that schedule and pass the desired
entries to `set_custom_schedule()` when preserving it matters. H7102
`set_work_mode(CUSTOM)` is rejected: Custom requires settings. Preserve the
marker from a read schedule when writing it back. Use `is_continuous` to
distinguish Continuous from malformed/mismatched duration words; both can yield
`duration_minutes is None`.

## Timeouts, errors, and recovery

All timeout values are seconds. See [API defaults](API.md#connection-options)
for which phase each deadline covers. Cleanup may extend elapsed time beyond
the requested deadline. A caller that adds its own overall timeout must still
allow and await cleanup.

| Failure | What the app should do |
| --- | --- |
| `TimeoutError` waiting for a notification | Mark the requested observation stale; choose whether to query again or reconnect |
| GATT operation timeout/cancellation | The active session is retired; delivery may be uncertain; reconnect before more I/O |
| `DeviceNotConnectedError` or backend disconnect error | End the old receiver; reconnect and resubscribe |
| `UnsupportedDeviceError` | Check device/profile and fresh service discovery; do not send guessed commands |
| `NotificationBufferOverflowError` | The encrypted session failed rather than dropping replies; reconnect and receive continuously |
| `ValueError` parsing a frame | Do not update decoded fields from it; retain safe diagnostics without raw sensitive traffic |
| `RuntimeError` about ownership/state | Fix lifecycle misuse or end the old session; do not retry in a tight loop |
| Other `bleak.exc.BleakError` | Handle the backend-specific connection/I/O failure at your app boundary |

Import library exceptions from `govee_ble_client.exceptions`. They share
`GoveeBLEError`, but validation, timeout, lifecycle, and Bleak errors do **not**
all inherit that base. Preserve `asyncio.CancelledError`; do not swallow it in
a reconnect loop.

`GattConnection.reconnect()` replaces only the GATT client. H5179/H7126 apps
must subscribe again and mark previous state stale. For H7102 use
`await fan.reconnect()` so both GATT and encryption are replaced; avoid
reconnecting the raw link underneath the fan client. Its `timeout` argument
covers encryption, while GATT uses the original connection options. No library
method automatically replays the failed app command.

Connection strategies may retry **setup**. Application reconnect loops should
use capped backoff with jitter, stop during unload, and keep failures local to
the affected device. After ambiguous control delivery, read the actual state
before deciding to send another control. If `disconnect()` or encrypted
`close()` fails, retain the handle and retry cleanup; ownership is kept for
that purpose. Context-manager cleanup can itself raise, so handle failures
around the entire context, not just the query inside it.

## Logging and debugging

The library uses Python's standard `logging` hierarchy under
`govee_ble_client`, with module loggers such as `govee_ble_client.device`,
`govee_ble_client.scanner`, and `govee_ble_client.encryption`. A package
`NullHandler` keeps it silent when the host has no logging handlers. Records
propagate to host handlers; the package does not call `basicConfig()`, change
levels, choose a formatter, or create console/file handlers. This follows
[Python's library logging guidance](https://docs.python.org/3/howto/logging.html#configuring-logging-for-a-library).

For a standalone application, configure logging once at the entry point:

```python
import logging

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logging.getLogger("govee_ble_client").setLevel(logging.DEBUG)
```

In a Home Assistant custom integration, let HA configure handlers. Enable the
library namespace in `configuration.yaml` (the import name uses underscores):

```yaml
logger:
  default: warning
  logs:
    govee_ble_client: debug
    # Also add custom_components.<your_integration_domain>: debug if needed.
```

The [Home Assistant logger documentation](https://www.home-assistant.io/integrations/logger/)
also describes changing levels with `logger.set_level`. To support the
integration's built-in debug logging controls, declare the library namespace in
your integration manifest's `loggers` list:

```json
"loggers": ["govee_ble_client"]
```

This is a manifest field fragment, alongside the integration's existing fields.
See [Home Assistant's manifest logging guidance](https://contributors.home-assistant.io/core/integrations/manifest/#loggers).
Enabling the custom integration's own namespace alone does not enable a separate
library namespace. Return the library to `warning` after collecting diagnostics
to avoid ongoing debug volume. No Home Assistant dependency is required here.

DEBUG records describe connection/reconnection, notification subscriptions,
GATT reads/writes, encrypted receive/close, handshake stages, scan counts,
buffer overflow, and cleanup failures. Timed operations report the configured
timeout, elapsed seconds, and completion or safe exception class name. GATT
elapsed time includes lock waits and any cleanup; it is not radio latency.
Handshake timing includes lifecycle waits/cleanup, while its configured timeout
covers subscription and both handshake stages after lifecycle ownership.
Encrypted close reports the five-second notification teardown budget; lifecycle
waits are additional. A completed GATT write still does not confirm device state.

Connection-related records include a `[connection-N]` prefix and a structured
`connection_id` attribute. The token identifies a handle within one process,
persists across reconnects, and is unrelated to an address, name, or stable
device identifier. GATT, encryption, and H7102 client logs share it when using a
`GattConnection`; standalone injected transports receive their own tokens.
Scanner records have no connection token. Use the ordinary message formatter
above; a formatter requiring `connection_id` on every record needs a fallback
for scanner, application, and dependency records.

Routine operations and propagated failures stay at DEBUG so the application
can decide availability and avoid repeated error messages during reconnects.
Isolated disconnect observer exceptions use WARNING because they do not reach
the caller. Suppressed cleanup failures include the error class at DEBUG.
Cancellation is preserved, and uncertain writes are never replayed.

The library logs metadata only: no payload bytes, decrypted values, session keys,
addresses, advertisement names/data, characteristic arguments, exception
messages, or tracebacks. Third-party backend exceptions can include identifying
data, so this applies even to cleanup diagnostics. Other dependencies and the
host application control their own logs; enabling Bleak debug logging or logging
exceptions in your integration can expose additional data. Hardware capture
examples intentionally print raw traffic for investigation and are separate
from package logging. Review logs and captures before sharing them.

## Several devices and shutdown

Maintain independent per-device workers. Per-connection locks coordinate
individual GATT operations, not an entire query/wait transaction. There is no
global adapter lock, fleet manager, polling scheduler, or automatic availability
tracker. The host app decides connection admission and persistence, especially
when proxies have limited slots. [Bleak's client documentation](https://bleak.readthedocs.io/en/latest/api/client.html)
explains that simultaneous connections and scan/connect coexistence depend on
the adapter.

On app shutdown or integration unload: stop scheduling polls and controls,
cancel and await worker/receiver tasks, close encrypted clients, then disconnect
GATT handles. Nested async contexts implement this order when task scopes are
inside the client contexts. A write already dispatched may finish; commands
are never replayed automatically. Do not discard task handles or cleanup
handles on an error.

## Home Assistant custom integrations

Home Assistant is not installed by this library. Keep its code in the custom
integration, including manifest dependencies, config flow, entities, coordinator,
and unload handlers. HACS distributes that integration; this repository does
not provide a ready-to-install HACS component.

Use HA's shared discovery and a fresh connectable device lookup. Its
[Bluetooth guidance](https://contributors.home-assistant.io/core/integrations/bluetooth/)
recommends fresh clients, at least ten seconds for setup, and retry connector
use. This adapter factory belongs inside the integration and imports its
optional dependencies only when called:

```python
from govee_ble_client import GattConnection, H7126Profile


def make_ha_connection(hass, address: str) -> GattConnection:
    from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
    from homeassistant.components import bluetooth

    def resolve_device():
        return bluetooth.async_ble_device_from_address(hass, address, connectable=True)

    async def connect_client(device, *, timeout, disconnected_callback, services=None):
        options = {} if services is None else {"services": services}
        return await establish_connection(
            BleakClientWithServiceCache,
            device,
            "Govee device",
            disconnected_callback=disconnected_callback,
            max_attempts=3,
            timeout=timeout,  # Constructor option; retry connector owns attempt timing.
            **options,
        )

    device = resolve_device()
    if device is None:
        raise RuntimeError("No connectable adapter can reach this device")
    return GattConnection(
        device,
        timeout=45.0,
        connector=connect_client,
        device_resolver=resolve_device,
        services=(H7126Profile.service_uuid,),
    )
```

This factory returns an unconnected handle. The integration owns connecting,
subscription setup, received state, and awaiting `disconnect()` at unload.
It must translate unavailable-device errors into its own availability/retry
behavior. The 45-second outer budget includes retries/backoff and may end setup
before the dependency finishes all retries. Constructor `timeout` kwargs do
not necessarily set each retry's connection deadline; verify the pinned version.
See the [retry connector API](https://bleak-retry-connector.readthedocs.io/en/stable/usage.html)
for connection policy. This recipe has no HA entity or coordinator implementation;
its imports and lifecycle assumptions must be validated in your target HA version.

A custom `ConnectionStrategy` must return a fresh connected client, preserve
the supplied disconnect callback, and clean up partial setup on error or
cancellation. `services` is passed only when requested. The library owns and
disconnects a successfully returned client. The synchronous `device_resolver`
runs before each session setup. Current retry-connector documentation marks
`ble_device_callback` deprecated and unused, so this recipe omits it. Pass the
freshest available native device and validate routing/cleanup with your chosen
HA and connector versions. See the [best-practice review](BLE_BEST_PRACTICES.md)
for source links and remaining setup, write-mode, and polling recommendations.
