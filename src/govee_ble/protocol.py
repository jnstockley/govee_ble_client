"""Small framing helpers for the common Govee BLE packet format.

These helpers describe a format observed across some Govee BLE products. They
do not imply that a particular model supports a query or command code.
"""

from __future__ import annotations

from dataclasses import dataclass

FRAME_LENGTH = 20
QUERY_PREFIX = 0xAA
COMMAND_PREFIX = 0x33


def build_query_packet(query_code: int) -> bytes:
    """Build a common-format query packet with zero padding and XOR checksum.

    Query-code meanings are model-specific; this function does not validate
    whether the target device supports a given code.
    """
    if not 0 <= query_code <= 0xFF:
        raise ValueError("query_code must be between 0 and 255")

    packet = bytearray([QUERY_PREFIX, query_code, *([0] * 17), 0])
    packet[-1] = _xor_checksum(packet[:-1])
    return bytes(packet)


def build_command_packet(command_code: int, payload: bytes = b"") -> bytes:
    """Build a common ``0x33`` command frame; payload semantics are model-specific."""
    return build_packet(COMMAND_PREFIX, command_code, payload)


def build_packet(prefix: int, command_code: int, payload: bytes = b"") -> bytes:
    """Build a padded Govee-style frame with a caller-selected prefix.

    This only constructs the 20-byte/XOR framing. Command meanings remain
    device-specific; for example, the H7126 mode command uses prefix ``0x3A``.
    """
    if not 0 <= prefix <= 0xFF:
        raise ValueError("prefix must be between 0 and 255")
    if not 0 <= command_code <= 0xFF:
        raise ValueError("command_code must be between 0 and 255")
    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    if len(payload) > 17:
        raise ValueError("payload cannot exceed 17 bytes")

    packet = bytearray([prefix, command_code, *payload])
    packet.extend([0] * (19 - len(packet)))
    packet.append(_xor_checksum(packet))
    return bytes(packet)


@dataclass(frozen=True, slots=True)
class GoveeNotification:
    """Parsed common-format notification with its model-specific data intact."""

    code: int
    payload: bytes
    raw: bytes

    @property
    def power_state(self) -> bool | None:
        """Return the common ``0xAA 0x01`` power value when recognizable.

        The remaining payload bytes are preserved because their meaning can
        differ between device families.
        """
        if self.code != 0x01 or not self.payload or self.payload[0] not in (0, 1):
            return None
        return self.payload[0] == 1


def parse_notification(data: bytes) -> GoveeNotification:
    """Parse a common Govee notification and verify its XOR checksum."""
    if len(data) < 3:
        raise ValueError("notification is too short")
    if data[0] != QUERY_PREFIX:
        raise ValueError("notification has an unexpected prefix")
    if _xor_checksum(data) != 0:
        raise ValueError("notification checksum is invalid")
    return GoveeNotification(code=data[1], payload=data[2:-1], raw=data)


def _xor_checksum(data: bytes | bytearray) -> int:
    checksum = 0
    for value in data:
        checksum ^= value
    return checksum
