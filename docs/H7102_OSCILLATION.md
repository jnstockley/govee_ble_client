# H7102 Global Oscillation Investigation

Global oscillation is now queried with `AA 1F 01`. The API merges recognized
replies into `H7102State.oscillation_state`; it starts as `None` until a reply
arrives. Saved Custom segment flags remain separate from this reported global
setting.

## Latest Hardware Result

The user's `h7102_oscillation_repeat.json` report completed all four physical
states (off/on/off/on), with three samples per round and no disconnects.
Power, Normal mode, and speed 5 remained fixed:

| State | Response (20 bytes, including checksum) | Samples |
| --- | --- | --- |
| Off | `aa1f0100000000000000000000000000000000b4` | 6 |
| On | `aa1f0101000000000000000000000000000000b5` | 6 |

Absolute byte 2 is selector `01`; byte 3 is the binary oscillation setting.
The off reply is identical to the query. That ambiguity is resolved here by
the differing on reply under controlled physical labels, rather than by
assuming every query-identical response is an echo.

`AA 1F 00`, `AA 1F 02`, and `AA 1D` remained unchanged in both states.
`AA 01` byte 7 followed the same transient pattern in every round, regardless
of oscillation. The automated comparison identified only `AA 1F 01` byte 3.
This verifies repeated state tracking under the tested conditions; other
speeds/modes and powered-off semantics still need confirmation.

The earlier `h7102_oscillation.json` run stopped during round three with
`DeviceNotConnectedError`. Its first six samples showed the same mapping.
The repeat run completes that earlier evidence, though recovery during an
actual disconnect still relies on offline simulation because this run needed
no reconnects.

## Evidence From Existing Captures

The H7102 ATT traffic in `govee_fan.pklg`, `govee_fan_2.pklg`, and
`govee_fan_3.pklg` was decrypted locally using their captured V1 handshakes.
Session keys and handshake contents are omitted here.

| Frame | Observed result | Interpretation |
| --- | --- | --- |
| `33 1F 01 01` / `33 1F 01 00` | Both receive the same zero-filled `33 1F 00` acknowledgement | An acknowledgement cannot distinguish on from off |
| `AA 1F 01` | Five replies in the third capture are identical to the query | Those replies alone were ambiguous; the newer controlled on-state replies establish the changing field |
| `AA 1F 02` | Five replies in the third capture are identical to the query | No demonstrated state value |
| `AA 01` | Byte 7 varies; earlier user samples did not consistently track motion | Retain raw data; no oscillation mapping |
| `AA 05 02` | Segment value bit `0x10` tracks the saved segment's oscillation setting | Useful for Custom configuration, not global state |

Neither plain `AA 1F 00` nor `AA 1D 00` occurs in the third capture.
Plain `AA 1F 00` is a query candidate inferred from the existing H7102 `1F`
command family; its behavior is unverified.

## Public Implementation Lead

[Homebridge's H7107 handler](https://github.com/homebridge-plugins/homebridge-govee/blob/latest/lib/device/fan-H7107.js)
interprets `AA 1D 00` and `AA 1D 01` status frames as oscillation off/on.
Its [device registry](https://github.com/homebridge-plugins/homebridge-govee/blob/latest/lib/device/index.js)
assigns H7102 to the separate H7100 handler. Consequently, the H7107 report
provides a research lead, not an H7102 decoder. Sending an `AA 1D` query to
request that report is also an inference; the source establishes status
decoding rather than H7102 BLE query support.

## Controlled Hardware Comparison

Run from the repository root:

```sh
PYTHONPATH=src python examples/investigate_h7102_oscillation.py --output h7102_oscillation_repeat.json
```

Keep the fan powered on in Normal mode at a fixed speed and disconnect
Govee Home. The tool asks for physical oscillation **off, on, off, on**,
with three passes per state. Use the physical oscillation button and confirm
the fan's motion before continuing. Change no other setting.

After confirmation, the tool sends the encryption handshake and seven
queries: power, mode, speed, `AA 1F 00/01/02`, and the related-model `AA 1D`
candidate. It sends no fan-control commands. It checks the link before every
query and retries an interrupted query once with fresh GATT and encryption.
Partial samples and reconnect session numbers are retained in the report.

The report retains application frames, including notifications pending before
a query. Scan metadata and handshake frames are excluded. Query-identical
replies are marked ambiguous: a legitimate off reply could resemble a
zero-filled query, but identical replies in both states establish no state.
Review raw application frames before sharing a report.

The comparison reports only payload bytes that remain constant in every
sample for each physical state and differ across all four completed rounds.
It excludes checksum differences and requires matching replies in every
pass. Reply windows provide correlation; the protocol has no transaction
identifier that proves which request caused a notification.

If a candidate appears, repeat at another speed and after a fresh connection
before adding a public decoder. If no candidate appears, preserve the report
and capture Govee Home refreshing the device with oscillation already on,
then off, while Wi-Fi and cellular are disabled. That can reveal a query or
notification the current probe does not request.
