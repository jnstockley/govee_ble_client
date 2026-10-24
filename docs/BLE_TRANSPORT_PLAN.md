# Portable BLE transport implementation plan

> Development record for the transport hardening work. For current app usage,
> use the [application guide](APP_INTEGRATION.md) and [API reference](API.md).

**Spec:** [Approved design](BLE_TRANSPORT_DESIGN.md)

**Execution:** Inline implementation, followed by an independent code review.
User approved proceeding on 2026-10-24. Work stays in the supplied checkout;
no commits, publication, or hardware operations are required.

**Constraints:** Python >=3.11, <3.16; Bleak >=3.0.2; no Home Assistant runtime
dependency; preserve protocol bytes and H7102 pacing.

## Task 1: GATT connection ownership

Files: `device.py`, `tests/test_device.py`, `tests/conftest.py`.

Interfaces: `GattConnection` accepts optional `connector`, `device_resolver`,
`services`, and `operation_timeout`. A connector takes a native device and
keyword options `timeout`, `disconnected_callback`, and optional `services`,
and returns a connected client. `add_disconnect_callback(callback)` returns
an unsubscribe function; callbacks take no arguments.

- [x] Write deterministic failing tests for serialization, independent-device
  progress, fresh clients, resolver and connector injection, stale disconnect
  callbacks, setup cleanup, cancellation, and deadlines.
- [x] Implement per-connection coordination, validated deadlines, injected
  connection setup, and disconnect events. Update earlier constructor tests
  for lazy client creation, which avoids allocating unused backend objects.
- [x] Run `.venv/bin/python -m pytest` and confirm the whole suite passes.

## Task 2: Encrypted session ownership and bounded reception

Files: `encryption.py`, `profiles/h7102.py`, `exceptions.py`, transport tests.

Consumes: task 1 disconnect subscription. Produces: bounded session queues,
explicit overflow exception, exclusive handshake and receive ownership,
and immediate invalidation/wakeup on close or link loss.

- [x] Write failing tests for shutdown wakeups, overflow, stale callbacks,
  overlapping handshake/receive, complete handshake deadlines, and cleanup.
- [x] Implement session generation isolation, bounded buffering, cancellation
  cleanup, and safe H7102 lifecycle integration without changing command bytes.
- [x] Run the complete pytest suite and inspect failures before continuing.

## Task 3: Usage, packaging, and final review

Files: README, `docs/TESTING.md`, new multi-device example, new offline example
tests, review/design status, this plan.

- [x] Add and verify a standalone concurrent read-only example and document
  Home Assistant's discovery/connector ownership and proxy slot constraints.
- [x] Run library and example tests, branch-inclusive coverage (>=95%),
  lint checks on changed code, and a distribution build.
- [x] Request independent review; reproduce and fix material findings with
  regression tests. Record evidence and remaining hardware validation limits.

## Review focus

Timeout while queued; cancellation during connection setup; disconnect during
subscription; old-session callbacks after reconnect; overflow while waiting for
handshake. Each belongs in the owning transport's regression tests.

## Execution ledger

- Baseline: 267 tests pass. The pytest executable has a stale shebang; use
  `.venv/bin/python -m pytest`.
- Ruling: use inline execution and the supplied checkout after the user's
  approval to proceed; avoid another permission round for routine reversible
  implementation choices. No automatic commits.
- Task 1 complete: connection regression tests first failed on overlapping I/O,
  stale-client reuse, missing injection/deadlines, and cancelled setup; then
  passed after per-connection coordination and lifecycle changes.
- Task 2 complete: encrypted regression tests first failed on shutdown wakeups,
  overflow, competing consumers, stale callbacks, and cleanup preservation;
  then passed after bounded session buffering and ownership changes.
- Independent core review found one remaining Important issue: cancelled
  teardown lost its client. Fixed with
  `test_cancelled_disconnect_retains_client_for_cleanup_retry` RED -> GREEN.
  The earlier H7102 handshake ownership concern was also addressed with a
  dedicated lifecycle test before the review finished.
- Final semantics: an already-dispatched write may complete; close waits for
  in-flight encrypted writes. Queued operations cannot cross session changes,
  and application writes are never replayed. Overflow requires reconnecting.
- Final verification: 308 library tests, 71 example tests; 98.31% branch-inclusive
  coverage; Ruff and whitespace checks pass; source and universal Python wheel
  build. No physical BLE or Home Assistant instance was used.
- Deferred minors: none from independent review. Adapter behavior and hardware
  latency remain outside offline verification.
