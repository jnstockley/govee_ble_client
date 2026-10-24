# govee-ble-client

An asynchronous Python library for discovering and communicating with Govee
Bluetooth Low Energy devices. It supports standalone applications and Home
Assistant custom integrations without requiring Home Assistant.

## Installation

```sh
pip install govee-ble-client
```

Use Python **3.11–3.15**. The distribution is `govee-ble-client`; import
`govee_ble_client`. Runtime dependencies are Bleak and cryptography. Adapter
permissions and access come from the host operating system; installing this
package does not configure Bluetooth or provision devices onto Wi-Fi.

## Start here

- [Application integration guide](docs/APP_INTEGRATION.md): complete read-only
  recipes, notifications, reconnects, multiple devices, and Home Assistant.
- [Public API reference](docs/API.md): constructors, profile methods, return
  values, defaults, and error handling.
- [BLE best-practice review](docs/BLE_BEST_PRACTICES.md): source-backed assessment
  of multi-device behavior and remaining recommendations.
- [Device status](docs/PROJECT_STATUS.md): supported operations and hardware
  verification limits.
- [Protocol evidence and hardware tools](docs/PROTOCOL_EVIDENCE.md): captures,
  packet layouts, monitors, and interactive verifiers.
- [Offline testing](docs/TESTING.md) and [contributor guide](AGENTS.md).

Guides and runnable examples live in the source repository; they are not
installed as importable modules by `pip install`.

## Discover devices

This standalone example scans advertisements without opening GATT connections
or sending device commands:

```python
import asyncio

from govee_ble_client import DeviceFilter, GoveeScanner


async def main() -> None:
    devices = await GoveeScanner().scan(
        timeout=8.0,
        device_filter=DeviceFilter(name_contains="H7126"),
    )
    for device in devices:
        print(device.name, device.rssi)
        # Pass this discovered record to the application guide's read recipe.


if __name__ == "__main__":
    asyncio.run(main())
```

Without a filter, discovery returns all nearby BLE devices. Names and shared
service UUIDs are hints, not proof of identity or protocol support. Keep the
native device objects returned by discovery; applications with an existing BLE
scanner can pass a native `BLEDevice` directly to `GattConnection`.
Home Assistant integrations should use its shared discovery instead of starting
`GoveeScanner`.

## Supported profiles

| Device | Application API | Available operations |
| --- | --- | --- |
| H5179 thermo-hygrometer | `H5179Profile` | Temperature in Celsius/Fahrenheit, humidity, battery queries and notification parsing |
| H7126 air purifier | `H7126Profile` | Power, Sleep/Low/High/Custom modes, three-part Custom schedule, display, filter life and reset |
| H7102 tower fan | `H7102Client` | Encrypted session, power, work mode, speed, display, global oscillation, Custom schedule, merged state |

H5179 and H7126 profiles are stateless: your app subscribes, requests values,
parses replies, and stores its latest state. H7102 provides an encrypted client
and a state snapshot updated only when your app calls `receive()`. Awaiting a
query or control write does **not** confirm the reply or applied device state.

Support is based on captured traffic and checks of supplied devices. H7102
speeds 1–4, some oscillation conditions, and changed Custom schedule round trips
still need hardware verification. No universal Govee command protocol is
assumed. See [device status](docs/PROJECT_STATUS.md) for details.

## Connection ownership

Use one connection handle per peripheral on one asyncio event loop. Operations
are coordinated per connection; separate devices progress independently. A
handle creates fresh Bleak clients for new sessions. Reconnects discard
subscriptions and invalidate queued operations from the previous session.

The application owns polling, response matching, reconnect backoff, availability,
and task shutdown. The library does not automatically retry application writes.
Native-device refresh and connection strategy hooks let a host app manage
adapters and proxies without adding a framework dependency. See the
[Home Assistant recipe](docs/APP_INTEGRATION.md#home-assistant-custom-integrations).

## Manual monitors and verifiers

Run repository examples from the repository root after installing development
dependencies. The multi-device monitor scans once and polls up to five matching
devices independently:

```sh
PYTHONPATH=src python examples/watch_multiple_ble.py
```

It opens physical connections and performs the H7102 handshake; application
commands request state only. A failed worker exits while others continue. It
has no automatic reconnect loop. These scripts are developer tools, not a
fleet manager or an application lifecycle framework.

Other `watch_*`, `inspect_*`, and `verify_*` tools are described in
[protocol evidence](docs/PROTOCOL_EVIDENCE.md). Verifiers can change device
settings; read their prompts and send only the intended actions. Govee Home
may need to disconnect before a standalone tool can connect.

## Development

```sh
uv sync --locked --all-groups
uv run pytest
uv run pytest tests/examples
uv run pytest --cov=govee_ble_client --cov-branch
uv run ruff check
uv run ruff format --check
uv build
```

Tests block real Bluetooth access. Default collection excludes `tests/examples`;
run those separately. Library coverage must reach 95%, including branches.
If the `pytest` launcher points at an old checkout after moving a virtual
environment, use `.venv/bin/python -m pytest`. See [offline testing](docs/TESTING.md)
for CI commands and coverage scope.

## Privacy and compatibility

Device identifiers and captures may identify people or devices; redact them
before sharing. Do not log keys, credentials, or sensitive raw traffic. The
AES/RC4 V1 transport is legacy compatibility code with an XOR checksum, not
modern authenticated encryption. AES-GCM V2 is not implemented.

The library delegates OS behavior to Bleak. Adapter connection limits, RF
latency, and firmware behavior require hardware checks; offline tests do not
establish them. x86 and ARM64 are intended targets, without architecture-specific
code in this library.

Source and issues: [jnstockley/govee_ble_client](https://github.com/jnstockley/govee_ble_client).

## License

MIT
