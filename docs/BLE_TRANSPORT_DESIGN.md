# Portable concurrent BLE transport design

> Development record for the transport hardening work. For current app usage,
> use the [application guide](APP_INTEGRATION.md) and [API reference](API.md).

Status: approved and implemented on 2026-10-24; hardware validation remains pending.

## Intent and constraints

Support a handful of persistent device connections on x86 and ARM64 using
Python 3.11+ and Bleak. Keep existing verified commands, encryption framing,
notification parsing, and device capabilities. Leave platform-specific BLE
behavior to Bleak. CPU architecture does not select a connection policy.
Operating systems and adapters remain unspecified.

The primary consumer is a Home Assistant custom integration distributed
through HACS. Home Assistant must not be a runtime dependency of this library.
Standalone applications and existing examples must continue to work.

## Approaches

Recommended: harden the existing connection and encrypted-session classes.
This addresses the races and unbounded waits identified in
[the review](BLE_CONCURRENCY_REVIEW.md) without requiring applications to
adopt a fleet manager.

Alternative: add a manager with connection admission, discovery refresh,
background polling, and reconnect backoff. This introduces application policy
and a larger API; the transport fixes would still be required first.

The user approved transport hardening with optional Home Assistant integration
hooks. No fleet manager or Home Assistant runtime dependency was added.

## Proposed behavior

- Serialize lifecycle changes and GATT operations per connection, while
  allowing different connections to progress independently. Notifications
  must remain deliverable during a pending write or subscription.
- Coalesce concurrent initial connection attempts through an idempotent
  connected-state check. Make reconnect an exclusive disconnect/replace/connect
  sequence. Do not automatically retry writes whose delivery is uncertain.
- Validate finite positive connection and operation deadlines. Give connection
  setup and the complete encrypted handshake explicit deadlines. Offer an
  operation timeout, including time spent waiting for connection coordination;
  preserve normal asyncio cancellation.
- Add optional service UUID filtering and preserve it when replacing a client.
  Keep full service discovery as the default for inspection tools.
- Accept externally discovered native `BLEDevice` objects without requiring
  this library's scanner. Allow the caller to supply a device resolver that
  refreshes that object before every connection attempt, so adapter changes
  do not leave reconnect tied to an obsolete device object.
- Provide an injectable asynchronous connection strategy that returns a
  connected Bleak-compatible client and accepts the library's disconnect
  callback. Its contract includes cleanup on failed or cancelled setup. The
  standalone default uses Bleak; a Home Assistant integration can wrap
  `bleak_retry_connector.establish_connection` without importing Home
  Assistant into the library. Create a fresh client for each connection
  session, including connect after disconnect or failed setup.
- Keep scanner ownership, adapter selection, connection-slot scheduling,
  entity availability, polling cadence, and idle-disconnect policy with the
  host application. Persistent connections are supported, not imposed;
  remote proxies may have scarce connection slots. Do not introduce a
  competing scanner or fleet-wide reconnect loop into Home Assistant.
- Invalidate encrypted sessions on link loss and close. Wake pending receivers
  promptly with a useful exception. Ignore callbacks belonging to an earlier
  subscription after a new session begins.
- Allow one handshake and one notification consumer per encrypted session.
  Application receivers must not steal handshake replies. Failed or cancelled
  setup must clean up subscriptions and key material without hiding the
  initiating exception.
- Bound notification storage with a configurable positive capacity (proposed
  default: 256 frames). On overflow, fail the session explicitly and require
  reconnect; do not silently discard command or handshake replies. The limit
  must cover both raw notifications and frames retained during the handshake.
- Keep H7102's default 80 ms query pacing. It already accepts an override;
  faster default bursts need device evidence. Keep fresh per-frame RC4 state.
- Document one event loop, scan once, native discovered device objects, and
  one persistent worker per device. Demonstrate independent polling and
  cleanup with a runnable example that sends only existing state queries.
  Also document the Home Assistant path: shared discovery, a fresh connectable
  device lookup, injected connection strategy, and explicit unload cleanup.
  Keep standalone and Home Assistant ownership rules distinct.

## Verification

First reproduce the lifecycle, shutdown, and overflow defects with offline
regression tests. Use events and controlled fake peripherals to check ordering
and independence across connections without timing-sensitive speed assertions.
Cover cancellation, failed cleanup, stale notifications, disconnect/reconnect,
handshake ownership, and timeout validation. Run the full library suite and
the existing 95% branch-inclusive coverage check, then build the distribution.
Test injected connection strategies, resolver failure, refreshed device
objects, fresh clients after disconnect, and disconnect callbacks from old
clients. These tests require neither Home Assistant nor live adapters.

Report software scheduling improvements separately from hardware latency.
Do not claim adapter capacity, measured radio speedups, or x86/ARM64 hardware
validation from offline tests. Physical device verification remains a
separate, explicitly initiated activity.

Implementation details, exception names, and test organization are left to
the implementer, provided these behaviors and the existing protocol remain
intact.

## Home Assistant references

- [Bluetooth integration guidance](https://contributors.home-assistant.io/core/integrations/bluetooth/):
  shared scanning, fresh clients per session, at least 10 seconds for connection
  setup, connectable device lookup, and retry-connector use.
- [Retry connector usage](https://bleak-retry-connector.readthedocs.io/en/stable/usage.html):
  connected-client creation and version-specific retry arguments. Retry budgets must
  fit the selected overall connection deadline; application writes are not
  replayed by this library.

Research update: current retry-connector documentation marks its legacy
`ble_device_callback` unused. Native-device refresh is supplied by this
library's resolver before each session. See [the best-practice review](BLE_BEST_PRACTICES.md)
for the corrected recipe and dependency timeout semantics.
