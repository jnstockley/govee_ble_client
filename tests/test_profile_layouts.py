"""Model identification and GATT validation without constructing BLE backends."""

from types import SimpleNamespace

import pytest

from govee_ble_client import H5179Profile, H7102Profile, H7126Profile
from govee_ble_client.exceptions import UnsupportedDeviceError

PROFILES = (H5179Profile(), H7102Profile(), H7126Profile())


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.model)
def test_matches_is_only_case_insensitive_model_name_hint(profile):
    assert profile.matches(
        SimpleNamespace(name=f"ihoment_{profile.model.lower()}_test")
    )
    assert not profile.matches(SimpleNamespace(name=None))
    assert not profile.matches(SimpleNamespace(name="unrelated"))


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.model)
@pytest.mark.parametrize(
    "mutation", ["empty", "wrong_service", "missing_char", "no_notify", "no_write"]
)
def test_rejects_unsupported_layouts(profile, mutation, connection_factory, run_async):
    connection = connection_factory(profile)
    service = connection.services[0]
    if mutation == "empty":
        connection.services.clear()
    elif mutation == "wrong_service":
        service.uuid = "unrelated"
    elif mutation == "missing_char":
        service.characteristics.pop()
    elif mutation == "no_notify":
        service.characteristics[0].properties.remove("notify")
    else:
        service.characteristics[-1].properties = ["notify", "read"]
    with pytest.raises(UnsupportedDeviceError):
        if profile.model == "H7102":
            profile.validate_connection(connection)
        elif profile.model == "H5179":
            run_async(profile.request_measurement(connection))
        else:
            run_async(profile.request_power_state(connection))
    connection.write.assert_not_awaited()


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.model)
def test_uuid_case_is_ignored(profile, connection_factory, run_async):
    connection = connection_factory(profile)
    connection.services[0].uuid = profile.service_uuid.upper()
    for char in connection.services[0].characteristics:
        char.uuid = char.uuid.upper()
    if profile.model == "H7102":
        profile.validate_connection(connection)
    elif profile.model == "H5179":
        run_async(profile.request_measurement(connection))
    else:
        run_async(profile.request_power_state(connection))


@pytest.mark.parametrize(
    "profile", [H5179Profile(), H7102Profile()], ids=lambda p: p.model
)
def test_write_with_response_property_is_supported(
    profile, connection_factory, run_async
):
    connection = connection_factory(profile, write_property="write")
    if profile.model == "H7102":
        profile.validate_connection(connection)
    else:
        run_async(profile.request_battery(connection))


def test_sensor_layout_ignores_unrelated_characteristics(connection_factory, run_async):
    profile = H5179Profile()
    connection = connection_factory(profile)
    connection.services[0].characteristics.insert(
        0, SimpleNamespace(uuid="unrelated", properties=["read"])
    )
    run_async(profile.request_measurement(connection))
    connection.write.assert_awaited_once()
