"""Purifier commands and recorded mode, display, filter, and schedule replies."""

import pytest

from govee_ble_client import H7126CustomSegment, H7126Mode, H7126Profile, build_packet
from govee_ble_client.profiles.h7126 import _parse_custom_schedule

PROFILE = H7126Profile()
SCHEDULE = ((H7126Mode.SLEEP, 10), (H7126Mode.LOW, 70), (H7126Mode.HIGH, None))
SETTINGS = bytes.fromhex("0001000a000a020046004603ffffffff")


@pytest.mark.parametrize(
    ("method", "args", "expected"),
    [
        ("request_power_state", (), "aa010000000000000000000000000000000000ab"),
        ("request_filter_life", (), "aa190000000000000000000000000000000000b3"),
        ("request_mode_hint", (), "aa050000000000000000000000000000000000af"),
        ("request_gear_mode", (), "aa050100000000000000000000000000000000ae"),
        (
            "request_custom_mode_settings",
            (),
            "aa050200000000000000000000000000000000ad",
        ),
        ("request_panel_display_state", (), "aa160000000000000000000000000000000000bc"),
        ("turn_on", (), "3301010000000000000000000000000000000033"),
        ("turn_off", (), "3301000000000000000000000000000000000032"),
        ("reset_filter_life", (), "3317010000000000000000000000000000000025"),
        ("set_sleep_mode", (), "3a0501010000000000000000000000000000003f"),
        ("set_low_mode", (), "3a0501020000000000000000000000000000003c"),
        ("set_high_mode", (), "3a0501030000000000000000000000000000003d"),
        ("set_panel_display", (False,), "331600ffffffff00000000000000000000000025"),
        ("set_panel_display", (True,), "331601ffffffff00000000000000000000000024"),
        ("set_custom_mode", (), "3a05020003000a000a02000a000a01ffffffff3d"),
        (
            "set_custom_schedule",
            (SCHEDULE,),
            "3a05020001000a000a020046004603ffffffff3d",
        ),
    ],
)
def test_all_public_commands_match_protocol_examples(
    method, args, expected, connection_factory, run_async
):
    connection = connection_factory(PROFILE)
    run_async(getattr(PROFILE, method)(connection, *args))
    connection.write.assert_awaited_once_with(
        PROFILE.write_uuid, bytes.fromhex(expected)
    )


@pytest.mark.parametrize(
    ("payload", "mode"),
    [
        (b"\x00\x02", H7126Mode.CUSTOM),
        (b"\x01\x01", H7126Mode.SLEEP),
        (b"\x01\x02", H7126Mode.LOW),
        (b"\x01\x03", H7126Mode.HIGH),
        (b"\x00\x01", None),
        (b"\x01\xff", None),
    ],
)
def test_mode_query_distinguishes_family_from_selected_gear(payload, mode):
    reply = PROFILE.parse_notification(build_packet(0xAA, 5, payload))
    assert reply.mode is mode
    assert reply.mode_query_response is True


@pytest.mark.parametrize(
    ("code", "mode"),
    [(1, H7126Mode.SLEEP), (2, H7126Mode.LOW), (3, H7126Mode.HIGH), (255, None)],
)
def test_unsolicited_mode_notifications(code, mode):
    reply = PROFILE.parse_notification(build_packet(0xEE, 5, bytes((1, code))))
    assert reply.mode is mode


@pytest.mark.parametrize("prefix", [0xAA, 0xEE, 0x3A])
def test_custom_read_and_notifications_preserve_schedule(prefix):
    reply = PROFILE.parse_notification(build_packet(prefix, 5, b"\x02" + SETTINGS))
    assert reply.custom_settings == SETTINGS
    assert reply.custom_schedule.marker == 0
    assert [
        (s.mode, s.duration_minutes) for s in reply.custom_schedule.segments
    ] == list(SCHEDULE)
    assert reply.custom_schedule.segments[2].is_continuous
    assert not reply.mode_query_response
    assert reply.mode is (None if prefix == 0xAA else H7126Mode.CUSTOM)


@pytest.mark.parametrize(
    ("prefix", "enabled"), [(0xAA, False), (0xAA, True), (0xEE, False), (0xEE, True)]
)
def test_display_replies(prefix, enabled):
    reply = PROFILE.parse_notification(build_packet(prefix, 0x16, bytes((enabled,))))
    assert reply.display_state is enabled


