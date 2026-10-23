"""Verified device-specific protocol profiles."""

from .h7126 import (
    H7126CustomSchedule,
    H7126CustomSegment,
    H7126Mode,
    H7126Notification,
    H7126Profile,
)
from .h5179 import H5179Notification, H5179Profile

__all__ = [
    "H7126CustomSchedule",
    "H7126CustomSegment",
    "H7126Mode",
    "H7126Notification",
    "H7126Profile",
    "H5179Notification",
    "H5179Profile",
]
