"""Offline checks for controlled comparisons and disconnect recovery."""

import asyncio
from copy import deepcopy

import pytest

from examples import investigate_h7102_oscillation as probe
from govee_ble import build_packet
from govee_ble.exceptions import DeviceNotConnectedError


def make_report():
    target = next(
        q for q in probe.QUERIES if q.payload == b"\x01" and q.command == 0x1F
    )
    rounds = []
    for state in probe.ROUND_STATES:
        samples = []
        for _ in range(3):
            sample = []
            for query in probe.QUERIES:
                response = (
                    build_packet(0xAA, 0x1F, bytes((1, int(state == "on"))))
                    if query == target
                    else query.frame
                )
                sample.append(
                    {
                        "query": query.name,
                        "tx": query.frame.hex(),
                        "rx": [response.hex()],
                    }
                )
            samples.append(sample)
        rounds.append({"state": state, "samples": samples})
    return {"rounds": rounds, "samples_per_round": 3}, target


def test_off_reply_identical_to_query_can_still_correlate_with_on():
    report, target = make_report()
    candidates = probe.find_candidates(report)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["query"] == target.name
    assert candidate["byte_offset_zero_based"] == 3
    assert candidate["off"] == "00"
    assert candidate["on"] == "01"
    assert candidate["off_all_query_identical"] is True


def test_echo_only_transient_and_incomplete_reports_prove_no_candidate():
    report, target = make_report()
    echo = deepcopy(report)
    for stage in echo["rounds"]:
        for sample in stage["samples"]:
            for entry in sample:
                entry["rx"] = [entry["tx"]]
    assert probe.find_candidates(echo) == []

    transient = deepcopy(report)
    for entry in transient["rounds"][1]["samples"][0]:
        if entry["query"] == target.name:
            entry["rx"] = [target.frame.hex()]
    assert probe.find_candidates(transient) == []
    assert probe.find_candidates({**report, "rounds": report["rounds"][:2]}) == []
    report["rounds"][-1]["samples"][-1].clear()
    assert probe.find_candidates(report) == []


@pytest.mark.parametrize("fail_replacement", [False, True])
def test_disconnect_between_connection_check_and_write_retries_once(
    monkeypatch, fail_replacement
):
    class Connection:
        is_connected = True
        reconnects = 0

        async def reconnect(self):
            self.reconnects += 1
            self.is_connected = True

    class Transport:
        def __init__(self, connection, *, fail):
            self.connection = connection
            self.fail = fail
            self.written = []
            self.delivered = False
            self.closed = False
            self.connected = False

        async def connect(self, *, timeout):
            self.connected = True

        async def close(self):
            self.closed = True
            if self is transports[0] and not self.connection.is_connected:
                raise DeviceNotConnectedError("stale notification subscription")

        async def write(self, frame):
            if self.fail:
                self.connection.is_connected = False
                raise DeviceNotConnectedError("link dropped before write")
            self.written.append(frame)

        async def receive(self, *, timeout):
            if not self.written or self.delivered:
                raise TimeoutError
            self.delivered = True
            return build_packet(0xAA, 0x1F, b"\x01\x01")

    connection = Connection()
    transports = []

    def factory(connection, **kwargs):
        fail = not transports or fail_replacement
        transport = Transport(connection, fail=fail)
        transports.append(transport)
        return transport

    monkeypatch.setattr(probe, "GoveeAesRc4V1", factory)
    monkeypatch.setattr(probe.PROFILE, "validate_connection", lambda _: None)
    query = next(q for q in probe.QUERIES if q.payload == b"\x01" and q.command == 0x1F)

    async def check():
        session = probe.ProbeSession(connection)
        await session.start()
        if fail_replacement:
            with pytest.raises(DeviceNotConnectedError):
                await session.capture(query, timeout=0.1)
        else:
            entry = await session.capture(query, timeout=0.1)
            assert entry["session"] == 1
            assert entry["rx"] == [build_packet(0xAA, 0x1F, b"\x01\x01").hex()]
            assert transports[1].written == [query.frame]
        assert connection.reconnects == session.reconnects == 1
        assert transports[0].closed
        assert transports[1].connected
        await session.close()

    asyncio.run(check())
