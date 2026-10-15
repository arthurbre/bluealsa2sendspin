"""Fast unit tests for SourceBridge's own orchestration logic.

Unlike test_integration.py, nothing here touches a real aiosendspin server or
connection: FakeSendspinSource and FakeBlueAlsa drive SourceBridge exactly the
way the real dependencies would -- through the same listener registrations and
Protocol methods SourceBridge itself calls -- so these run in milliseconds.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import pytest
from aiosendspin.models.types import SignalState
from fakes import FakeBlueAlsa, FakeSendspinSource

from bluealsa2sendspin import bridge as bridge_module
from bluealsa2sendspin.bluealsa import PcmFormat, PcmInfo
from bluealsa2sendspin.bridge import _READ_CHUNK_MS, SourceBridge

PCM_FORMAT = PcmFormat(
    sample_rate=48000, channels=2, bit_depth=16, signed=True, big_endian=False, byte_width=2
)
CHUNK_BYTES = PCM_FORMAT.frame_bytes * (PCM_FORMAT.sample_rate * _READ_CHUNK_MS // 1000)


def _pcm_info(*, running: bool) -> PcmInfo:
    return PcmInfo(
        object_path="/org/bluealsa/hci0/dev_AA/a2dpsnk/source",
        device_path="/org/bluez/hci0/dev_AA",
        pcm_format=PCM_FORMAT,
        running=running,
    )


async def _wait_until(condition: Callable[[], bool]) -> None:
    """Poll a background-task-driven outcome, bounded so a bug hangs the test loudly."""
    async with asyncio.timeout(2.0):
        while not condition():  # noqa: ASYNC110
            await asyncio.sleep(0.001)


async def test_start_syncs_topology_and_reports_initial_signal() -> None:
    fake_bluealsa = FakeBlueAlsa(_pcm_info(running=True), asyncio.StreamReader())
    fake_client = FakeSendspinSource()
    bridge = SourceBridge(fake_client, fake_bluealsa)

    await bridge.start()

    assert fake_client.reported_signals == [SignalState.PRESENT]
    status = bridge.status()
    assert status.reported_signal == SignalState.PRESENT
    assert not status.capturing


async def test_server_start_command_starts_feeding_capture() -> None:
    reader = asyncio.StreamReader()
    fake_bluealsa = FakeBlueAlsa(_pcm_info(running=True), reader)
    fake_client = FakeSendspinSource()
    bridge = SourceBridge(fake_client, fake_bluealsa)
    await bridge.start()

    fake_client.simulate_server_command("start")
    await _wait_until(lambda: bridge.status().capturing)

    assert len(fake_client.created_captures) == 1
    capture = fake_client.created_captures[0]
    assert capture.started

    pcm = bytes(range(256)) * (CHUNK_BYTES // 256 + 1)
    pcm = pcm[:CHUNK_BYTES]
    reader.feed_data(pcm)
    await _wait_until(lambda: len(capture.fed) >= CHUNK_BYTES)
    assert bytes(capture.fed) == pcm


async def test_server_stop_command_stops_capture() -> None:
    fake_bluealsa = FakeBlueAlsa(_pcm_info(running=True), asyncio.StreamReader())
    fake_client = FakeSendspinSource()
    bridge = SourceBridge(fake_client, fake_bluealsa)
    await bridge.start()
    fake_client.simulate_server_command("start")
    await _wait_until(lambda: bridge.status().capturing)
    capture = fake_client.created_captures[0]

    fake_client.simulate_server_command("stop")
    await _wait_until(lambda: not bridge.status().capturing)

    assert capture.stopped


async def test_topology_pcm_removed_tears_down_capture_and_reports_absent() -> None:
    fake_bluealsa = FakeBlueAlsa(_pcm_info(running=True), asyncio.StreamReader())
    fake_client = FakeSendspinSource()
    bridge = SourceBridge(fake_client, fake_bluealsa)
    await bridge.start()
    fake_client.simulate_server_command("start")
    await _wait_until(lambda: bridge.status().capturing)
    capture = fake_client.created_captures[0]
    assert fake_client.reported_signals == [SignalState.PRESENT]

    fake_bluealsa.present = False
    fake_bluealsa.simulate_topology_change()
    await _wait_until(lambda: bridge.status().reported_signal == SignalState.ABSENT)

    assert not bridge.status().capturing
    assert capture.stopped
    assert fake_client.reported_signals[-1] == SignalState.ABSENT


async def test_disconnect_clears_capture_and_signal_state() -> None:
    fake_bluealsa = FakeBlueAlsa(_pcm_info(running=True), asyncio.StreamReader())
    fake_client = FakeSendspinSource()
    bridge = SourceBridge(fake_client, fake_bluealsa)
    await bridge.start()
    fake_client.simulate_server_command("start")
    await _wait_until(lambda: bridge.status().capturing)

    fake_client.simulate_disconnect()

    status = bridge.status()
    assert not status.capturing
    assert status.reported_signal is None


async def test_pcm_replacement_does_not_restart_a_consumed_server_start() -> None:
    fake_bluealsa = FakeBlueAlsa(_pcm_info(running=True), asyncio.StreamReader())
    fake_client = FakeSendspinSource()
    bridge = SourceBridge(fake_client, fake_bluealsa)
    await bridge.start()
    fake_client.simulate_server_command("start")
    await _wait_until(lambda: bridge.status().capturing)

    fake_bluealsa.present = False
    fake_bluealsa.simulate_topology_change()
    await _wait_until(lambda: not bridge.status().capturing)
    fake_bluealsa.present = True
    fake_bluealsa.simulate_topology_change()
    await _wait_until(lambda: fake_client.reported_signals[-1] == SignalState.PRESENT)

    assert not bridge.status().capturing
    assert len(fake_client.created_captures) == 1


async def test_server_start_received_without_pcm_is_honored_once_pcm_appears() -> None:
    fake_bluealsa = FakeBlueAlsa(_pcm_info(running=True), asyncio.StreamReader())
    fake_bluealsa.present = False
    fake_client = FakeSendspinSource()
    bridge = SourceBridge(fake_client, fake_bluealsa)
    await bridge.start()
    fake_client.simulate_server_command("start")
    await asyncio.sleep(0.01)
    assert not bridge.status().capturing

    fake_bluealsa.present = True
    fake_bluealsa.simulate_topology_change()
    await _wait_until(lambda: bridge.status().capturing)

    assert len(fake_client.created_captures) == 1


async def test_disconnect_discards_a_pending_server_start() -> None:
    fake_bluealsa = FakeBlueAlsa(_pcm_info(running=True), asyncio.StreamReader())
    fake_bluealsa.present = False
    fake_client = FakeSendspinSource()
    bridge = SourceBridge(fake_client, fake_bluealsa)
    await bridge.start()
    fake_client.simulate_server_command("start")
    await asyncio.sleep(0.01)

    fake_client.simulate_disconnect()
    fake_bluealsa.present = True
    fake_bluealsa.simulate_topology_change()
    await _wait_until(lambda: bridge.status().reported_signal == SignalState.PRESENT)

    assert not bridge.status().capturing
    assert fake_client.created_captures == []


async def test_stop_is_retried_while_the_connection_refuses_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge_module, "_STOP_RETRY_DELAY_S", 0.0)
    fake_bluealsa = FakeBlueAlsa(_pcm_info(running=True), asyncio.StreamReader())
    fake_client = FakeSendspinSource()
    fake_client.capture_stop_failures = 2
    bridge = SourceBridge(fake_client, fake_bluealsa)
    await bridge.start()
    fake_client.simulate_server_command("start")
    await _wait_until(lambda: bridge.status().capturing)
    capture = fake_client.created_captures[0]

    fake_client.simulate_server_command("stop")
    await _wait_until(lambda: not bridge.status().capturing)

    assert capture.stopped
    assert capture.stop_attempts == 3


async def test_capture_is_released_when_stop_keeps_failing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(bridge_module, "_STOP_RETRY_DELAY_S", 0.0)
    caplog.set_level(logging.WARNING, logger="bluealsa2sendspin.bridge")
    fake_bluealsa = FakeBlueAlsa(_pcm_info(running=True), asyncio.StreamReader())
    fake_client = FakeSendspinSource()
    fake_client.capture_stop_failures = 1000
    bridge = SourceBridge(fake_client, fake_bluealsa)
    await bridge.start()
    fake_client.simulate_server_command("start")
    await _wait_until(lambda: bridge.status().capturing)
    capture = fake_client.created_captures[0]

    fake_client.simulate_server_command("stop")
    await _wait_until(lambda: not bridge.status().capturing)

    assert not capture.stopped
    assert capture.stop_attempts == bridge_module._STOP_ATTEMPTS
    assert any("releasing" in record.message for record in caplog.records)


async def test_refused_capture_start_is_dropped_instead_of_retried() -> None:
    fake_bluealsa = FakeBlueAlsa(_pcm_info(running=True), asyncio.StreamReader())
    fake_client = FakeSendspinSource()
    fake_client.capture_start_fails = True
    bridge = SourceBridge(fake_client, fake_bluealsa)
    await bridge.start()

    fake_client.simulate_server_command("start")
    await _wait_until(lambda: len(fake_client.created_captures) == 1)
    fake_bluealsa.present = False
    fake_bluealsa.simulate_topology_change()
    await _wait_until(lambda: fake_client.reported_signals[-1] == SignalState.ABSENT)
    fake_bluealsa.present = True
    fake_bluealsa.simulate_topology_change()
    await _wait_until(lambda: fake_client.reported_signals[-1] == SignalState.PRESENT)

    assert not bridge.status().capturing
    assert len(fake_client.created_captures) == 1


async def test_fed_chunks_are_timestamped_at_the_start_of_the_chunk() -> None:
    reader = asyncio.StreamReader()
    fake_bluealsa = FakeBlueAlsa(_pcm_info(running=True), reader)
    fake_client = FakeSendspinSource()
    fake_client.now_us_value = 10_000_000
    bridge = SourceBridge(fake_client, fake_bluealsa)
    await bridge.start()
    fake_client.simulate_server_command("start")
    await _wait_until(lambda: bridge.status().capturing)
    capture = fake_client.created_captures[0]

    reader.feed_data(bytes(CHUNK_BYTES))
    await _wait_until(lambda: len(capture.capture_timestamps_us) == 1)

    assert capture.capture_timestamps_us == [10_000_000 - _READ_CHUNK_MS * 1000]
