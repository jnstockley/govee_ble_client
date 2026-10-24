"""Offline checks for example verifier output."""

import pytest

from examples.verify_h7102_ble import _describe, _fields_in, _last_field
from govee_ble_client import H7102Profile

OFF = bytes.fromhex("aa1f0100000000000000000000000000000000b4")
ON = bytes.fromhex("aa1f0101000000000000000000000000000000b5")


@pytest.mark.parametrize(("frame", "expected"), [(OFF, False), (ON, True)])
def test_verifier_checks_and_displays_both_binary_states(frame, expected):
    notification = H7102Profile.parse_notification(frame)
    assert "oscillation" in _fields_in(notification)
    assert _last_field([notification], "oscillation") is expected
    assert _describe(notification) == (
        f"global oscillation={'on' if expected else 'off'}"
    )