@pytest.mark.parametrize("percent", [0, 34, 100])
def test_filter_life_is_at_observed_offset(percent):
    reply = PROFILE.parse_notification(
        build_packet(0xAA, 0x19, bytes((9, 8, 7, 6, 0, percent)))
    )
    assert reply.filter_life_percent == percent


def test_unknown_or_invalid_values_remain_uninterpreted():
    assert (
        PROFILE.parse_notification(
            build_packet(0xAA, 0x19, b"\x00" * 5 + b"\x65")
        ).filter_life_percent
        is None
    )
    assert (
        PROFILE.parse_notification(build_packet(0xAA, 0x16, b"\x02")).display_state
        is None
    )
    assert (
        PROFILE.parse_notification(build_packet(0xEE, 0x16, b"\x02")).display_state
        is None
    )
    assert PROFILE.parse_notification(build_packet(0xEE, 0xFE)).mode is None


@pytest.mark.parametrize("enabled", [0, 1, "on", None])
def test_display_requires_boolean(enabled, connection_factory, run_async):
    connection = connection_factory(PROFILE)
    with pytest.raises(TypeError):
        run_async(PROFILE.set_panel_display(connection, enabled))
    connection.write.assert_not_awaited()


@pytest.mark.parametrize(
    "segments",
    [
        (),
        SCHEDULE[:2],
        ((H7126Mode.CUSTOM, 10), *SCHEDULE[1:]),
        (("invalid", 10), *SCHEDULE[1:]),
        ((H7126Mode.SLEEP, None), *SCHEDULE[1:]),
        ((H7126Mode.SLEEP, True), *SCHEDULE[1:]),
        ((H7126Mode.SLEEP, 0), *SCHEDULE[1:]),
        ((H7126Mode.SLEEP, -1), *SCHEDULE[1:]),
        ((H7126Mode.SLEEP, 1.5), *SCHEDULE[1:]),
        ((H7126Mode.SLEEP, 65535), *SCHEDULE[1:]),
        ((H7126Mode.SLEEP, 65536), *SCHEDULE[1:]),
    ],
)
def test_invalid_custom_schedule_never_writes(segments, connection_factory, run_async):
    connection = connection_factory(PROFILE)
    with pytest.raises(ValueError):
        run_async(PROFILE.set_custom_schedule(connection, segments))
    connection.write.assert_not_awaited()


def test_finite_duration_boundary_and_continuous_sentinel(
    connection_factory, run_async
):
    connection = connection_factory(PROFILE)
    segments = ((H7126Mode.SLEEP, 1), (H7126Mode.LOW, 65534), (H7126Mode.HIGH, 2))
    run_async(PROFILE.set_custom_schedule(connection, segments))
    frame = connection.write.call_args.args[1]
    decoded = PROFILE.parse_notification(frame)
    assert [s.duration_minutes for s in decoded.custom_schedule.segments] == [
        1,
        65534,
        2,
    ]
    assert all(not s.is_continuous for s in decoded.custom_schedule.segments)


@pytest.mark.parametrize(
    ("raw", "words", "minutes"),
    [
        (b"", None, None),
        (b"\xff" * 4, None, None),
        (b"\x00\x0a\x00\x46", (10, 70), None),
        (b"\x00\x0a\x00\x0a", (10, 10), 10),
    ],
)
def test_custom_segment_preserves_unknown_duration(raw, words, minutes):
    segment = H7126CustomSegment(255, raw)
    assert segment.mode is None
    assert segment.duration_raw == raw
    assert segment.duration_words == words
    assert segment.duration_minutes == minutes
    assert _parse_custom_schedule(b"") is None


@pytest.mark.parametrize(
    "raw", [b"", b"\xee", b"\x33\x17\x01", bytes(20), build_packet(0x33, 1)]
)
def test_bad_or_unexpected_frames_are_rejected(raw):
    with pytest.raises(ValueError):
        PROFILE.parse_notification(raw)


def test_filter_life_from_live_h7126_reply():
    """The percentage follows a zero byte in the observed 100% reply."""
    frame = bytes.fromhex("aa 19 00 00 00 06 00 64 00 00 00 00 00 00 00 00 00 00 00 d1")
    assert PROFILE.parse_notification(frame).filter_life_percent == 100


def test_short_filter_reply_does_not_decode_padding_as_percentage():
    frame = bytes.fromhex("aa 19 00 00 00 06 64 d1")
    assert PROFILE.parse_notification(frame).filter_life_percent is None
