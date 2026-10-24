"""Sensor query bytes, conversions, and invalid sensor replies."""

import pytest

from govee_ble_client import H5179Notification, H5179Profile, build_packet

PROFILE = H5179Profile()


@pytest.mark.parametrize(
    ("method", "frame"),
    [
        ("request_measurement", "aa0a0000000000000000000000000000000000a0"),
        ("request_battery", "aa080000000000000000000000000000000000a2"),
    ],
)
def test_query_frames_are_recorded_only_in_fake_connection(
    method, frame, connection_factory, run_async
):
    connection = connection_factory(PROFILE)
    run_async(getattr(PROFILE, method)(connection))
    connection.write.assert_awaited_once_with(PROFILE.io_uuid, bytes.fromhex(frame))


@pytest.mark.parametrize(
    ("temperature", "humidity"), [(2345, 4567), (-1234, 0), (-32768, 10000), (32767, 1)]
)
def test_measurement_signed_little_endian_hundredths(temperature, humidity):
    payload = temperature.to_bytes(2, "little", signed=True) + humidity.to_bytes(
        2, "little"
    )
    frame = build_packet(0xAA, 0x0A, payload)
    reading = PROFILE.parse_notification(frame)
    assert reading.raw == frame
    assert reading.temperature_c == temperature / 100
    assert reading.temperature_f == pytest.approx(temperature / 100 * 1.8 + 32)
    assert reading.humidity_percent == humidity / 100
    assert reading.battery_percent is None


@pytest.mark.parametrize("value", [0, 1, 50, 100])
def test_battery_percentage(value):
    reading = PROFILE.parse_notification(build_packet(0xAA, 8, bytes((value,))))
    assert reading.battery_percent == value
    assert reading.temperature_c is None
    assert reading.temperature_f is None


@pytest.mark.parametrize(
    ("code", "payload"), [(8, b"\x65"), (8, b"\xff"), (10, b"\x00\x00\x11\x27")]
)
def test_rejects_out_of_range_sensor_values(code, payload):
    with pytest.raises(ValueError, match="range"):
        PROFILE.parse_notification(build_packet(0xAA, code, payload))


def test_unknown_and_short_replies_preserve_raw_without_inventing_readings():
    # The shared envelope allows variable-length replies; these are valid XOR.
    for raw in (b"\xaa\x08\xa2", b"\xaa\x0a\x01\xa1", build_packet(0xAA, 0xFE)):
        reading = PROFILE.parse_notification(raw)
        assert reading.raw == raw
        assert (
            reading.temperature_c
            is reading.humidity_percent
            is reading.battery_percent
            is None
        )
    assert H5179Notification(raw=b"").temperature_f is None


@pytest.mark.parametrize("raw", [b"", b"\xaa\x0a", b"\x33\x08\x3b", b"\xaa\x08\xff"])
def test_corrupt_replies_are_rejected(raw):
    with pytest.raises(ValueError):
        PROFILE.parse_notification(raw)
