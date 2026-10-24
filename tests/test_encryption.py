"""V1 crypto vectors and a synthetic peripheral; no Bluetooth backend."""

from functools import reduce
from operator import xor

import pytest

from govee_ble_client import build_packet
from govee_ble_client.encryption import (
    _GOVEE_V1_PUBLIC_KEY,
    GoveeAesRc4V1,
    _crypt_frame,
    build_encryption_handshake_frame,
    xor_checksum,
)

KEY = bytes(range(1, 17))  # Synthetic test session key, not device material.


class Peripheral:
    """Respond to encrypted writes by invoking an in-memory notification callback."""

    def __init__(self, *, noise=False, confirm=True):
        self.is_connected = True
        self.noise = noise
        self.confirm = confirm
        self.callback = None
        self.handshakes = []
        self.app_writes = []
        self.stopped = []

    async def start_notify(self, uuid, callback):
        self.callback = callback

    async def stop_notify(self, uuid):
        self.stopped.append(uuid)

    def emit(self, frame, key=_GOVEE_V1_PUBLIC_KEY):
        self.callback(None, bytearray(_crypt_frame(frame, key, decrypt=False)))

    async def write(self, uuid, data, *, response):
        assert uuid == "write" and response is False
        if len(self.handshakes) < 2:
            frame = _crypt_frame(data, _GOVEE_V1_PUBLIC_KEY, decrypt=True)
            self.handshakes.append(frame)
            assert frame[:2] == bytes((0xE7, len(self.handshakes)))
            assert xor_checksum(frame) == 0
            if frame[1] == 1:
                if self.noise:
                    self.callback(None, bytearray(b"short"))
                    self.emit(build_packet(0xFF, 1))
                    invalid = bytearray(build_packet(0xE7, 1))
                    invalid[-1] ^= 1
                    self.emit(bytes(invalid))
                    self.emit(build_packet(0xAA, 1, b"\x01"))
                self.emit(build_packet(0xE7, 1, KEY + b"\x00"))
            elif self.confirm:
                self.emit(build_packet(0xE7, 2))
        else:
            self.app_writes.append(_crypt_frame(data, KEY, decrypt=True))


def make_transport(peripheral=None):
    peripheral = peripheral or Peripheral()
    return GoveeAesRc4V1(
        peripheral, write_uuid="write", notify_uuid="notify"
    ), peripheral


def test_aes_first_block_matches_nist_vector():
    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    block = bytes.fromhex("00112233445566778899aabbccddeeff")
    frame = block + bytes(4)
    encrypted = _crypt_frame(frame, key, decrypt=False)
    assert encrypted[:16].hex() == "69c4e0d86a7b0430d8cdb78070b4c55a"
    assert _crypt_frame(encrypted, key, decrypt=True) == frame


def test_rc4_tail_matches_rfc6229_128bit_key_vector_and_restarts_per_packet():
    # RFC 6229: key 01..10, keystream starts 9a c7 cc 9a 60 9d ...
    first = _crypt_frame(bytes(20), KEY, decrypt=False)
    second = _crypt_frame(bytes((1,)) * 16 + bytes(4), KEY, decrypt=False)
    assert first[16:].hex() == second[16:].hex() == "9ac7cc9a"


@pytest.mark.parametrize(
    ("length", "key_length"), [(0, 16), (19, 16), (21, 16), (20, 0), (20, 15), (20, 17)]
)
def test_crypto_rejects_wrong_sizes(length, key_length):
    with pytest.raises(ValueError):
        _crypt_frame(bytes(length), bytes(key_length), decrypt=False)


@pytest.mark.parametrize("opcode", [1, 2])
def test_handshake_framing_and_random_padding(monkeypatch, opcode):
    monkeypatch.setattr(
        "govee_ble_client.encryption.secrets.token_bytes", lambda n: bytes(range(n))
    )
    frame = build_encryption_handshake_frame(opcode)
    assert len(frame) == 20
    assert frame[:2] == bytes((0xE7, opcode))
    assert frame[2:19] == bytes(range(17))
    assert reduce(xor, frame, 0) == xor_checksum(frame) == 0


@pytest.mark.parametrize("opcode", [0, 3, 255])
def test_handshake_rejects_unknown_opcodes(opcode):
    with pytest.raises(ValueError):
        build_encryption_handshake_frame(opcode)


