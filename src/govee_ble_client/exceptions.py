"""Library-specific errors; import them from ``govee_ble_client.exceptions``.

Validation, timeout, lifecycle, cancellation, and Bleak backend exceptions also
propagate and are not all subclasses of ``GoveeBLEError``. Apps should preserve
cancellation and distinguish setup retries from uncertain control delivery.
"""


class GoveeBLEError(Exception):
    """Base for the connection, unsupported-layout, and buffer errors below."""


class DeviceNotConnectedError(GoveeBLEError):
    """No usable GATT session, unreachable device, or stale queued operation."""


class UnsupportedDeviceError(GoveeBLEError):
    """Raised when a device does not match a verified protocol layout."""


class NotificationBufferOverflowError(GoveeBLEError, BufferError):
    """Notification storage filled; reconnect to recover a coherent session."""
