"""Reusable asynchronous Bluetooth Low Energy primitives for Govee devices.

This package handles BLE discovery and GATT transport. Device-specific wire
protocols are intentionally supplied by protocol profiles; Govee products do
not expose a single command protocol shared by every model.

Start with GoveeScanner for standalone discovery or pass a host-discovered
native BLEDevice to GattConnection. H5179/H7126 profiles are stateless query,
control, and parsing helpers; H7102Client adds encryption and received state.
Apps own polling, response matching, recovery, and task shutdown. Exceptions
are available from govee_ble_client.exceptions. The application guide and API
reference live in docs/APP_INTEGRATION.md and docs/API.md in the repository.
"""

import logging
import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .device import ConnectionStrategy, DiscoveredDevice, GattConnection
from .encryption import GoveeAesRc4V1
from .profiles import (
    H5179Notification,
    H5179Profile,
    H7102Client,
    H7102CustomSchedule,
    H7102CustomSegment,
    H7102Notification,
    H7102Profile,
    H7102State,
    H7102WorkMode,
    H7126CustomSchedule,
    H7126CustomSegment,
    H7126Mode,
    H7126Notification,
    H7126Profile,
)
from .protocol import (
    GoveeNotification,
    build_command_packet,
    build_packet,
    build_query_packet,
    parse_notification,
)
from .scanner import DeviceFilter, GoveeScanner

logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = [
    "ConnectionStrategy",
    "DeviceFilter",
    "DiscoveredDevice",
    "GattConnection",
    "GoveeAesRc4V1",
    "GoveeNotification",
    "GoveeScanner",
    "H5179Notification",
    "H5179Profile",
    "H7102Client",
    "H7102CustomSchedule",
    "H7102CustomSegment",
    "H7102Notification",
    "H7102Profile",
    "H7102State",
    "H7102WorkMode",
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
try:
    __version__ = version("govee-ble-client")
except PackageNotFoundError:
    # Source-tree imports work before the distribution has been installed.
    _project_file = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if _project_file.is_file():
        with _project_file.open("rb") as _project_stream:
            __version__ = tomllib.load(_project_stream)["project"]["version"]
    else:
        __version__ = "unknown"
    del _project_file
