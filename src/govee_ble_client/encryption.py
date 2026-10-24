"""Legacy AES/RC4 application encryption used by some Govee BLE devices.

This is a compatibility implementation for Govee's reverse-engineered V1
transport. It is not suitable as a general-purpose security protocol: the
application key is public, RC4 is obsolete, and packet integrity is only XOR.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections import deque
from collections.abc import Callable
from math import isfinite
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.decrepit.ciphers.algorithms import ARC4
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

if TYPE_CHECKING:
    from .device import GattConnection

from ._logging import connection_logger, log_operation
from .exceptions import DeviceNotConnectedError, NotificationBufferOverflowError

_LOGGER = logging.getLogger(__name__)

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
    """Raw V1 encrypted frames over a caller-owned, connected GATT handle.

    Prefer ``H7102Client`` for typed fan operations. This transport owns its
    notification subscription and key material, not the GATT link. The positive
    integer ``notification_buffer_size`` (default 256) bounds combined raw
    notifications and frames retained during setup. Overflow fails the session
    with ``NotificationBufferOverflowError`` instead of silently dropping data.
    Run one receiver after setup and explicitly ``close()`` before releasing
    GATT; the transport itself is not an async context manager.
    """

    def __init__(
        self,
        connection: GattConnection,
        *,
        write_uuid: str,
        notify_uuid: str,
        notification_buffer_size: int = 256,
    ) -> None:
        if (
            isinstance(notification_buffer_size, bool)
            or not isinstance(notification_buffer_size, int)
            or notification_buffer_size <= 0
        ):
            raise ValueError("notification_buffer_size must be a positive integer")
        self._connection = connection
        self._logger = connection_logger(_LOGGER, connection)
        self._write_uuid = write_uuid
        self._notify_uuid = notify_uuid
        self._capacity = notification_buffer_size
        self._session_key: bytes | None = None
        self._raw_notifications: asyncio.Queue[bytes] = asyncio.Queue(
            maxsize=notification_buffer_size
        )
        self._pending: deque[bytes] = deque()
        self._available = asyncio.Event()
        self._notifications_started = False
        self._cleanup_pending = False
        self._connecting = False
        self._receiving = False
        self._lifecycle = asyncio.Lock()
        self._generation: object | None = None
        self._epoch = 0
        self._failure: Exception | None = None
        self._unsubscribe: Callable[[], None] | None = None

    @property
    def established(self) -> bool:
        """Whether a key belongs to an active, healthy GATT session."""
        return (
            self._session_key is not None
            and self._failure is None
            and self._connection.is_connected
        )

    def _fail(self, error: Exception) -> None:
        self._failure = error
        self._session_key = None
        self._generation = None
        self._epoch += 1
        self._pending.clear()
        while not self._raw_notifications.empty():
            self._raw_notifications.get_nowait()
        self._available.set()

    async def connect(self, *, timeout: float = 5.0) -> None:
        """Subscribe and complete both handshake stages within one deadline.

        ``timeout`` is finite positive seconds, starting after lifecycle
        ownership is acquired. It covers subscription, writes, and both replies.
        Cleanup has a separate five-second stop-notify budget. Failed/cancelled
        setup invalidates key material and attempts to unsubscribe. If cleanup
        also fails, the initiating error is preserved; retry ``close()`` before
        reuse. Repeated or concurrent setup raises ``RuntimeError``.
        """
        if not isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a finite positive number")
        if self._connecting or self._notifications_started or self._cleanup_pending:
            raise RuntimeError("encryption handshake has already been started")
        with log_operation(self._logger, "V1 handshake", timeout):
            self._connecting = True
            try:
                async with self._lifecycle:
                    self._failure = None
                    self._epoch += 1
                    epoch = self._epoch
                    generation = self._generation = object()
                    self._available.clear()

                    def on_notification(_sender: Any, data: bytearray) -> None:
                        if (
                            self._generation is not generation
                            or self._failure is not None
                        ):
                            return
                        if (
                            self._raw_notifications.qsize() + len(self._pending)
                            >= self._capacity
                        ):
                            self._logger.debug(
                                "Notification buffer overflow capacity=%d; reconnect required",
                                self._capacity,
                            )
                            self._fail(
                                NotificationBufferOverflowError(
                                    "notification buffer overflow; reconnect required"
                                )
                            )
                            return
                        self._raw_notifications.put_nowait(bytes(data))
                        self._available.set()

                    subscribe = getattr(
                        self._connection, "add_disconnect_callback", None
                    )
                    if subscribe is not None:
                        self._unsubscribe = subscribe(
                            lambda: self._fail(
                                DeviceNotConnectedError("GATT link disconnected")
                            )
                        )
                    try:
                        async with asyncio.timeout(timeout):
                            # Set before awaiting: cancellation can occur after the
                            # backend installed a subscription but before returning.
                            self._notifications_started = True
                            self._cleanup_pending = True
                            await self._connection.start_notify(
                                self._notify_uuid, on_notification
                            )
                            self._check_session(epoch)
                            self._logger.debug("V1 handshake stage=01 sending request")
                            await self._write_with_public_key(
                                build_encryption_handshake_frame(0x01)
                            )
                            response = await self._wait_for_handshake(
                                0x01, timeout=timeout
                            )
                            session_key = response[2:18]
                            self._logger.debug("V1 handshake stage=02 sending request")
                            await self._write_with_public_key(
                                build_encryption_handshake_frame(0x02)
                            )
                            await self._wait_for_handshake(0x02, timeout=timeout)
                            self._check_session(epoch)
                            self._session_key = session_key
                    except BaseException:
                        try:
                            await self._close_locked()
                        except Exception as error:  # noqa: BLE001
                            self._logger.debug(
                                "Handshake cleanup failed error=%s",
                                type(error).__name__,
                            )
                        raise
            finally:
                self._connecting = False

    async def write(self, frame: bytes) -> None:
        """Encrypt a valid 20-byte XOR-checksummed plaintext frame and write once.

        A healthy established session is required. Return ``None`` after the
        GATT write; no reply or state confirmation is awaited. Delivery may be
        uncertain on cancellation, and application writes are never replayed.
        """
        epoch = self._epoch
        async with self._lifecycle:
            key = self._session_key
            if not self.established or key is None:
                raise RuntimeError("V1 encryption handshake has not completed")
            self._check_session(epoch)
            if len(frame) != FRAME_LENGTH or xor_checksum(frame) != 0:
                raise ValueError("frame must be a valid 20-byte XOR-checksummed packet")
            await self._connection.write(
                self._write_uuid,
                _crypt_frame(frame, key, decrypt=False),
                response=False,
            )

    def _check_session(self, epoch: int) -> None:
        if self._failure is not None:
            raise self._failure
        if epoch != self._epoch:
            raise RuntimeError("encrypted session closed or replaced")

    async def _next_raw(self, epoch: int) -> bytes:
        while True:
            self._check_session(epoch)
            if not self._raw_notifications.empty():
                return self._raw_notifications.get_nowait()
            self._available.clear()
            await self._available.wait()

    async def receive(self, *, timeout: float | None = None) -> bytes:
        """Return the next decrypted frame; do not receive during setup.

        Use exactly one consumer. ``timeout=None`` waits without a deadline;
        otherwise supply finite positive seconds. ``TimeoutError`` alone does
        not close the session. Malformed frames raise ``ValueError``. Close,
        link loss, and overflow wake waiters with a lifecycle/connection/buffer
        error. Closing or replacing a session prevents old buffered data from
        entering the new one. No model-specific state is decoded here.
        """
        if self._connecting:
            raise RuntimeError("encryption handshake is in progress")
        if self._receiving:
            raise RuntimeError("another receiver owns this notification stream")
        self._check_session(self._epoch)
        if not self._notifications_started:
            raise RuntimeError("notifications have not been started")
        if timeout is not None and (not isfinite(timeout) or timeout <= 0):
            raise ValueError("timeout must be a finite positive number or None")
        with log_operation(self._logger, "Encrypted receive", timeout):
            self._receiving = True
            epoch = self._epoch
            try:
                if self._pending:
                    return self._pending.popleft()
                # asyncio.timeout avoids creating a task for every received frame.
                async with asyncio.timeout(timeout):
                    raw = await self._next_raw(epoch)
                if len(raw) != FRAME_LENGTH:
                    raise ValueError(
                        "notification is not a 20-byte Govee application frame"
                    )
                for key in (self._session_key, _GOVEE_V1_PUBLIC_KEY):
                    if key is not None:
                        decoded = _crypt_frame(raw, key, decrypt=True)
                        if xor_checksum(decoded) == 0:
                            return decoded
                raise ValueError(
                    "notification did not match a valid V1 encrypted frame"
                )
            finally:
                self._receiving = False

    async def close(self) -> None:
        """Invalidate, wake receivers, wait for writes, and stop notifications.

        Leave GATT connected for its owner. In-flight writes may complete; no
        new session write is accepted after invalidation. Stop-notify cleanup
        has a five-second budget and can raise. Retry this method if teardown
        fails; repeated close after successful cleanup is safe.
        """
        with log_operation(self._logger, "Encrypted close", 5.0):
            self._fail(RuntimeError("encrypted session closed"))
            async with self._lifecycle:
                await self._close_locked()

    async def _close_locked(self) -> None:
        self._fail(RuntimeError("encrypted session closed"))
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        started = self._notifications_started or self._cleanup_pending
        self._notifications_started = False
        if started and self._connection.is_connected:
            async with asyncio.timeout(5.0):
                await self._connection.stop_notify(self._notify_uuid)
        self._cleanup_pending = False

    async def _write_with_public_key(self, frame: bytes) -> None:
        encrypted = _crypt_frame(frame, _GOVEE_V1_PUBLIC_KEY, decrypt=False)
        await self._connection.write(self._write_uuid, encrypted, response=False)

    async def _wait_for_handshake(self, opcode: int, *, timeout: float) -> bytes:
        epoch = self._epoch
        self._logger.debug("V1 handshake stage=%02x waiting for reply", opcode)
        try:
            async with asyncio.timeout(timeout):
                while True:
                    raw = await self._next_raw(epoch)
                    if len(raw) != FRAME_LENGTH:
                        continue
                    decoded = _crypt_frame(raw, _GOVEE_V1_PUBLIC_KEY, decrypt=True)
                    if xor_checksum(decoded) != 0:
                        continue
                    if decoded[:2] == bytes((0xE7, opcode)):
                        self._logger.debug(
                            "V1 handshake stage=%02x reply accepted", opcode
                        )
                        return decoded
                    if decoded[0] in (0xAA, 0xEE, 0x33, 0x3A):
                        self._pending.append(decoded)
        except TimeoutError as error:
            raise TimeoutError(
                f"timed out waiting for E7/{opcode:02x} handshake reply"
            ) from error
