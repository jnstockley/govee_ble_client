"""Packet framing against known examples and malformed input."""

from functools import reduce
from operator import xor

import pytest

from govee_ble_client import (
    GoveeNotification,
    build_command_packet,
    build_packet,
    build_query_packet,
    parse_notification,
)


@pytest.mark.parametrize(
    ("builder", "args", "expected"),
    [
        (build_query_packet, (1,), "aa010000000000000000000000000000000000ab"),
        (
            build_command_packet,
            (1, b"\x01"),
            "3301010000000000000000000000000000000033",
        ),
        (
            build_packet,
            (0x3A, 5, b"\x01\x03"),
            "3a0501030000000000000000000000000000003d",
        ),
        (
            build_packet,
            (0xAA, 0x1F, b"\x01"),
            "aa1f0100000000000000000000000000000000b4",
        ),
    ],
)
def test_recorded_frames(builder, args, expected):
    assert builder(*args) == bytes.fromhex(expected)


@pytest.mark.parametrize("size", [0, 1, 16, 17])
def test_packet_padding_and_checksum(size):
    payload = bytes(range(size))
    frame = build_packet(0xEE, 255, payload)
    assert len(frame) == 20
    assert frame[:2] == b"\xee\xff"
    assert frame[2 : 2 + size] == payload
    assert frame[2 + size : 19] == bytes(17 - size)
    assert reduce(xor, frame, 0) == 0


@pytest.mark.parametrize("code", [-1, 256])
def test_query_code_bounds(code):
    with pytest.raises(ValueError):
        build_query_packet(code)


@pytest.mark.parametrize(("prefix", "code"), [(-1, 1), (256, 1), (1, -1), (1, 256)])
def test_packet_code_bounds(prefix, code):
    with pytest.raises(ValueError):
        build_packet(prefix, code)


@pytest.mark.parametrize("payload", ["text", bytearray(b"\x01"), None])
def test_packet_requires_immutable_bytes(payload):
    with pytest.raises(TypeError):
        build_packet(0x33, 1, payload)


def test_oversized_payload_is_not_truncated():
    with pytest.raises(ValueError):
        build_packet(0x33, 1, bytes(18))


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"", "short"),
        (b"\xaa\x01", "short"),
        (b"\x33\x00\x33", "prefix"),
        (b"\xaa\x01\x00", "checksum"),
    ],
)
def test_invalid_notifications(raw, message):
    with pytest.raises(ValueError, match=message):
        parse_notification(raw)


@pytest.mark.parametrize(("value", "expected"), [(0, False), (1, True), (2, None)])
def test_power_preserves_unrecognized_payload(value, expected):
    raw = build_packet(0xAA, 1, bytes((value, 0x42)))
    notification = parse_notification(raw)
    assert notification.raw == raw
    assert notification.payload[:2] == bytes((value, 0x42))
    assert notification.power_state is expected


def test_unknown_command_and_missing_power_payload():
    assert parse_notification(build_packet(0xAA, 0xFE)).power_state is None
    assert GoveeNotification(code=1, payload=b"", raw=b"").power_state is None
    assert parse_notification(b"\xaa\xaa\x00").payload == b""
