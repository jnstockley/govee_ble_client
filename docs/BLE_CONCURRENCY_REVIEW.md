# BLE concurrency review

> Development record for the transport hardening work. For current app usage,
> use the [application guide](APP_INTEGRATION.md) and [API reference](API.md).

Review date: 2026-10-24. The findings below describe the original baseline.
Transport hardening is implemented; hardware validation remains pending.
Line-number references in the findings identify that baseline, not current
source positions. Current constructor and error contracts are in the API guide.

## Current data flow

`GoveeScanner.scan()` performs one timed Bleak discovery, copies advertisement
data, and retains each native `BLEDevice`. `DiscoveredDevice.connect()` creates
an independent `GattConnection` and Bleak client. No global lock or shared
session state prevents different devices from running concurrently. Applications
must schedule that concurrency themselves; the README quick start connects
and disconnects devices sequentially.

`GattConnection` delegates reads, writes, subscriptions, and lifecycle calls
directly to Bleak. H5179 and H7126 validate the service layout before each
command. Their state requests are characteristic writes followed by
notifications, rather than direct ATT reads. Callers own subscription handling
and response matching. Writes leave response selection to Bleak.

H7102 adds a two-step encrypted handshake and explicitly writes without ATT
responses. Notification callbacks copy bytes into an asyncio queue. Receiving
decrypts and validates a frame, then merges recognized values into immutable
state. Its seven-query snapshot has six 80 ms gaps, totaling 480 ms of
intentional pacing, excluding BLE I/O. The caller can already override this
interval. A fresh RC4 context per frame is required by the protocol.

## Findings

1. **High: same-connection lifecycle and I/O are unsynchronized.**
   `device.py:82-126` permits overlapping connect/reconnect/disconnect and
   characteristic operations. Two reconnects can replace each other's client;
   disconnect can run during active I/O. There is no same-device ordering
   contract. Use per-connection coordination while preserving concurrency
   between devices. This is a software race risk, not a measured assertion that
   all backends reject concurrent reads/writes.
2. **High: session shutdown cannot wake waiting receivers.**
   `encryption.py:124` waits on a queue; `close()` drains that queue without
   signaling waiters. A receiver with no timeout can remain blocked after
   close. A dropped GATT link also leaves `established` true until explicit
   cleanup because it only checks for a stored key.
3. **High: notification accumulation is unbounded.**
   `encryption.py:73-74,91,177` has unbounded raw and handshake-pending buffers.
   An application that stops receiving can accumulate memory and process
   increasingly stale state. A bounded buffer needs an explicit overflow
   policy; silently dropping handshake or command replies is undesirable.
4. **Medium: encrypted lifecycle lacks exclusive ownership.**
   Two `connect()` calls can both pass the initial guard before subscription
   completes. `receive()` can compete with the handshake for replies. Direct
   transport users must clean up failed handshakes themselves; H7102 wraps
   this cleanup, but cleanup errors can replace the original failure.
5. **Medium: timeout semantics are incomplete.**
   The GATT constructor does not validate its timeout, unlike the discovery
   record helper. Encrypted handshake validation accepts NaN/infinity, each
   handshake stage receives a separate timeout, and subscription/writes are
   outside those stage deadlines. Raw GATT operations have no library-level
   deadline. Define finite deadlines explicitly, preserving cancellation.
6. **Performance opportunity: connection setup and application scheduling.**
   Optional service UUID filtering can reduce enumeration time on supporting
   platforms. An example with one task per device would demonstrate concurrent
   persistent connections, independent polling, and failure cleanup. A global
   I/O lock would undo that advantage. Automatic retries of state-changing
   writes should not be introduced because delivery may be ambiguous.

## Recommended scope

Harden the existing transport and encrypted session before adding a fleet
manager. Add deterministic offline tests for same-device ordering, concurrent
progress across different devices, cancellation, reconnect races, close and
disconnect wakeups, handshake failure cleanup, and bounded notification
handling. Add optional service filtering and operation deadlines, with explicit
documentation of their scope. Preserve all command bytes, parsing, and the
current default H7102 pacing until hardware evidence supports changing it.
Document how to keep several devices connected on one event loop.

An alternative is a higher-level manager providing discovery refresh,
connection admission, background receive tasks, and reconnection backoff. That
is a larger public API with adapter-specific policy. It does not remove the
need to fix the transport findings above.

## Validation and limits

Implementation resolves same-connection coordination and stale queued writes,
adds native-device resolution and injectable connection setup, wakes encrypted
receivers on close/link loss, bounds combined notification storage, and enforces
complete handshake deadlines. H7102 lifecycle calls are coordinated, and replies
from replaced transports cannot update the new state. Protocol command bytes and
default snapshot pacing remain unchanged. An independent review identified a
cancelled-disconnect ownership leak; a regression test now verifies that a
retained client can be cleaned up on retry. Failed notification teardown can
also be retried.

Setup creates clients lazily. Optional service filtering can reduce enumeration
work on supporting backends. Reception uses an event and `asyncio.timeout`
instead of creating a timed queue-get task for every frame. The concurrent
monitor demonstrates that a slow device need not block another device's I/O.
These are software changes, not measured radio latency improvements.

Verification: 308 library tests and 71 example tests pass; branch-inclusive
coverage is 98.31%. Changed code passes Ruff. Source and wheel distributions
build successfully. All testing is offline on macOS ARM64/Python 3.15; no x86
host, Home Assistant instance, BLE proxy, or physical device was used.

Baseline: `.venv/bin/python -m pytest` passed all 267 library tests in 0.26 s.
The normal `uv run pytest` command hit a pre-existing stale pytest shebang
pointing to the repository's former path; invoking the environment's Python
module works. No device was scanned, connected, or changed during this review.
Existing tests block real BLE access.

Offline tests can establish concurrency and cleanup behavior, but cannot
establish RF latency, adapter capacity, firmware burst tolerance, or platform
reliability. The user targets x86 and ARM64 with a handful of devices.
Operating systems and adapters remain unspecified; hardware-specific tuning
is deferred.
The primary consumer is a Home Assistant custom integration through HACS,
while standalone use remains required. The proposed design therefore adds
external discovery, refreshed device lookup, and injectable connection setup
without introducing a Home Assistant dependency. Home Assistant owns scanner
and adapter policy, including remote proxy connection slots.

References checked against Bleak 3.0.2:

- [Client API](https://bleak.readthedocs.io/en/latest/api/client.html): native
  device objects avoid implicit discovery; optional service filtering is
  platform-dependent; adapter hardware limits simultaneous connections.
- [BlueZ implementation](https://bleak.readthedocs.io/en/latest/_modules/bleak/backends/bluezdbus/client.html):
  backend connection and GATT handling remain Bleak's responsibility.
