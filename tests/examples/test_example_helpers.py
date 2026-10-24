"""Parsing, state handling, and reporting helpers used by manual tools."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from examples import inspect_h7126 as inspect
from examples import verify_h7102_ble as fan
from examples import verify_h7126_ble as purifier
from examples import watch_h5179_ble as sensor
from examples import watch_h7102_raw_ble as fan_watch
from examples import watch_h7126_ble as purifier_watch
from govee_ble_client import (
    H5179Notification,
    H7102CustomSegment,
    H7102Profile,
    H7126Mode,
    H7126Notification,
    H7126Profile,
    build_packet,
)
from govee_ble_client.exceptions import GoveeBLEError

CUSTOM = bytes.fromhex("0001000a000a020046004603ffffffff")
FAN_CUSTOM = bytes.fromhex("00150032003207003c003c03ffffffff")
EXPECTED = ((H7126Mode.SLEEP, 10), (H7126Mode.LOW, 70), (H7126Mode.HIGH, None))


def test_inspection_helpers_keep_latest_decodable_power():
    assert inspect._hex_map({12: b"\x00\xff"}) == {"12": "00ff"}
    off = build_packet(0xAA, 1, b"\x00")
    on = build_packet(0xAA, 1, b"\x01")
    assert inspect._latest_power_state([off, on, b"bad"]) is True
    assert inspect._latest_power_state([on, off]) is False
    assert inspect._latest_power_state([b"bad"]) is None


def test_inspection_simulated_query_and_power_confirmation(
    monkeypatch, connection_factory, run_async, capsys
):
    connection = connection_factory(H7126Profile())
    connection.services[0].description = "test service"
    for char in connection.services[0].characteristics:
        char.description = "test characteristic"
        char.properties.append("read")
    callbacks = {}
    power = False

    async def subscribe(uuid, callback):
        callbacks[uuid] = callback

    async def write(uuid, data):
        nonlocal power
        if data[:2] == b"\x33\x01":
            power = data[2] == 1
        callbacks[inspect.NOTIFY_UUID](
            None, bytearray(build_packet(0xAA, 1, bytes((power,))))
        )

    connection.start_notify.side_effect = subscribe
    connection.write.side_effect = write

    class Context:
        async def __aenter__(self):
            return connection

        async def __aexit__(self, *args):
            return False

    device = SimpleNamespace(
        name="ihoment_H7126_test",
        rssi=-45,
        manufacturer_data={},
        service_data={},
        service_uuids=[],
        connect=lambda **kwargs: Context(),
    )
    monkeypatch.setattr(
        inspect,
        "GoveeScanner",
        lambda: SimpleNamespace(scan=AsyncMock(return_value=[device])),
    )
    answers = iter(("0", "y", "y"))
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(inspect.asyncio, "sleep", AsyncMock())
    run_async(inspect.main())
    assert [c.args[1][:3] for c in connection.write.await_args_list] == [
        b"\xaa\x01\x00",
        b"\x33\x01\x01",
        b"\xaa\x01\x00",
    ]
    assert "power state: on" in capsys.readouterr().out


def test_sensor_updates_do_not_erase_unrelated_fields_and_print_host_time(capsys):
    values = {"temperature_c": 20.0, "humidity_percent": 40.0, "battery_percent": 90}
    sensor._update_readings(H5179Notification(raw=b"", battery_percent=0), values)
    assert values == {
        "temperature_c": 20.0,
        "humidity_percent": 40.0,
        "battery_percent": 0,
    }
    stamp = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
    sensor._print_readings(values, stamp)
    output = capsys.readouterr().out
    assert "20.00 °C / 68.00 °F" in output
    assert "battery: 0%" in output
    assert stamp.isoformat(timespec="seconds") in output
    sensor._print_readings(
        {"temperature_c": None, "humidity_percent": None, "battery_percent": None},
        None,
    )
    assert "unknown" in capsys.readouterr().out


@pytest.mark.parametrize("respond", [True, False])
def test_sensor_request_success_and_timeout_are_local(
    respond, monkeypatch, run_async, capsys
):
    monkeypatch.setattr(sensor, "RESPONSE_TIMEOUT", 0.001)

    async def check():
        event = asyncio.Event()

        async def request(connection):
            if respond:
                event.set()

        await sensor._request_and_wait(object(), request, event)

    run_async(check())
    assert ("before timeout" in capsys.readouterr().out) is not respond


def test_purifier_watch_state_keeps_saved_schedule_separate_from_active_mode(run_async):
    async def check():
        event = asyncio.Event()
        others = asyncio.Queue()
        state = {"mode": H7126Mode.HIGH}
        raw = build_packet(0xAA, 5, b"\x02" + CUSTOM)
        purifier_watch._on_notification(raw, event, state, others)
        assert state["mode"] is H7126Mode.HIGH
        assert state["custom_settings"] == CUSTOM
        assert "Continuous" in purifier_watch._format_custom_schedule(
            state["custom_schedule"]
        )
        purifier_watch._on_notification(
            build_packet(0xAA, 5, b"\x00\x01"), event, state, others
        )
        assert state["mode"] is None
        purifier_watch._on_notification(
            build_packet(0xAA, 1, b"\x00"), event, state, others
        )
        purifier_watch._on_notification(
            build_packet(0xAA, 0x16, b"\x00"), event, state, others
        )
        purifier_watch._on_notification(
            build_packet(0xAA, 0x19, b"\x00" * 4), event, state, others
        )
        assert state["power"] is state["display"] is False
        assert state["reply_time"] is not None and state["filter_life"] == 0
        purifier_watch._on_notification(b"bad", event, state, others)
        assert event.is_set()
        assert b"bad" in list(others._queue)

    run_async(check())


def test_purifier_schedule_verifier_handles_match_mismatch_and_missing():
    notification = H7126Profile.parse_notification(
        build_packet(0xAA, 5, b"\x02" + CUSTOM)
    )
    verify = purifier.schedule_verifier(EXPECTED)
    assert verify([notification]) is True
    assert verify([]) is None
    assert verify([H7126Notification(raw=b"")]) is None
    assert purifier.schedule_verifier(EXPECTED[::-1])([notification]) is False
    assert purifier._parse_packets([b"bad", notification.raw]) == [notification]


def test_purifier_human_output_includes_states_and_schedule(capsys):
    notifications = [
        H7126Profile.parse_notification(build_packet(0xAA, 1, b"\x00")),
        H7126Profile.parse_notification(build_packet(0xAA, 5, b"\x00\x01")),
        H7126Profile.parse_notification(build_packet(0xAA, 5, b"\x02" + CUSTOM)),
        H7126Profile.parse_notification(build_packet(0xEE, 0x16, b"\x00")),
        H7126Profile.parse_notification(build_packet(0xAA, 0x19, bytes(5) + b"\x64")),
    ]
    purifier._print_human_values(notifications)
    purifier._print_packets([n.raw for n in notifications], notifications)
    output = capsys.readouterr().out
    for phrase in (
        "Power: Off",
        "exact fan mode not included",
        "Continuous",
        "1 hr 10 min",
        "Filter life: 100%",
        "Panel display: Off",
    ):
        assert phrase in output
    purifier._print_human_values([])
    purifier._print_packets([], [])
    assert "none received" in capsys.readouterr().out


def test_filter_reset_ack_and_query_echo_reporting(capsys):
    reset = build_packet(0x33, 0x17, b"\x01")
    ack = build_packet(0x33, 0x17)
    purifier._print_echo_results([("uuid", reset)], [ack], expect_echo=True)
    assert "acknowledgement: received" in capsys.readouterr().out
    purifier._print_echo_results([("uuid", reset)], [reset], expect_echo=True)
    assert "MATCH" in capsys.readouterr().out
    purifier._print_echo_results(
        [("uuid", build_packet(0xAA, 1))], [], expect_echo=False
    )
    assert "not expected" in capsys.readouterr().out
    purifier._print_echo_results(
        [("uuid", build_packet(0x33, 1))], [], expect_echo=True
    )
    assert "not seen" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("5,50,on;7,60,off;3,c,true", ((5, 50, True), (7, 60, False), (3, None, True))),
        ("5,50;7,60;3,continuous", ((5, 50, False), (7, 60, False), (3, None, False))),
        (
            " 5,50,1 ; 7,60,0 ; 3,C,FALSE ",
            ((5, 50, True), (7, 60, False), (3, None, False)),
        ),
    ],
)
def test_custom_input_parser(text, expected):
    assert fan._parse_custom_segments(text) == expected
    raw = fan._encode_custom_settings(expected, marker=0)
    reply = H7102Profile.parse_notification(build_packet(0xAA, 5, b"\x02" + raw))
    assert [
        (s.speed, s.duration_minutes, s.oscillate)
        for s in reply.custom_schedule.segments
    ] == list(expected)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "5,50;7,60",
        "5;7,60;3,c",
        "x,50;7,60;3,c",
        "5,hour;7,60;3,c",
        "5,50,maybe;7,60;3,c",
        "9,50;7,60;3,c",
        "5,0;7,60;3,c",
        "5,65535;7,60;3,c",
        "5,c;7,60;3,c",
    ],
)
def test_custom_input_validation_rejects_before_any_write(text):
    with pytest.raises(ValueError):
        fan._encode_custom_settings(fan._parse_custom_segments(text), marker=0)


def test_fan_reporting_keeps_off_values_and_oscillation_visible():
    schedule = H7102Profile.parse_notification(
        build_packet(0xAA, 5, b"\x02" + FAN_CUSTOM)
    )
    description = fan._describe(schedule)
    assert "speed 5/8, oscillate on" in description
    assert "Continuous" in description
    assert "oscillate on" in fan_watch._format_custom_segment(
        H7102CustomSegment(0x15, bytes.fromhex("00320032"))
    )
    assert fan._format_value(False) == "off"
    assert fan._format_value(b"\x00\xff") == "00ff"
    for field, frame in (
        ("power", build_packet(0xAA, 1, b"\x00")),
        ("display", build_packet(0xAA, 0x16, b"\x00")),
        ("oscillation", build_packet(0xAA, 0x1F, b"\x01\x00")),
    ):
        notification = H7102Profile.parse_notification(frame)
        assert field in fan._fields_in(notification)
        assert fan._last_field([notification], field) is False
    assert fan._last_field([], "power") is None


def test_snapshot_discards_stale_replies_before_query(monkeypatch, run_async, capsys):
    frames = [
        build_packet(0xAA, 1, b"\x00"),
        build_packet(0xAA, 5, b"\x00\x01"),
        build_packet(0xAA, 5, b"\x01\x05"),
        build_packet(0xAA, 5, b"\x02" + FAN_CUSTOM),
        build_packet(0xAA, 0x16, b"\x00"),
        build_packet(0xAA, 0x1F, b"\x01\x00"),
    ]
    notifications = [H7102Profile.parse_notification(frame) for frame in frames]
    stale = H7102Profile.parse_notification(build_packet(0xAA, 1, b"\x01"))
    client = SimpleNamespace(
        receive=AsyncMock(side_effect=[stale, TimeoutError, *notifications]),
        request_state=AsyncMock(),
    )
    assert run_async(fan._read_snapshot(client)) == notifications
    client.request_state.assert_awaited_once()
    assert "queued before fresh snapshot" in capsys.readouterr().out


def test_snapshot_timeout_reports_missing_fields(run_async, capsys):
    client = SimpleNamespace(receive=AsyncMock(side_effect=TimeoutError))
    assert (
        run_async(
            fan._collect_notifications(client, timeout=0.001, require_fields={"power"})
        )
        == []
    )
    assert "Missing readbacks: power" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        (("y",), "match"),
        (("s",), "skipped"),
        (("n", "different"), "mismatch (different)"),
    ],
)
def test_operator_comparison_records_answers_without_cloud_calls(
    answers, expected, monkeypatch, run_async
):
    monkeypatch.setattr(fan, "_ainput", AsyncMock(side_effect=answers))
    results = []
    run_async(fan._compare_with_app(results, "test", ble_result="local readback"))
    assert results[0].app_match == expected
    assert results[0].ble_result == "local readback"


def test_recording_connection_reconnects_and_cleans_up(connection_factory, run_async):
    first = connection_factory(H7126Profile())
    second = connection_factory(H7126Profile())
    first.connect.return_value = first
    second.connect.return_value = second
    device = SimpleNamespace(connect=iter((first, second)).__next__)
    # The device API takes a timeout keyword.
    connections = iter((first, second))
    device.connect = lambda **kwargs: next(connections)
    callback = lambda *_: None
    recorder = purifier.RecordingConnection(device, callback)
    with pytest.raises(GoveeBLEError):
        _ = recorder.services

    async def check():
        async with recorder as entered:
            assert entered is recorder
            assert recorder.services == first.services
            first.is_connected = False
            await recorder.write("write", b"recorded")
            assert recorder.is_connected
            assert recorder.writes == [("write", b"recorded")]
        assert not recorder.is_connected
        await recorder.disconnect()

    run_async(check())
    second.write.assert_awaited_once_with("write", b"recorded")
    second.stop_notify.assert_awaited_once()
    second.disconnect.assert_awaited_once()


def test_recording_connection_subscription_failure_is_cleaned(
    connection_factory, run_async
):
    connection = connection_factory(H7126Profile())
    connection.connect.return_value = connection
    connection.start_notify.side_effect = RuntimeError("subscription")
    recorder = purifier.RecordingConnection(
        SimpleNamespace(connect=lambda **_: connection), lambda *_: None
    )
    with pytest.raises(RuntimeError, match="subscription"):
        run_async(recorder.__aenter__())
    assert not recorder.is_connected
    connection.disconnect.assert_awaited_once()
