"""The standalone example isolates worker failures and cleans up cancellation."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from examples import watch_multiple_ble as example
from govee_ble_client import H5179Profile, H7126Profile


def test_no_supported_devices_never_connects(monkeypatch, run_async, capsys):
    scanner = SimpleNamespace(
        scan=AsyncMock(
            return_value=[SimpleNamespace(name="other", connect=AsyncMock())]
        )
    )
    monkeypatch.setattr(example, "GoveeScanner", lambda: scanner)
    run_async(example.main())
    assert "No supported" in capsys.readouterr().out


def test_worker_failure_does_not_cancel_another_device(monkeypatch, run_async):
    async def check():
        finished = []
        failed = asyncio.Event()

        async def monitor(device, *, interval):
            if device == "bad":
                failed.set()
                raise RuntimeError("unreachable")
            await failed.wait()
            await asyncio.sleep(0)
            finished.append(device)

        monkeypatch.setattr(example, "monitor", monitor)
        await example.run_devices(["bad", "good"], interval=5)
        assert finished == ["good"]

    run_async(check())


@pytest.mark.parametrize("profile", [H5179Profile(), H7126Profile()])
def test_monitor_sends_only_queries_and_disconnects_on_cancel(profile, run_async):
    async def check():
        writes, cleanup = [], []
        queried = asyncio.Event()
        characteristic = getattr(profile, "io_uuid", None) or profile.write_uuid
        notify = getattr(profile, "io_uuid", None) or profile.notify_uuid
        services = [
            SimpleNamespace(
                uuid=profile.service_uuid,
                characteristics=[
                    SimpleNamespace(
                        uuid=characteristic,
                        properties=["notify", "write-without-response"],
                    ),
                    SimpleNamespace(uuid=notify, properties=["notify"]),
                ]
                if characteristic != notify
                else [
                    SimpleNamespace(
                        uuid=characteristic,
                        properties=["notify", "write-without-response"],
                    )
                ],
            )
        ]

        async def write(_uuid, data, **_options):
            writes.append(data)
            queried.set()

        connection = SimpleNamespace(
            services=services, write=write, start_notify=AsyncMock()
        )

        @asynccontextmanager
        async def connect(**_options):
            try:
                yield connection
            finally:
                cleanup.append(True)

        device = SimpleNamespace(name=profile.model, connect=connect)
        worker = asyncio.create_task(example.monitor(device, interval=5))
        await asyncio.wait_for(queried.wait(), 0.1)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        assert writes and all(data[0] == 0xAA for data in writes)
        assert cleanup == [True]

    run_async(check())
