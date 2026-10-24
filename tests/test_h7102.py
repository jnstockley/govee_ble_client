"""Fan commands, validation, session lifecycle, and partial state updates."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

from govee_ble_client import (
    H7102Client,
    H7102CustomSegment,
    H7102Notification,
    H7102Profile,
    H7102State,
    H7102WorkMode,
    build_packet,
)
from govee_ble_client.profiles import h7102 as module
from govee_ble_client.profiles.h7102 import _parse_custom_schedule

PROFILE = H7102Profile()
SEGMENTS = ((5, 50, True), (7, 60, False), (3, None, False))
SETTINGS = bytes.fromhex("00150032003207003c003c03ffffffff")


@pytest.fixture
def client_factory(connection_factory, monkeypatch):
    created = []

    def factory():
        connection = connection_factory(PROFILE)
        transport = SimpleNamespace(
            established=True,
            connect=AsyncMock(),
            close=AsyncMock(),
            write=AsyncMock(),
            receive=AsyncMock(),
        )
        callback = Mock()
        client = H7102Client(connection, on_send=callback)
        client._transport = transport
        created.append((client, connection, transport, callback))
        return created[-1]

    return factory


@pytest.mark.parametrize(
    ("method", "args", "prefix", "command", "payload"),
    [
        ("turn_on", (), 0x33, 1, b"\x01"),
        ("turn_off", (), 0x33, 1, b"\x00"),
        ("set_work_mode", (H7102WorkMode.NORMAL,), 0x33, 5, b"\x01"),
        ("set_work_mode", (H7102WorkMode.SLEEP,), 0x33, 5, b"\x05"),
        ("set_work_mode", (H7102WorkMode.NATURE,), 0x33, 5, b"\x06"),
        ("set_panel_display", (True,), 0x33, 0x16, b"\x01" + b"\xff" * 4),
        ("set_panel_display", (False,), 0x33, 0x16, b"\x00" + b"\xff" * 4),
        ("set_oscillation", (True,), 0x33, 0x1F, b"\x01\x01"),
        ("set_oscillation", (False,), 0x33, 0x1F, b"\x01\x00"),
        ("request_custom_schedule", (), 0xAA, 5, b"\x02"),
        ("request_oscillation_state", (), 0xAA, 0x1F, b"\x01"),
    ],
)
def test_public_command_shapes(
    method, args, prefix, command, payload, client_factory, run_async
):
    client, _, transport, callback = client_factory()
    run_async(getattr(client, method)(*args))
    expected = build_packet(prefix, command, payload)
    transport.write.assert_awaited_once_with(expected)
    callback.assert_called_once_with(expected)
    assert client.state == H7102State()


@pytest.mark.parametrize("speed", range(1, 9))
def test_all_speeds_encode_without_hardware(speed, client_factory, run_async):
    client, _, transport, _ = client_factory()
    run_async(client.set_fan_speed(speed))
    assert transport.write.call_args.args[0][2:4] == bytes((1, speed))


@pytest.mark.parametrize(
    ("method", "args", "exception"),
    [
        *[
            ("set_fan_speed", (value,), ValueError)
            for value in (0, 9, -1, True, 1.5, "5")
        ],
        ("set_work_mode", ("normal",), TypeError),
        ("set_work_mode", (H7102WorkMode.CUSTOM,), ValueError),
        *[
            (name, (value,), TypeError)
            for name in ("set_panel_display", "set_oscillation")
            for value in (0, 1, None, "on")
        ],
        ("set_custom_settings", (bytearray(16),), TypeError),
        ("set_custom_settings", (bytes(15),), ValueError),
        ("set_custom_settings", (bytes(17),), ValueError),
    ],
)
def test_invalid_commands_never_write(
    method, args, exception, client_factory, run_async
):
    client, _, transport, callback = client_factory()
    with pytest.raises(exception):
        run_async(getattr(client, method)(*args))
    transport.write.assert_not_awaited()
    callback.assert_not_called()


def test_custom_schedule_round_trip_speed_oscillation_and_minutes(
    client_factory, run_async
):
    client, _, transport, _ = client_factory()
    run_async(client.set_custom_schedule(SEGMENTS, marker=0x42))
    expected = bytes((0x42,)) + SETTINGS[1:]
    transport.write.assert_awaited_once_with(build_packet(0x33, 5, b"\x02" + expected))
    reply = PROFILE.parse_notification(build_packet(0xAA, 5, b"\x02" + expected))
    assert reply.custom_schedule.marker == 0x42
    assert [
        (s.speed, s.duration_minutes, s.oscillate)
        for s in reply.custom_schedule.segments
    ] == list(SEGMENTS)
    assert reply.custom_schedule.segments[0].raw_value == 0x15


def test_two_item_segments_default_to_oscillation_off(client_factory, run_async):
    client, _, transport, _ = client_factory()
    run_async(client.set_custom_schedule(((1, 1), (8, 65534), (3, 2))))
    frame = transport.write.call_args.args[0]
    reply = PROFILE.parse_notification(
        bytes((0xAA,)) + frame[1:19] + bytes((frame[-1] ^ 0x33 ^ 0xAA,))
    )
    assert [s.duration_minutes for s in reply.custom_schedule.segments] == [1, 65534, 2]
    assert all(not s.oscillate for s in reply.custom_schedule.segments)


@pytest.mark.parametrize(
    ("segments", "marker", "exception"),
    [
        (SEGMENTS[:2], 0, ValueError),
        (((1,), *SEGMENTS[1:]), 0, ValueError),
        (((1, 10, False, 9), *SEGMENTS[1:]), 0, ValueError),
        *[(SEGMENTS, marker, ValueError) for marker in (-1, 256, True, "0")],
        *[
            (((speed, 10, False), *SEGMENTS[1:]), 0, ValueError)
            for speed in (0, 9, True, "5")
        ],
        (((1, 10, 1), *SEGMENTS[1:]), 0, TypeError),
        (((1, None, False), *SEGMENTS[1:]), 0, ValueError),
        *[
            (((1, duration, False), *SEGMENTS[1:]), 0, ValueError)
            for duration in (0, -1, True, 1.5, 65535, 65536)
        ],
    ],
)
def test_invalid_schedule_is_atomic(
    segments, marker, exception, client_factory, run_async
):
    client, _, transport, _ = client_factory()
    with pytest.raises(exception):
        run_async(client.set_custom_schedule(segments, marker=marker))
    transport.write.assert_not_awaited()


def test_client_context_closes_after_caller_error(client_factory, run_async):
    client, _, transport, _ = client_factory()

    async def check():
        with pytest.raises(RuntimeError, match="caller"):
            async with client as entered:
                assert entered is client
                raise RuntimeError("caller")

    run_async(check())
    transport.connect.assert_awaited_once_with(timeout=8)
    transport.close.assert_awaited_once()


def test_failed_handshake_closes_transport(client_factory, run_async):
    client, _, transport, _ = client_factory()
    transport.connect.side_effect = TimeoutError("handshake")
    with pytest.raises(TimeoutError):
        run_async(client.connect())
    transport.close.assert_awaited_once()


def test_reconnect_replaces_encryption_and_clears_cached_state(
    client_factory, monkeypatch, run_async
):
    client, connection, old, _ = client_factory()
    client.state = H7102State(power_state=True, fan_speed=5)
    new = SimpleNamespace(connect=AsyncMock(), close=AsyncMock(), established=True)
    factory = Mock(return_value=new)
    monkeypatch.setattr(module, "GoveeAesRc4V1", factory)
    assert run_async(client.reconnect(timeout=3)) is client
    old.close.assert_awaited_once()
    connection.reconnect.assert_awaited_once()
    new.connect.assert_awaited_once_with(timeout=3)
    assert client.state == H7102State()


def test_disconnected_client_rejects_send_receive_and_query(client_factory, run_async):
    client, _, transport, _ = client_factory()
    transport.established = False
    for operation in (client.turn_on, client.receive, client.request_state):
        with pytest.raises(RuntimeError, match="not connected"):
            run_async(operation())
    transport.write.assert_not_awaited()
    transport.receive.assert_not_awaited()


def test_failed_write_is_not_logged_as_transmitted(client_factory, run_async):
    client, _, transport, callback = client_factory()
    transport.write.side_effect = RuntimeError("write failed")
    with pytest.raises(RuntimeError):
        run_async(client.turn_on())
    callback.assert_not_called()


def test_snapshot_spacing_and_invalid_interval(client_factory, monkeypatch, run_async):
    client, _, transport, _ = client_factory()
    sleep = AsyncMock()
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    run_async(client.request_state(interval=0.1))
    assert sleep.await_args_list == [call(0.1)] * (len(PROFILE.state_queries) - 1)
    transport.write.reset_mock()
    with pytest.raises(ValueError):
        run_async(client.request_state(interval=-1))
    transport.write.assert_not_awaited()


def test_partial_state_updates_unknown_modes_and_corrupt_frames(
    client_factory, run_async
):
    client, _, transport, _ = client_factory()
    replies = [
        build_packet(0xAA, 1, b"\x01"),
        build_packet(0xAA, 5, b"\x00\x05"),
        build_packet(0xAA, 5, b"\x01\x08"),
        build_packet(0xAA, 0x16, b"\x00"),
        build_packet(0xAA, 5, b"\x02" + SETTINGS),
        build_packet(0xAA, 5, b"\x00\xfe"),
        build_packet(0xEE, 0xFE),
    ]
    transport.receive.side_effect = replies + [b"corrupt"]

    async def check():
        for _ in replies:
            await client.receive(timeout=0.1)
        state = client.state
        assert state.power_state is True
        assert state.work_mode is None
        assert state.fan_speed == 8
        assert state.display_state is False
        assert state.custom_settings == SETTINGS
        assert state.custom_schedule.segments[0].oscillate
        with pytest.raises(ValueError):
            await client.receive()
        assert client.state is state

    run_async(check())


@pytest.mark.parametrize(
    ("raw", "words", "minutes"),
    [
        (b"", None, None),
        (b"\xff" * 4, None, None),
        (b"\x00\x0a\x00\x46", (10, 70), None),
        (b"\x00\x0a\x00\x0a", (10, 10), 10),
    ],
)
def test_segment_duration_and_reserved_bits_are_preserved(raw, words, minutes):
    segment = H7102CustomSegment(0x95, raw)
    assert segment.raw_value == 0x95
    assert segment.speed == 5
    assert segment.oscillate
    assert segment.duration_words == words
    assert segment.duration_minutes == minutes
    assert _parse_custom_schedule(b"") is None


def test_short_unknown_and_invalid_status_fields_are_not_invented():
    for prefix, command, payload in (
        (0xAA, 5, b""),
        (0xAA, 5, b"\x01"),
        (0xEE, 5, b"\x00\x01"),
    ):
        item = H7102Notification(b"", prefix, command, payload)
        assert item.mode_code is item.work_mode is item.fan_speed is None
    for command in (1, 0x16):
        assert (
            PROFILE.parse_notification(build_packet(0xAA, command, b"\x02")).power_state
            is None
        )
        assert (
            PROFILE.parse_notification(
                build_packet(0xAA, command, b"\x02")
            ).display_state
            is None
        )
    with pytest.raises(ValueError, match="20 bytes"):
        PROFILE.parse_notification(b"")


def test_non_mode_frame_has_no_selector_data_or_custom_schedule():
    reply = PROFILE.parse_notification(build_packet(0xAA, 1, b"\x01"))
    assert reply.mode_selector_data is None
    assert reply.custom_schedule is None
    truncated = H7102Notification(b"", 0xAA, 5, b"\x02")
    assert truncated.custom_schedule is None
