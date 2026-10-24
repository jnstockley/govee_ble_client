"""Advertisement filtering and ordering with discovery fully mocked."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from govee_ble_client import DeviceFilter, DiscoveredDevice, GoveeScanner
from govee_ble_client import scanner as scanner_module


def record(**kwargs):
    return DiscoveredDevice(
        "synthetic-id",
        kwargs.pop("name", "ihoment_H7102"),
        kwargs.pop("rssi", -50),
        **kwargs,
    )


def test_empty_filters_match_and_nameless_devices_are_supported():
    assert DeviceFilter().matches(record(name=None))
    assert not DeviceFilter(name_contains="govee").matches(record(name=None))
    assert DeviceFilter(name_contains="H7102").matches(record(name="IHOMENT_h7102"))


@pytest.mark.parametrize(
    ("wanted", "advertised", "expected"),
    [
        (frozenset({"abcd"}), ("ABCD",), True),
        (frozenset({"ABCD"}), ("abcd",), True),
        (frozenset({"abcd", "ef01"}), ("EF01",), True),
        (frozenset({"abcd"}), (), False),
        (frozenset({"abcd"}), ("ef01",), False),
    ],
)
def test_service_filters_are_case_insensitive_intersections(
    wanted, advertised, expected
):
    assert (
        DeviceFilter(service_uuids=wanted).matches(record(service_uuids=advertised))
        is expected
    )


def test_manufacturer_zero_and_predicate_short_circuit():
    predicate = Mock(return_value=True)
    device_filter = DeviceFilter(manufacturer_id=0, predicate=predicate)
    assert not device_filter.matches(record(manufacturer_data={1: b""}))
    predicate.assert_not_called()
    found = record(manufacturer_data={0: b""})
    assert device_filter.matches(found)
    predicate.assert_called_once_with(found)
    assert not DeviceFilter(predicate=lambda _: False).matches(found)


def test_scan_filters_sorts_and_forwards_timeout(monkeypatch, run_async):
    def entry(name, rssi):
        return (
            SimpleNamespace(address=f"synthetic-{name}-{rssi}", name=name),
            SimpleNamespace(
                local_name=name,
                rssi=rssi,
                manufacturer_data={},
                service_data={},
                service_uuids=[],
            ),
        )

    discover = AsyncMock(
        return_value={
            "a": entry("H7102 weak", -80),
            "b": entry("other", -10),
            "c": entry("H7102 unknown", None),
            "d": entry("H7102 strong", -30),
        }
    )
    monkeypatch.setattr(scanner_module.BleakScanner, "discover", discover)
    results = run_async(
        GoveeScanner().scan(
            timeout=2, device_filter=DeviceFilter(name_contains="H7102")
        )
    )
    assert [device.rssi for device in results] == [-30, -80, None]
    discover.assert_awaited_once_with(timeout=2, return_adv=True)
    discover.return_value = {}
    assert run_async(GoveeScanner().scan()) == []


@pytest.mark.parametrize("timeout", [0, -1])
def test_invalid_timeout_does_not_discover(monkeypatch, run_async, timeout):
    discover = AsyncMock()
    monkeypatch.setattr(scanner_module.BleakScanner, "discover", discover)
    with pytest.raises(ValueError):
        run_async(GoveeScanner().scan(timeout=timeout))
    discover.assert_not_awaited()
