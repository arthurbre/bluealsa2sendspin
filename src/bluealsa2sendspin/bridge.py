"""Bridges a BlueALSA source PCM into a Sendspin SOURCE-role client.

Two things drive this independently, matching how ``sendspin_source``'s
autostart works on the Music Assistant side:

- BlueALSA's ``Running`` property (a phone is actively streaming) is reported
  as the Sendspin ``line_sense`` signal, which the server uses to decide
  whether to autostart/autostop this source.
- The server's own ``server/command`` (``start``/``stop``) is what actually
  starts and stops our ``SourceCapture``.
"""

from __future__ import annotations

import asyncio
import logging

from aiosendspin.client import SendspinClient, SourceCapture
from aiosendspin.models.core import ServerCommandPayload
from aiosendspin.models.player import SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, SignalState

from .bluealsa import BlueAlsaClient, OpenPcm, PcmInfo

logger = logging.getLogger(__name__)

_READ_CHUNK_MS = 20


class SourceBridge:
    """Owns the live BlueALSA PCM <-> Sendspin ``SourceCapture`` wiring for one client."""

    def __init__(self, client: SendspinClient, bluealsa: BlueAlsaClient) -> None:
        self._client = client
        self._bluealsa = bluealsa
        self._open_pcm: OpenPcm | None = None
        self._capture: SourceCapture | None = None
        self._feed_task: asyncio.Task[None] | None = None
        self._stream_requested = False
        self._reported_signal: SignalState | None = None

    async def start(self) -> None:
        """Wire up listeners and reconcile against whatever BlueALSA currently reports."""
        self._client.add_server_command_listener(self._on_server_command)
        await self._bluealsa.watch_topology_changes(self._schedule_topology_sync)
        await self._sync_topology()

    async def on_connected(self) -> None:
        """Re-announce the current signal state on a fresh (re)connection."""
        await self._report_signal()

    def _schedule_topology_sync(self) -> None:
        asyncio.get_running_loop().create_task(self._sync_topology())

    async def _sync_topology(self) -> None:
        info = await self._bluealsa.find_source_pcm()
        current_path = self._open_pcm.info.object_path if self._open_pcm else None
        if info is None:
            if current_path is not None:
                await self._teardown_pcm()
            return
        if info.object_path == current_path:
            return
        if current_path is not None:
            await self._teardown_pcm()
        await self._setup_pcm(info)

    async def _setup_pcm(self, info: PcmInfo) -> None:
        logger.info("BlueALSA source PCM connected: %s", info.device_path)
        self._open_pcm = await self._bluealsa.open(info)
        self._open_pcm.on_running_changed(self._on_running_changed)
        await self._report_signal()
        if self._stream_requested:
            await self._ensure_capture_started()

    async def _teardown_pcm(self) -> None:
        logger.info("BlueALSA source PCM disconnected")
        await self._stop_capture()
        if self._open_pcm is not None:
            self._open_pcm.close()
            self._open_pcm = None
        await self._report_signal()

    def _on_running_changed(self, _running: bool) -> None:
        asyncio.get_running_loop().create_task(self._report_signal())

    async def _report_signal(self) -> None:
        # aiosendspin 9.1.x has no public wrapper for send_source_signal(); see the
        # dependency pin note in pyproject.toml for why this reaches into the
        # client's private admitted connection instead.
        connection = self._client._admitted_connection
        if connection is None:
            return
        signal = (
            SignalState.PRESENT
            if self._open_pcm is not None and self._open_pcm.running
            else SignalState.ABSENT
        )
        if signal == self._reported_signal:
            return
        await connection.send_source_signal(signal)
        self._reported_signal = signal

    def _on_server_command(self, payload: ServerCommandPayload) -> None:
        if payload.source is None:
            return
        asyncio.get_running_loop().create_task(self._handle_server_command(payload.source.command))

    async def _handle_server_command(self, command: str) -> None:
        if command == "start":
            self._stream_requested = True
            await self._ensure_capture_started()
        elif command == "stop":
            self._stream_requested = False
            await self._stop_capture()

    async def _ensure_capture_started(self) -> None:
        if self._capture is not None or self._open_pcm is None:
            return
        fmt = self._open_pcm.info.pcm_format
        audio_format = SupportedAudioFormat(
            codec=AudioCodec.PCM,
            channels=fmt.channels,
            sample_rate=fmt.sample_rate,
            bit_depth=fmt.bit_depth,
        )
        capture = self._client.create_source_capture(audio_format)
        await capture.start()
        self._capture = capture
        chunk_bytes = fmt.frame_bytes * (fmt.sample_rate * _READ_CHUNK_MS // 1000)
        self._feed_task = asyncio.get_running_loop().create_task(
            self._feed_loop(capture, self._open_pcm.reader, chunk_bytes)
        )

    async def _stop_capture(self) -> None:
        if self._feed_task is not None:
            self._feed_task.cancel()
            self._feed_task = None
        if self._capture is not None:
            await self._capture.stop()
            self._capture = None

    @staticmethod
    async def _feed_loop(
        capture: SourceCapture, reader: asyncio.StreamReader, chunk_bytes: int
    ) -> None:
        while True:
            try:
                chunk = await reader.readexactly(chunk_bytes)
            except asyncio.IncompleteReadError:
                return
            await capture.feed(chunk)
