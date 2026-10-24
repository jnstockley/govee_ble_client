"""Session cancellation, notification ownership, and bounded buffering."""

import asyncio

import pytest
from test_encryption import KEY, Peripheral, make_transport

from govee_ble_client import build_packet
from govee_ble_client.encryption import GoveeAesRc4V1


def test_close_wakes_receiver_without_timeout(run_async):
    async def check():
        transport, _ = make_transport()
        await transport.connect()
        receiver = asyncio.create_task(transport.receive())
        await asyncio.sleep(0)
        await transport.close()
        with pytest.raises(RuntimeError, match="closed"):
            await asyncio.wait_for(receiver, 0.1)

    run_async(check())


def test_link_loss_invalidates_key_and_wakes_receiver(run_async):
    async def check():
        transport, peripheral = make_transport()
        listeners = []
        peripheral.add_disconnect_callback = lambda cb: (
            listeners.append(cb) or (lambda: listeners.remove(cb))
        )
        await transport.connect()
        receiver = asyncio.create_task(transport.receive())
        await asyncio.sleep(0)
        peripheral.is_connected = False
        listeners[0]()
        assert not transport.established
        with pytest.raises(Exception, match="disconnect"):
            await asyncio.wait_for(receiver, 0.1)
        await transport.close()
        assert listeners == []

    run_async(check())


def test_overflow_is_explicit_and_invalidates_session(run_async):
    async def check():
        peripheral = Peripheral()
        transport = GoveeAesRc4V1(
            peripheral,
            write_uuid="write",
            notify_uuid="notify",
            notification_buffer_size=2,
        )
        await transport.connect()
        for _ in range(3):
            peripheral.emit(build_packet(0xAA, 1, b"\x01"), KEY)
        assert not transport.established
        with pytest.raises(BufferError, match="overflow"):
            await transport.receive()
        with pytest.raises(RuntimeError):
            await transport.write(build_packet(0xAA, 1))
        await transport.close()

    run_async(check())


def test_old_callback_cannot_contaminate_reopened_session(run_async):
    async def check():
        transport, peripheral = make_transport()
        await transport.connect()
        old_callback = peripheral.callback
        await transport.close()
        peripheral.handshakes.clear()
        await transport.connect()
        old_callback(None, bytearray(bytes(20)))
        reply = build_packet(0xAA, 1, b"\x01")
        peripheral.emit(reply, KEY)
        assert await transport.receive(timeout=0.1) == reply
        await transport.close()

    run_async(check())


def test_only_one_receiver_can_own_notification_stream(run_async):
    async def check():
        transport, _ = make_transport()
        await transport.connect()
        receiver = asyncio.create_task(transport.receive())
        await asyncio.sleep(0)
        try:
            with pytest.raises(RuntimeError, match="receiv"):
                await transport.receive(timeout=0.01)
        finally:
            receiver.cancel()
            with pytest.raises(asyncio.CancelledError):
                await receiver
            await transport.close()

    run_async(check())


def test_failed_handshake_cleans_itself_and_preserves_initiating_error(run_async):
    async def check():
        transport, peripheral = make_transport(Peripheral(confirm=False))

        async def stop(_uuid):
            peripheral.stopped.append("notify")
            raise RuntimeError("cleanup failed")

        peripheral.stop_notify = stop
        with pytest.raises(TimeoutError):
            await transport.connect(timeout=0.001)
        assert peripheral.stopped == ["notify"]
        assert not transport.established

    run_async(check())


def test_handshake_deadline_includes_subscription_and_excludes_concurrent_handshake(
    run_async,
):
    async def check():
        transport, peripheral = make_transport()
        entered = asyncio.Event()

        async def subscribe(_uuid, callback):
            peripheral.callback = callback
            entered.set()
            await asyncio.Event().wait()

        peripheral.start_notify = subscribe
        first = asyncio.create_task(transport.connect(timeout=0.02))
        await entered.wait()
        with pytest.raises(RuntimeError, match="already"):
            await asyncio.wait_for(transport.connect(timeout=0.001), 0.05)
        with pytest.raises(RuntimeError, match="handshake"):
            await transport.receive(timeout=0.001)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(first, 0.1)
        assert peripheral.stopped == ["notify"]

    run_async(check())


@pytest.mark.parametrize("timeout", [float("nan"), float("inf")])
def test_nonfinite_handshake_timeout_rejected(timeout, run_async):
    transport, _ = make_transport()
    with pytest.raises(ValueError):
        run_async(transport.connect(timeout=timeout))


@pytest.mark.parametrize("size", [0, -1, True, 1.5])
def test_invalid_buffer_capacity_rejected(size):
    with pytest.raises(ValueError):
        GoveeAesRc4V1(
            Peripheral(),
            write_uuid="write",
            notify_uuid="notify",
            notification_buffer_size=size,
        )


def test_close_during_subscription_cannot_establish_session_afterward(run_async):
    async def check():
        transport, peripheral = make_transport()
        entered, release = asyncio.Event(), asyncio.Event()

        async def subscribe(_uuid, callback):
            peripheral.callback = callback
            entered.set()
            await release.wait()

        peripheral.start_notify = subscribe
        setup = asyncio.create_task(transport.connect())
        await entered.wait()
        closing = asyncio.create_task(transport.close())
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(RuntimeError, match="closed"):
            await setup
        await closing
        assert not transport.established
        assert peripheral.stopped == ["notify"]

    run_async(check())


def test_overflow_limit_includes_handshake_pending_frames(run_async):
    async def check():
        peripheral = Peripheral(noise=True)
        transport = GoveeAesRc4V1(
            peripheral,
            write_uuid="write",
            notify_uuid="notify",
            notification_buffer_size=6,
        )
        # The first step retains one application frame. During confirmation,
        # inject enough frames to exceed the combined raw + pending capacity.
        original = peripheral.write

        async def flood(uuid, data, *, response):
            if len(peripheral.handshakes) == 1:
                for _ in range(6):
                    peripheral.emit(build_packet(0xAA, 1))
            await original(uuid, data, response=response)

        peripheral.write = flood
        with pytest.raises(BufferError):
            await transport.connect()
        assert not transport.established
        assert peripheral.stopped == ["notify"]

    run_async(check())


def test_close_waits_for_inflight_write_and_rejects_queued_writes(run_async):
    async def check():
        transport, peripheral = make_transport()
        await transport.connect()
        original = peripheral.write
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(uuid, data, *, response):
            entered.set()
            await release.wait()
            await original(uuid, data, response=response)

        peripheral.write = delayed
        first = asyncio.create_task(transport.write(build_packet(0xAA, 1)))
        await entered.wait()
        closing = asyncio.create_task(transport.close())
        await asyncio.sleep(0)
        try:
            assert not closing.done()
        finally:
            release.set()
            await asyncio.gather(first, closing)
        with pytest.raises(RuntimeError):
            await transport.write(build_packet(0xAA, 2))
        assert len(peripheral.app_writes) == 1

    run_async(check())


def test_failed_notification_teardown_can_be_retried(run_async):
    async def check():
        transport, peripheral = make_transport()
        await transport.connect()
        original = peripheral.stop_notify

        async def fail(_uuid):
            raise RuntimeError("temporary teardown failure")

        peripheral.stop_notify = fail
        with pytest.raises(RuntimeError, match="temporary"):
            await transport.close()
        peripheral.stop_notify = original
        await transport.close()
        assert peripheral.stopped == ["notify"]

    run_async(check())
