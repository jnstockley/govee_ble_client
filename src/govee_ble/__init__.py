"""Reusable asynchronous Bluetooth Low Energy primitives for Govee devices.

This package handles BLE discovery and GATT transport. Device-specific wire
protocols are intentionally supplied by protocol profiles; Govee products do
not expose a single command protocol shared by every model.
"""

from .device import DiscoveredDevice, GattConnection
from .protocol import (
    GoveeNotification,
    build_command_packet,
    build_packet,
    build_query_packet,
    parse_notification,
)
from .profiles import (
    H7126CustomSchedule,
    H7126CustomSegment,
    H7126Mode,
    H7126Notification,
    H7126Profile,
)
from .scanner import DeviceFilter, GoveeScanner

__all__ = [
    "DeviceFilter",
    "DiscoveredDevice",
    "GattConnection",
    "GoveeNotification",
    "GoveeScanner",
    "H7126CustomSchedule",
    "H7126CustomSegment",
    "H7126Mode",
    "H7126Notification",
    "H7126Profile",
    "build_command_packet",
    "build_packet",
    "build_query_packet",
    "parse_notification",
]
__version__ = "0.1.0"
