"""Recorded global oscillation replies and API behavior without BLE hardware."""

import asyncio
from types import SimpleNamespace

import pytest

from govee_ble import H7102Client, H7102Profile, build_packet
from examples.verify_h7102_ble import _describe, _fields_in, _last_field

OFF = bytes.fromhex("aa1f0100000000000000000000000000000000b4")
ON = bytes.fromhex("aa1f0101000000000000000000000000000000b5")


class FakeTransport:
    established = True

    def __init__(self, frames=()):
        self.frames = list(frames)
        self.written = []

    async def receive(self, *, timeout=None):
        return self.frames.pop(0)

    async def write(self, frame):
        self.written.append(frame)


def make_client(frames=()):
    client = H7102Client(SimpleNamespace())
    transport = FakeTransport(frames)
    client._transport = transport
    return client, transport


@pytest.mark.parametrize(("frame", "expected"), [(OFF, False), (ON, True)])
def test_recorded_oscillation_reply(frame, expected):
    assert H7102Profile.parse_notification(frame).oscillation_state is expected


@pytest.mark.parametrize(("frame", "expected"), [(OFF, False), (ON, True)])
def test_verifier_checks_and_displays_both_binary_states(frame, expected):
    notification = H7102Profile.parse_notification(frame)
    assert "oscillation" in _fields_in(notification)
    assert _last_field([notification], "oscillation") is expected
    assert _describe(notification) == (
        f"global oscillation={'on' if expected else 'off'}"
    )


@pytest.mark.parametrize(
    ("prefix", "command", "payload"),
    [
        (0x33, 0x1F, b"\x01\x01"),
        (0x33, 0x1F, b"\x00"),
        (0xEE, 0x1F, b"\x01\x01"),
        (0xAA, 0x1F, b"\x00\x01"),
        (0xAA, 0x1F, b"\x02\x01"),
        (0xAA, 0x1F, b"\x01\x02"),
        (0xAA, 0x1D, b"\x01"),
    ],
)
def test_unverified_frames_do_not_set_oscillation(prefix, command, payload):
    frame = build_packet(prefix, command, payload)
    assert H7102Profile.parse_notification(frame).oscillation_state is None


def test_bad_checksum_is_rejected():
    damaged = ON[:-1] + bytes((ON[-1] ^ 1,))
    with pytest.raises(ValueError, match="checksum"):
        H7102Profile.parse_notification(damaged)


def test_acks_and_custom_schedule_do_not_override_live_state():
    ack = build_packet(0x33, 0x1F)
    custom = bytes.fromhex("aa0502001500320032050032003203ffffffffbe")
    decoded_custom = H7102Profile.parse_notification(custom)
    assert decoded_custom.custom_schedule.segments[0].oscillate is True
    assert decoded_custom.oscillation_state is None

    async def check():
        client, _ = make_client((ON, ack, custom, OFF))
        assert client.state.oscillation_state is None
        for expected in (True, True, True, False):
            await client.receive()
            assert client.state.oscillation_state is expected

    asyncio.run(check())


def test_query_and_snapshot_include_observed_oscillation_request():
    async def check():
        client, transport = make_client()
        await client.request_oscillation_state()
        assert transport.written == [OFF]
        transport.written.clear()
        await client.request_state(interval=0)
        assert len(transport.written) == len(H7102Profile.state_queries)
        assert transport.written.count(OFF) == 1
        assert all(frame[0] == 0xAA for frame in transport.written)

    asyncio.run(check())


def test_write_keeps_last_read_state_until_reply():
    async def check():
        client, transport = make_client((ON,))
        await client.set_oscillation(True)
        assert client.state.oscillation_state is None
        await client.receive()
        assert client.state.oscillation_state is True
        await client.set_oscillation(False)
        assert client.state.oscillation_state is True
        assert transport.written == [
            build_packet(0x33, 0x1F, b"\x01\x01"),
            build_packet(0x33, 0x1F, b"\x01\x00"),
        ]

    asyncio.run(check())
