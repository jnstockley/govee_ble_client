"""Legacy AES/RC4 application encryption used by some Govee BLE devices.

This is a compatibility implementation for Govee's reverse-engineered V1
transport. It is not suitable as a general-purpose security protocol: the
application key is public, RC4 is obsolete, and packet integrity is only XOR.
"""

from __future__ import annotations

import asyncio
import secrets
from collections import deque
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.decrepit.ciphers.algorithms import ARC4
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

if TYPE_CHECKING:
    from .device import GattConnection

FRAME_LENGTH = 20
_GOVEE_V1_PUBLIC_KEY = b"MakingLifeSmarte"


def xor_checksum(data: bytes) -> int:
    """Return the XOR checksum used by 20-byte Govee application frames."""
    checksum = 0
    for value in data:
        checksum ^= value
    return checksum


def build_encryption_handshake_frame(opcode: int) -> bytes:
    """Build a V1 E7 handshake packet with random padding and XOR checksum."""
    if opcode not in (0x01, 0x02):
        raise ValueError("V1 handshake opcode must be 0x01 or 0x02")
    frame = bytearray((0xE7, opcode))
    frame.extend(secrets.token_bytes(17))
    frame.append(xor_checksum(frame))
    return bytes(frame)


def _crypt_frame(frame: bytes, key: bytes, *, decrypt: bool) -> bytes:
    if len(frame) != FRAME_LENGTH:
        raise ValueError(f"encrypted Govee frames must be {FRAME_LENGTH} bytes")
    if len(key) != 16:
        raise ValueError("AES/RC4 V1 keys must be exactly 16 bytes")

    aes = Cipher(algorithms.AES(key), modes.ECB())
    aes_context = aes.decryptor() if decrypt else aes.encryptor()
    block = aes_context.update(frame[:16]) + aes_context.finalize()

    # Govee runs a fresh RC4 stream for each packet's four-byte tail.
    rc4 = Cipher(ARC4(key), mode=None).decryptor()
    tail = rc4.update(frame[16:])
    return block + tail


class GoveeAesRc4V1:
    """Cipher state and GATT transport for Govee's V1 encrypted protocol."""

    def __init__(
        self,
        connection: GattConnection,
        *,
        write_uuid: str,
        notify_uuid: str,
    ) -> None:
        self._connection = connection
        self._write_uuid = write_uuid
        self._notify_uuid = notify_uuid
        self._session_key: bytes | None = None
        self._raw_notifications: asyncio.Queue[bytes] = asyncio.Queue()
        self._pending: deque[bytes] = deque()
        self._notifications_started = False

    @property
    def established(self) -> bool:
        """Whether the device accepted the V1 session-key handshake."""
        return self._session_key is not None

    async def connect(self, *, timeout: float = 5.0) -> None:
        """Subscribe and perform the observed E7/01 + E7/02 handshake."""
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if self._notifications_started:
            raise RuntimeError("encryption handshake has already been started")

        def on_notification(_sender: Any, data: bytearray) -> None:
            """Copy callback data before Bleak can reuse its buffer."""
            self._raw_notifications.put_nowait(bytes(data))

        await self._connection.start_notify(self._notify_uuid, on_notification)
        self._notifications_started = True

        request = build_encryption_handshake_frame(0x01)
        await self._write_with_public_key(request)
        response = await self._wait_for_handshake(0x01, timeout=timeout)
        session_key = response[2:18]

        confirmation = build_encryption_handshake_frame(0x02)
        await self._write_with_public_key(confirmation)
        await self._wait_for_handshake(0x02, timeout=timeout)
        self._session_key = session_key

    async def write(self, frame: bytes) -> None:
        """Encrypt and send one session frame to the device."""
        key = self._session_key
        if key is None:
            raise RuntimeError("V1 encryption handshake has not completed")
        if len(frame) != FRAME_LENGTH or xor_checksum(frame) != 0:
            raise ValueError("frame must be a valid 20-byte XOR-checksummed packet")
        await self._connection.write(
            self._write_uuid, _crypt_frame(frame, key, decrypt=False), response=False
        )

    async def receive(self, *, timeout: float | None = None) -> bytes:
        """Return the next valid decrypted frame, trying session and handshake keys."""
        if self._pending:
            return self._pending.popleft()
        if not self._notifications_started:
            raise RuntimeError("notifications have not been started")

        raw = await asyncio.wait_for(self._raw_notifications.get(), timeout=timeout)
        if len(raw) != FRAME_LENGTH:
            raise ValueError("notification is not a 20-byte Govee application frame")
        keys = [self._session_key, _GOVEE_V1_PUBLIC_KEY]
        for key in keys:
            if key is None:
                continue
            decoded = _crypt_frame(raw, key, decrypt=True)
            if xor_checksum(decoded) == 0:
                return decoded
        raise ValueError("notification did not match a valid V1 encrypted frame")

    async def close(self) -> None:
        """Stop notifications for this encrypted session."""
        try:
            if self._notifications_started:
                self._notifications_started = False
                if self._connection.is_connected:
                    await self._connection.stop_notify(self._notify_uuid)
        finally:
            self._session_key = None
            self._pending.clear()
            while not self._raw_notifications.empty():
                try:
                    self._raw_notifications.get_nowait()
                except asyncio.QueueEmpty:
                    break

    async def _write_with_public_key(self, frame: bytes) -> None:
        encrypted = _crypt_frame(frame, _GOVEE_V1_PUBLIC_KEY, decrypt=False)
        await self._connection.write(self._write_uuid, encrypted, response=False)

    async def _wait_for_handshake(self, opcode: int, *, timeout: float) -> bytes:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(
                    f"timed out waiting for E7/{opcode:02x} handshake reply"
                )
            raw = await asyncio.wait_for(
                self._raw_notifications.get(), timeout=remaining
            )
            if len(raw) != FRAME_LENGTH:
                continue
            decoded = _crypt_frame(raw, _GOVEE_V1_PUBLIC_KEY, decrypt=True)
            if xor_checksum(decoded) != 0:
                continue
            if decoded[:2] == bytes((0xE7, opcode)):
                return decoded
            # Some firmware reports state while the handshake is finishing.
            if decoded[0] in (0xAA, 0xEE, 0x33, 0x3A):
                self._pending.append(decoded)
