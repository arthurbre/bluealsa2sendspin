"""End-to-end check against a real aiosendspin server.

Exercises the full lifecycle bluealsa2sendspin relies on: static-PIN pairing,
the server activating the source role, our ``line_sense`` signal reaching it,
a server-driven start/stop cycle, and the PCM bytes round-tripping intact.
BlueALSA itself is faked (`FakeBlueAlsa`) since no real hardware is available
in CI; everything downstream of that fake is real aiosendspin protocol code.
"""

from __future__ import annotations

import asyncio
import dataclasses
import math
import struct
from argparse import Namespace
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer
from aiosendspin.client import SendspinClient
from aiosendspin.models.types import PairMethod, Roles
from aiosendspin.noise.keys import Identity
from aiosendspin.noise.pairing import PairingAttempt
from aiosendspin.noise.trust_store import InMemoryServerPairingStore
from aiosendspin.server.roles.source.events import (
    SourceSignalChangedEvent,
    SourceStreamEndedEvent,
    SourceStreamStartedEvent,
)
from aiosendspin.server.server import SendspinServer

from bluealsa2sendspin import cli
from bluealsa2sendspin.bluealsa import PcmFormat, PcmInfo
from bluealsa2sendspin.bridge import SourceBridge
from bluealsa2sendspin.config import load_or_create_identity, open_pairing_store

KNOWN_PIN = "12345678"


class FakeOpenPcm:
    def __init__(self, info: PcmInfo, reader: asyncio.StreamReader) -> None:
        self.info = info
        self.running = info.running
        self.reader = reader
        self._callbacks: list[Callable[[bool], None]] = []

    def on_running_changed(self, callback: Callable[[bool], None]) -> None:
        self._callbacks.append(callback)

    def close(self) -> None:
        pass


class FakeBlueAlsa:
    def __init__(self, info: PcmInfo, reader: asyncio.StreamReader) -> None:
        self.pcm = FakeOpenPcm(info, reader)

    async def find_source_pcm(self) -> PcmInfo:
        return self.pcm.info

    async def watch_topology_changes(self, on_change: Callable[[], None]) -> None:
        pass

    async def open(self, info: PcmInfo) -> FakeOpenPcm:
        return self.pcm


def sine_pcm_16bit(n_samples: int, channels: int = 2) -> bytes:
    out = bytearray()
    for i in range(n_samples):
        sample = int(3000 * math.sin(2 * math.pi * 440 * i / 48000))
        out += struct.pack("<h", sample) * channels
    return bytes(out)


async def wait_for(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(5.0):
        # Polls arbitrary caller-supplied predicates (list contents, object
        # attributes) rather than a single flag, so there's no one event to
        # await instead.
        while not condition():  # noqa: ASYNC110
            await asyncio.sleep(0.01)


@pytest_asyncio.fixture
async def sendspin_server() -> AsyncIterator[tuple[SendspinServer, str]]:
    server = SendspinServer(
        loop=asyncio.get_running_loop(),
        identity=Identity.generate(),
        server_name="test-server",
        pairing_store=InMemoryServerPairingStore(),
    )
    app = web.Application()
    app.router.add_get(SendspinServer.API_PATH, server.on_client_connect)
    test_server = TestServer(app)
    await test_server.start_server()
    try:
        yield server, f"ws://127.0.0.1:{test_server.port}{SendspinServer.API_PATH}"
    finally:
        await test_server.close()
        await server.close()


async def _pair_over(url: str, state_dir: Path) -> Identity:
    identity = load_or_create_identity(state_dir)
    store = await open_pairing_store(state_dir)
    config = await store.get_pairing_config()
    await store.store_pairing_config(dataclasses.replace(config, static_pin_enabled=True))
    await store.set_static_pin(KNOWN_PIN)
    return identity


async def test_pair_then_stream_end_to_end(
    tmp_path: Path, sendspin_server: tuple[SendspinServer, str]
) -> None:
    server, url = sendspin_server
    identity = await _pair_over(url, tmp_path)

    pair_args = Namespace(state_dir=tmp_path, server_url=url, client_name="bluealsa2sendspin-test")
    pair_task = asyncio.create_task(cli._pair(pair_args))
    await wait_for(lambda: server.get_client(identity.peer_id) is not None)

    async def provide_pin() -> str:
        return KNOWN_PIN

    await server.initiate_pairing(
        identity.peer_id, PairingAttempt(method=PairMethod.STATIC_PIN, pin_provider=provide_pin)
    )
    await asyncio.wait_for(pair_task, timeout=10)

    # Reload the store fresh, as a separate `run` process invocation would,
    # rather than reusing the pre-pairing in-memory store above.
    store = await open_pairing_store(tmp_path)
    client = SendspinClient(
        identity,
        "bluealsa2sendspin-test",
        [Roles.SOURCE],
        pairing_store=store,
        source_support=cli._source_support(),
    )

    pcm_format = PcmFormat(
        sample_rate=48000, channels=2, bit_depth=16, signed=True, big_endian=False, byte_width=2
    )
    info = PcmInfo(
        object_path="/org/bluealsa/hci0/dev_AA/a2dpsnk/source",
        device_path="/org/bluez/hci0/dev_AA",
        pcm_format=pcm_format,
        running=True,
    )
    reader = asyncio.StreamReader()
    bridge = SourceBridge(client, FakeBlueAlsa(info, reader))
    await bridge.start()

    try:
        await client.connect(url)
        assert client.connected
        await wait_for(client.is_time_synchronized)
        await bridge.on_connected()

        server_client = server.get_client(identity.peer_id)
        assert server_client is not None
        assert "source@v1" in server_client.active_role_ids

        events: list[Any] = []
        server_client.add_event_listener(lambda _c, e: events.append(e))

        await wait_for(lambda: any(isinstance(e, SourceSignalChangedEvent) for e in events))
        signal_event = next(e for e in events if isinstance(e, SourceSignalChangedEvent))
        assert signal_event.signal.value == "present"

        source_role = server_client.role("source@v1")
        assert source_role is not None
        source_role.request_start()

        await wait_for(lambda: any(isinstance(e, SourceStreamStartedEvent) for e in events))
        handle = next(e for e in events if isinstance(e, SourceStreamStartedEvent)).handle

        pcm = sine_pcm_16bit(4800)  # 100ms @ 48kHz stereo
        drained = bytearray()

        async def _drain() -> None:
            async for chunk, _timestamp in handle:
                drained.extend(chunk)
                if len(drained) >= len(pcm):
                    return

        drain_task = asyncio.create_task(_drain())
        reader.feed_data(pcm)
        await asyncio.wait_for(drain_task, timeout=5)
        assert bytes(drained) == pcm

        source_role.request_stop()
        await wait_for(lambda: any(isinstance(e, SourceStreamEndedEvent) for e in events))
    finally:
        if client.connected:
            await client.disconnect()
