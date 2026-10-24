"""PacketLogger parsing uses synthetic records, never private capture files."""

import pytest

from examples import watch_h7126_trace as trace
from govee_ble_client import build_packet


def record(frame, *, receive=False, handle=0x11, opcode=None):
    kind = trace.ATT_RECEIVE if receive else trace.ATT_SEND
    opcode = opcode if opcode is not None else (0x1B if receive else 0x52)
    packet = bytes(7) + bytes((opcode,)) + handle.to_bytes(2, "little") + frame
    body = bytes(8) + kind + packet
    return len(body).to_bytes(4, "little") + body


def test_record_stream_handles_multiple_records_and_partial_tail():
    first = record(build_packet(0x3A, 5, b"\x01\x01"))
    second = record(build_packet(0xAA, 1, b"\x01"), receive=True)
    records = list(trace.iter_pklg_records(first + second + first[:10]))
    assert len(records) == 2
    assert records[0][0] == trace.ATT_SEND
    assert records[1][0] == trace.ATT_RECEIVE
    assert list(trace.iter_pklg_records(b"\x01\x00\x00\x00x")) == []


@pytest.mark.parametrize(("code", "label"), [(1, "Sleep"), (2, "Low"), (3, "High")])
def test_mode_commands_are_labeled_as_app_commands(code, label):
    state = trace.ObservedState()
    trace.update_state(record(build_packet(0x3A, 5, bytes((1, code)))), state)
    assert state.mode == f"{label} (last app command)"


def test_ignores_unrelated_handles_opcodes_and_short_writes():
    state = trace.ObservedState()
    trace.update_state(
        record(build_packet(0x3A, 5, b"\x01\x03"), handle=99)
        + record(build_packet(0x3A, 5, b"\x01\x03"), opcode=0x12)
        + record(b"short"),
        state,
    )
    assert state.mode == "not observed"


@pytest.mark.parametrize(
    ("raw", "field", "expected"),
    [
        (build_packet(0xAA, 1, b"\x00"), "power", "off"),
        (build_packet(0xAA, 1, b"\x01"), "power", "on"),
        (build_packet(0xAA, 0x19, bytes(4) + b"\x64"), "filter_life", "100%"),
        (build_packet(0xEE, 5, b"\x01\x02"), "mode", "Low (BLE notification)"),
        (build_packet(0xEE, 5, b"\x02"), "mode", "Custom (BLE notification)"),
        (build_packet(0xEE, 0x16, b"\x01"), "display", "on (BLE notification)"),
    ],
)
def test_notification_updates(raw, field, expected):
    state = trace.ObservedState()
    trace.update_state(record(raw, receive=True), state)
    assert getattr(state, field) == expected


def test_mode_family_query_does_not_falsely_report_high():
    state = trace.ObservedState()
    trace.update_state(record(build_packet(0xAA, 5, b"\x00\x01"), receive=True), state)
    assert "exact fan mode not included" in state.mode
    trace.update_state(record(build_packet(0xAA, 5, b"\x01\x01"), receive=True), state)
    assert state.mode == "Sleep"


def test_trace_main_reads_only_local_file(tmp_path, monkeypatch, capsys):
    capture = tmp_path / "synthetic.pklg"
    capture.write_bytes(record(build_packet(0xAA, 1, b"\x01"), receive=True))
    monkeypatch.setattr("sys.argv", ["trace", str(capture)])
    monkeypatch.setattr(
        trace.time, "sleep", lambda _: (_ for _ in ()).throw(KeyboardInterrupt())
    )
    trace.main()
    assert "power: on" in capsys.readouterr().out
