"""Unit tests for cli.py's reconnect/backoff policy, in isolation from real networking.

``_run_reconnect_loop`` accepts a caller-supplied ``connect`` coroutine factory instead
of constructing a ``SendspinClient`` itself, so these tests drive it with fakes and
real (but tiny) delays -- no server, no real sockets, no mocked clock.
"""

from __future__ import annotations

import asyncio
import logging
from itertools import pairwise

import pytest

from bluealsa2sendspin.cli import _run_reconnect_loop


async def test_reconnect_loop_backs_off_exponentially_and_caps_at_max_delay() -> None:
    stop = asyncio.Event()
    disconnected = asyncio.Event()
    loop = asyncio.get_running_loop()
    attempt_times: list[float] = []

    async def connect() -> None:
        attempt_times.append(loop.time())
        if len(attempt_times) < 5:
            raise RuntimeError("simulated connect failure")

    async def on_connected() -> None:
        stop.set()

    await asyncio.wait_for(
        _run_reconnect_loop(
            connect,
            on_connected,
            stop,
            disconnected,
            label="test-server",
            initial_delay=0.03,
            max_delay=0.09,
        ),
        timeout=2.0,
    )

    assert len(attempt_times) == 5
    gaps = [b - a for a, b in pairwise(attempt_times)]
    expected = [0.03, 0.06, 0.09, 0.09]  # doubles each retry, capped at max_delay
    for gap, exp in zip(gaps, expected, strict=True):
        assert exp * 0.5 <= gap <= exp + 0.15


async def test_reconnect_loop_stop_interrupts_pending_sleep_immediately() -> None:
    stop = asyncio.Event()
    disconnected = asyncio.Event()
    loop = asyncio.get_running_loop()

    async def connect() -> None:
        raise RuntimeError("always fails")

    async def on_connected() -> None:
        pass

    async def stop_soon() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    start = loop.time()
    stopper = asyncio.create_task(stop_soon())
    # initial_delay is deliberately much longer than stop_soon's 0.05s: if `stop`
    # didn't interrupt the pending sleep, this would hit the 1.0s wait_for timeout.
    await asyncio.wait_for(
        _run_reconnect_loop(
            connect,
            on_connected,
            stop,
            disconnected,
            label="test-server",
            initial_delay=5.0,
            max_delay=30.0,
        ),
        timeout=1.0,
    )
    await stopper
    assert 0.05 <= loop.time() - start < 1.0


async def test_reconnect_loop_resets_delay_after_success_and_reconnects_immediately_on_disconnect() -> (  # noqa: E501
    None
):
    stop = asyncio.Event()
    disconnected = asyncio.Event()
    loop = asyncio.get_running_loop()
    attempt_times: list[float] = []
    # 1 fail, 2 fail, 3 succeed (drop -> reconnect), 4 fail, 5 succeed (stop)
    outcomes = ["fail", "fail", "succeed_then_disconnect", "fail", "succeed_then_stop"]

    async def connect() -> None:
        attempt_times.append(loop.time())
        if outcomes[len(attempt_times) - 1].startswith("fail"):
            raise RuntimeError("simulated connect failure")

    async def on_connected() -> None:
        if outcomes[len(attempt_times) - 1] == "succeed_then_disconnect":
            disconnected.set()
        else:
            stop.set()

    await asyncio.wait_for(
        _run_reconnect_loop(
            connect,
            on_connected,
            stop,
            disconnected,
            label="test-server",
            initial_delay=0.05,
            max_delay=1.0,
        ),
        timeout=2.0,
    )

    assert len(attempt_times) == 5
    gap_after_disconnect = attempt_times[3] - attempt_times[2]
    gap_after_reset_failure = attempt_times[4] - attempt_times[3]
    # A disconnect (not a failed connect) triggers an immediate retry, no backoff sleep.
    assert gap_after_disconnect < 0.03
    # Without the reset this would be ~0.10s (the pre-success backoff level); with it,
    # back to initial_delay.
    assert 0.03 <= gap_after_reset_failure <= 0.08


async def test_reconnect_loop_logs_target_label_for_timeout_and_generic_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop = asyncio.Event()
    disconnected = asyncio.Event()
    outcomes = ["timeout", "generic_fail", "succeed"]
    calls = {"n": 0}

    async def connect() -> None:
        outcome = outcomes[calls["n"]]
        calls["n"] += 1
        if outcome == "timeout":
            raise TimeoutError
        if outcome == "generic_fail":
            raise RuntimeError("boom")

    async def on_connected() -> None:
        stop.set()

    caplog.set_level(logging.WARNING, logger="bluealsa2sendspin.cli")

    await asyncio.wait_for(
        _run_reconnect_loop(
            connect,
            on_connected,
            stop,
            disconnected,
            label="ws://test-server",
            initial_delay=0.01,
            max_delay=0.01,
        ),
        timeout=2.0,
    )

    messages = [record.message for record in caplog.records]
    assert any("Timed out connecting to ws://test-server" in m for m in messages)
    assert any("Failed to connect to ws://test-server" in m for m in messages)
