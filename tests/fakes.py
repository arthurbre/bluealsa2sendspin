"""Shared test doubles for BlueALSA and Sendspin, used by both the fast unit tests
(``test_bridge_orchestration.py``) and the real-server integration tests
(``test_integration.py``) -- kept in one place so the two don't silently drift apart.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from aiosendspin.models.core import ServerCommandPayload
from aiosendspin.models.player import SupportedAudioFormat
from aiosendspin.models.source import SourceCommandServerPayload
from aiosendspin.models.types import SignalState

from bluealsa2sendspin.bluealsa import PcmInfo


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
        self.present = True
        self._on_topology_change: Callable[[], None] | None = None

    async def find_source_pcm(self) -> PcmInfo | None:
        return self.pcm.info if self.present else None

    async def watch_topology_changes(self, on_change: Callable[[], None]) -> None:
        self._on_topology_change = on_change

    async def open(self, info: PcmInfo) -> FakeOpenPcm:
        return self.pcm

    def simulate_topology_change(self) -> None:
        assert self._on_topology_change is not None, "SourceBridge.start() was never called"
        self._on_topology_change()


class FakeSourceCapture:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self.fed = bytearray()

    async def start(self) -> None:
        self.started = True

    async def feed(self, chunk: bytes) -> None:
        self.fed.extend(chunk)

    async def stop(self) -> None:
        self.stopped = True


class FakeSendspinSource:
    """A ``_SendspinSource`` stand-in: records what ``SourceBridge`` does and lets
    tests drive it from the other side (a server command, a disconnect) directly,
    without a real aiosendspin connection.
    """

    def __init__(self) -> None:
        self.reported_signals: list[SignalState] = []
        self.created_captures: list[FakeSourceCapture] = []
        self._command_listener: Callable[[ServerCommandPayload], None] | None = None
        self._disconnect_listener: Callable[[], None] | None = None

    def add_command_listener(self, callback: Callable[[ServerCommandPayload], None]) -> None:
        self._command_listener = callback

    def add_disconnect_listener(self, callback: Callable[[], None]) -> None:
        self._disconnect_listener = callback

    def create_capture(self, audio_format: SupportedAudioFormat) -> FakeSourceCapture:
        capture = FakeSourceCapture()
        self.created_captures.append(capture)
        return capture

    async def report_signal(self, signal: SignalState) -> bool:
        self.reported_signals.append(signal)
        return True

    def simulate_server_command(self, command: str) -> None:
        assert self._command_listener is not None, "SourceBridge.start() was never called"
        self._command_listener(ServerCommandPayload(source=SourceCommandServerPayload(command=command)))

    def simulate_disconnect(self) -> None:
        assert self._disconnect_listener is not None, "SourceBridge.start() was never called"
        self._disconnect_listener()
