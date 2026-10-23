"""Exceptions raised by :mod:`govee_ble`."""


class GoveeBLEError(Exception):
    """Base exception for library errors."""


class DeviceNotConnectedError(GoveeBLEError):
    """Raised when a GATT operation is attempted without a connection."""


class UnsupportedDeviceError(GoveeBLEError):
    """Raised when a device does not match a verified protocol layout."""
