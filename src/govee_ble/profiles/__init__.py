"""Verified device-specific protocol profiles."""

from .h7126 import (
    H7126CustomSchedule,
    H7126CustomSegment,
    H7126Mode,
    H7126Notification,
    H7126Profile,
)
from .h7102 import (
    H7102Client,
    H7102CustomSchedule,
    H7102CustomSegment,
    H7102Notification,
    H7102Profile,
    H7102State,
    H7102WorkMode,
)
from .h5179 import H5179Notification, H5179Profile

__all__ = [
    "H7126CustomSchedule",
    "H7126CustomSegment",
    "H7126Mode",
    "H7126Notification",
    "H7126Profile",
    "H7102Notification",
    "H7102Profile",
    "H7102Client",
    "H7102CustomSchedule",
    "H7102CustomSegment",
    "H7102State",
    "H7102WorkMode",
    "H5179Notification",
    "H5179Profile",
]
