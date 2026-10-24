# BLE Best-Practice Review

Reviewed 2026-10-24 against the working tree, installed Bleak 3.0.2, official
Bleak/Home Assistant documentation, and retry-connector 4.7.1 source. This is a
source review, not a hardware certification. Runtime behavior was not changed
as part of this research.

## Assessment

The library's foundation is appropriate for a handful of devices: native
objects from discovery, separate connection handles, asynchronous I/O, fresh
clients per session, notification-based state, and explicit app ownership.
It is **not yet a complete production multi-device policy**. Remaining work
centers on connection admission, explicit write modes, and application polling
and recovery. The diagnostic monitor should not be treated as that policy.

## What matches current guidance

| Area | Current implementation and assessment |
| --- | --- |
| Native discovery objects | `DiscoveredDevice.from_advertisement()` retains the native device and `GattConnection` passes it to Bleak. This avoids the implicit discovery associated with address-only clients. [Bleak client API](https://bleak.readthedocs.io/en/latest/api/client.html) |
| One running event loop | Public guidance and the multi-device monitor keep related objects on one asyncio loop, matching Bleak's requirement. [Bleak troubleshooting](https://bleak.readthedocs.io/en/latest/troubleshooting.html#calling-asyncio-run-more-than-once) |
| Fresh connection clients | Setup creates a fresh Bleak client for each new session. HA explicitly recommends this. GATT's default setup budget is 10 seconds; the separate H7102 encryption default is 8 seconds. [HA Bluetooth guidance](https://contributors.home-assistant.io/core/integrations/bluetooth/#best-practices) |
| Independent connected devices | Per-device workers can read/write/receive concurrently after setup. Bleak's own example keeps existing connections active while establishing another. [Official multi-device example](https://github.com/hbldh/bleak/blob/develop/examples/two_devices.py) |
| Same-connection coordination | A per-handle lock protects lifecycle and I/O. This is a conservative library design, not a universal requirement that all BLE operations everywhere be serialized. BlueZ documents `InProgress` failures for GATT operations. [BlueZ GATT API](https://github.com/bluez/bluez/blob/master/doc/org.bluez.GattCharacteristic.rst) |
| Notifications before queries | Encryption and the app recipes subscribe before requesting state. Model-specific query/reply behavior is supported by the repository's [protocol evidence](PROTOCOL_EVIDENCE.md); this is not a rule that every BLE device needs query writes. |
| Lifecycle cleanup | Async contexts, retained failed-cleanup handles, single-consumer reception, bounded encrypted queues, and cancellation propagation are useful safeguards. Task groups/finally cleanup follow Python's structured concurrency model. [Python asyncio](https://docs.python.org/3/library/asyncio-task.html#task-cancellation) |
| HA discovery ownership | Native-device/resolver/connector hooks allow HA to own discovery and adapter routing. The standalone scanner is not required. HA integrations should use its shared Bluetooth infrastructure. [HA Bluetooth guidance](https://contributors.home-assistant.io/core/integrations/bluetooth/) |

## Gaps and recommendations

### 1. Limit connection establishment separately from connected-device I/O

`examples/watch_multiple_ble.py::run_devices()` starts all workers together.
The per-device lock cannot prevent several devices from attempting setup on
one adapter at once. Bleak's official example uses a shared lock around
scan/connect, then releases it while existing connections remain active.
[Official example](https://github.com/hbldh/bleak/blob/develop/examples/two_devices.py)

**Recommendation:** let the host/connection strategy apply adapter-specific
setup admission. For a standalone direct adapter, start conservatively with
one setup at a time, then measure. Keep established-device I/O independent;
a fleet-wide read/write lock would needlessly couple unrelated devices.
Account for time spent waiting for admission in timeout budgets. In HA, use
its routing/slot management with retry connector instead of adding a separate
competing adapter scheduler.

This is a portable starting policy, not evidence that every adapter rejects
parallel setup or that one global lock is optimal across several adapters.

### 2. Select write response mode explicitly in profiles

H7102 encryption already passes `response=False`. H7126 and H5179 currently
leave it at `None`. Bleak encourages an explicit choice because characteristic
properties can be wrong; its default prefers response writes when `write` is
present. Response-less writes queue data and return without a server response.
[Bleak write API](https://bleak.readthedocs.io/en/latest/api/client.html#bleak.BleakClient.write_gatt_char)

**Recommendation:** make H7126's observed write-without-response mode explicit.
For H5179, resolve the connected characteristic and select the validated mode,
maintaining current behavior until capture evidence establishes otherwise.
The generic raw GATT API can continue exposing an optional choice.

Also tighten H7102 layout validation: it currently accepts a write-only
characteristic even though encryption always uses write-without-response.
Reject unsupported layouts early rather than discovering that mismatch during
the handshake. This is a code-derived inconsistency, not a failure observed on
the supplied fan.

ATT acknowledgement still does not establish an applied Govee setting; use
profile readback. Do not concatenate protocol frames or change encryption
framing to chase throughput without device evidence.

### 3. Treat scanning as host-managed infrastructure in a long-running app

`GoveeScanner.scan()` is a finite `BleakScanner.discover()` convenience wrapper.
It is useful for setup and diagnostics, but does not continuously refresh
native-device references. Bleak recommends its scanner lifecycle/callback APIs
rather than convenience discovery for advanced long-running/multi-device apps.
[Bleak scanner documentation](https://bleak.readthedocs.io/en/latest/api/scanner.html#easy-methods)

**Recommendation:** standalone apps needing ongoing recovery should own a
scanner lifecycle/cache and supply fresh native devices through the existing
hooks. HA should supply its existing discovery records. Avoid repeated scans
for each read or opening several scanners. Active/passive scan mode is a host
policy, and passive scanning is not supported by every backend. [Bleak scanner
API](https://bleak.readthedocs.io/en/latest/api/scanner.html)

### 4. Keep persistence and polling aware of power and connection slots

The monitor holds up to five connections and queries battery/filter/schedule
alongside faster-changing state each cycle. Those are diagnostic defaults,
not researched optimal polling intervals.

ESPHome documents finite active slots; an ESP32 proxy defaults to three, and a
persistent device occupies a slot continuously. Short sessions free slots for
other devices. [ESPHome proxy documentation](https://esphome.io/components/bluetooth_proxy/#how-active-connections-work)

**Recommendation:** choose persistent sessions where command latency and
notifications justify them, especially for the encrypted fan; measure the
handshake/reconnect cost. Consider brief, less frequent sensor sessions when
slots or battery matter. Refresh slow-changing metadata less frequently and
stagger polls. Do not assume H5179 advertisements contain a supported decoded
measurement, or that subscription alone produces updates, without evidence.
No universal five-second interval or five-device capacity is established here.

### 5. Keep callbacks short and coordinate whole query/reply exchanges

The encrypted callback only copies/enqueues bytes. The diagnostic plain-device
callback parses and prints synchronously; stdout can block an app's event loop.
Use a short callback and a bounded application queue/worker when processing or
publication is substantial. Explicitly choose an overflow policy. Python's
asyncio guidance explains why blocking work must leave the loop. [Asyncio
blocking-work guidance](https://docs.python.org/3/library/asyncio-task.html#running-in-threads)

Per-operation locking does not reserve a full query/wait transaction. The host
must coordinate same-device exchanges and match commands/selectors. Continue
using one encrypted receiver and field-based completion deadlines. Do not
assume an exact reply count or retry a control whose delivery is uncertain.
These recommendations follow the code/protocol's lack of transaction IDs;
they are not claimed to be a universal Bleak request-matching feature.

### 6. Correct and version-check the retry-connector recipe

Research found two errors in the earlier application-guide recipe:

- Current usage docs mark `ble_device_callback` deprecated and unused.
- In tagged 4.7.1 source, constructor kwargs do not define the actual connect
  attempt deadline: `establish_connection()` supplies its own timeout to
  `client.connect()`. Its retry loop also distinguishes transient errors, so
  `max_attempts` is not a simple universal count of every physical attempt.

Sources: [current parameter documentation](https://bleak-retry-connector.readthedocs.io/en/stable/usage.html#parameters),
[tagged 4.7.1 implementation](https://github.com/Bluetooth-Devices/bleak-retry-connector/blob/v4.7.1/src/bleak_retry_connector/__init__.py).

The [application guide](APP_INTEGRATION.md#home-assistant-custom-integrations)
is corrected to omit that callback and avoid a per-attempt timing guarantee.
The library's resolver still refreshes the native device before each session.
An overall budget may stop setup before all dependency retries finish; pin/test
compatible integration dependencies and verify cancellation cleanup of
partially connected clients. A mock factory cannot prove remote-adapter cleanup.

## Recommended validation before deploying the integration

Run the existing offline suite, then exercise one Linux/BlueZ host and one
active HA proxy path with a handful of supported devices. Include mixed models,
cold service discovery, all-device startup, independent connected-device I/O,
slot exhaustion, device loss, adapter failover, recovery, and unload during setup
or writes. Include a battery-powered sensor in long-duration checks.

Record setup latency and query-to-matching-notification latency separately
(p50/p95), failure/recovery counts, queue occupancy, and leaked tasks/slots.
Compare serial versus bounded-parallel setup and persistent versus brief
sessions. Retain conservative pacing until tests support reducing it. Repeat
representative runs on x86 and ARM64; CPU architecture alone does not establish
adapter behavior. Redact identifiers and sensitive traffic.

This review does not establish RF performance, battery lifetime, proxy
compatibility, or support for uncaptured firmware. Recommended next code work
is explicit profile write modes/layout validation, then host-aware setup
admission and polling policy. No new universal pacing or concurrency limit is
justified by the sources.
