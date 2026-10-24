"""Public exports and the library exception hierarchy."""


def test_public_exports_are_present_and_exceptions_share_base():
    import govee_ble_client
    from govee_ble_client import exceptions

    assert all(hasattr(govee_ble_client, name) for name in govee_ble_client.__all__)
    assert issubclass(exceptions.DeviceNotConnectedError, exceptions.GoveeBLEError)
    assert issubclass(exceptions.UnsupportedDeviceError, exceptions.GoveeBLEError)