def test_full_handshake_and_application_traffic(run_async):
    async def check():
        transport, peripheral = make_transport()
        assert not transport.established
        await transport.connect()
        assert transport.established
        frame = build_packet(0xAA, 0x1F, b"\x01")
        await transport.write(frame)
        assert peripheral.app_writes == [frame]
        reply = build_packet(0xAA, 0x1F, b"\x01\x01")
        peripheral.emit(reply, KEY)
        assert await transport.receive(timeout=0.1) == reply
        peripheral.emit(build_packet(0xAA, 0x16, b"\x01"))
        assert (await transport.receive(timeout=0.1))[1] == 0x16
        with pytest.raises(RuntimeError, match="already"):
            await transport.connect()
        await transport.close()
        assert not transport.established
        assert peripheral.stopped == ["notify"]
        await transport.close()
        assert peripheral.stopped == ["notify"]

    run_async(check())


def test_handshake_ignores_noise_and_preserves_application_frames(run_async):
    async def check():
        transport, _ = make_transport(Peripheral(noise=True))
        await transport.connect()
        assert await transport.receive(timeout=0.1) == build_packet(0xAA, 1, b"\x01")
        with pytest.raises(TimeoutError):
            await transport.receive(timeout=0.001)
        await transport.close()

    run_async(check())


def test_confirmation_timeout_does_not_claim_established_session(run_async):
    async def check():
        transport, _ = make_transport(Peripheral(confirm=False))
        try:
            with pytest.raises(TimeoutError):
                await transport.connect(timeout=0.001)
            assert not transport.established
        finally:
            await transport.close()

    run_async(check())


def test_state_and_timeout_guards(run_async):
    async def check():
        transport, _ = make_transport()
        for timeout in (0, -1):
            with pytest.raises(ValueError):
                await transport.connect(timeout=timeout)
        with pytest.raises(RuntimeError):
            await transport.write(build_packet(0xAA, 1))
        with pytest.raises(RuntimeError):
            await transport.receive(timeout=0.001)
        with pytest.raises(TimeoutError, match="handshake"):
            await transport._wait_for_handshake(1, timeout=0)

    run_async(check())


@pytest.mark.parametrize("raw", [b"short", bytes(20)])
def test_bad_notification_lengths_and_ciphertext_are_rejected(raw, run_async):
    async def check():
        transport, peripheral = make_transport()
        await transport.connect()
        peripheral.callback(None, bytearray(raw))
        with pytest.raises(ValueError):
            await transport.receive(timeout=0.1)
        await transport.close()

    run_async(check())


@pytest.mark.parametrize(
    "frame", [b"", bytes(19), bytes.fromhex("aa01000000000000000000000000000000000000")]
)
def test_invalid_app_frame_never_reaches_fake_peripheral(frame, run_async):
    async def check():
        transport, peripheral = make_transport()
        await transport.connect()
        with pytest.raises(ValueError):
            await transport.write(frame)
        assert peripheral.app_writes == []
        await transport.close()

    run_async(check())


@pytest.mark.parametrize("connected", [False, True])
def test_close_discards_all_session_material(connected, run_async):
    async def check():
        transport, peripheral = make_transport()
        await transport.connect()
        peripheral.is_connected = connected
        transport._pending.append(build_packet(0xAA, 1))
        transport._raw_notifications.put_nowait(bytes(20))
        await transport.close()
        assert not transport.established
        assert not transport._pending
        assert transport._raw_notifications.empty()
        assert peripheral.stopped == (["notify"] if connected else [])

    run_async(check())


def test_close_clears_session_even_when_notification_cleanup_raises(run_async):
    async def check():
        transport, peripheral = make_transport()
        await transport.connect()

        async def fail(_uuid):
            raise RuntimeError("notify cleanup failed")

        peripheral.stop_notify = fail
        with pytest.raises(RuntimeError, match="cleanup"):
            await transport.close()
        assert not transport.established
        assert not transport._notifications_started

    run_async(check())


def test_receive_can_decode_public_key_frames_without_session_key(run_async):
    async def check():
        transport, _ = make_transport()
        transport._notifications_started = True
        frame = build_packet(0xAA, 1, b"\x01")
        transport._raw_notifications.put_nowait(
            _crypt_frame(frame, _GOVEE_V1_PUBLIC_KEY, decrypt=False)
        )
        assert await transport.receive(timeout=0.1) == frame
        assert not transport.established
        await transport.close()

    run_async(check())
