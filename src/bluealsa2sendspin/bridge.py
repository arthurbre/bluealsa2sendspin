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
import contextlib
import logging
from collections.abc import Coroutine
from typing import Any, Protocol, cast

from aiosendspin.client import SendspinClient, SourceCapture
from aiosendspin.models.core import ServerCommandPayload
from aiosendspin.models.player import SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, SignalState

from .bluealsa import BlueAlsaClient, OpenPcm, PcmInfo, UnsupportedPcmFormatError

logger = logging.getLogger(__name__)

_READ_CHUNK_MS = 20

# Keep this in sync with the aiosendspin dependency pin in pyproject.toml: it's
# quoted in _get_signal_connection's RuntimeError so that message points straight
# at the pin that needs bumping/re-pinning if aiosendspin's private shape below
# ever changes.
_AIOSENDSPIN_PIN = "aiosendspin[source]>=9.1,<9.2"

# Sentinel distinguishing "attribute absent" from "attribute present but None"
# in _get_signal_connection; a real SendspinClient always has this attribute
# (defaulting to None until a connection is admitted), so seeing the sentinel
# back means the attribute itself is gone.
_MISSING: Any = object()


def _log_task_exception(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    if (exc := task.exception()) is not None:
        logger.error("Unhandled error in background task", exc_info=exc)


class _SignalConnection(Protocol):
    """The one member ``_get_signal_connection`` needs from the admitted connection.

    aiosendspin 9.1.x's ``SendspinClient`` forwards ``send_player_state``/
    ``send_available``/``send_group_command`` to its private admitted connection as
    public pass-throughs, but has no equivalent public wrapper for
    ``send_source_signal()``, and ``SourceCapture`` (also reachable only via
    ``SendspinClient``) exposes no signal-related API either. So bluealsa2sendspin
    reaches into ``SendspinClient``'s private ``_admitted_connection`` directly --
    see the dependency pin comment in pyproject.toml.

    This narrow Protocol plus the runtime shape check in ``_get_signal_connection``
    mean a future aiosendspin release that renames/removes either that attribute or
    this method fails with a clear ``RuntimeError`` at the one call site that
    depends on it, instead of a raw ``AttributeError`` surfacing from inside
    aiosendspin.
    """

    async def send_source_signal(self, signal: SignalState) -> None: ...


def _get_signal_connection(client: SendspinClient) -> _SignalConnection | None:
    """Fetch ``client``'s private admitted connection, verifying its shape first.

    Returns ``None`` when there's currently no admitted connection -- a normal,
    expected state (not yet connected, or connected but not yet admitted).
    Raises ``RuntimeError`` if the private ``_admitted_connection`` attribute or
    its ``send_source_signal`` method is missing, which would mean a newer
    aiosendspin release changed shape in a way bluealsa2sendspin's pin didn't
    account for.

    Re-checked on every call rather than validated once and cached: both checks
    below are plain attribute lookups with no I/O or locking, negligible next to
    the network send that follows a successful check, so there's no meaningful
    overhead to re-verifying instead of trusting a cached "shape is fine" flag.
    """
    connection = getattr(client, "_admitted_connection", _MISSING)
    if connection is _MISSING:
        raise RuntimeError(
            "aiosendspin's SendspinClient no longer has a '_admitted_connection' attribute. "
            f"bluealsa2sendspin is pinned to {_AIOSENDSPIN_PIN} and relies on reaching into "
            "that private attribute to call send_source_signal() (see the dependency comment "
            "in pyproject.toml); aiosendspin's internals have changed shape and "
            "bridge.py's _get_signal_connection() needs updating to match."
        )
    if connection is None:
        return None
    send_source_signal = getattr(connection, "send_source_signal", None)
    if not callable(send_source_signal):
        raise RuntimeError(
            "aiosendspin's admitted connection object no longer has a callable "
            f"'send_source_signal' method. bluealsa2sendspin is pinned to {_AIOSENDSPIN_PIN} "
            "and relies on calling it directly (see the dependency comment in pyproject.toml); "
            "aiosendspin's internals have changed shape and bridge.py's "
            "_get_signal_connection() needs updating to match."
        )
    return cast(_SignalConnection, connection)


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
        # Guards topology (dis)connection and capture start/stop against each
        # other; _report_signal has its own lock since it's called from
        # within sections already holding this one.
        self._lock = asyncio.Lock()
        self._signal_lock = asyncio.Lock()

    async def start(self) -> None:
        """Wire up listeners and reconcile against whatever BlueALSA currently reports."""
        self._client.add_server_command_listener(self._on_server_command)
        self._client.add_disconnect_listener(self._on_disconnected)
        await self._bluealsa.watch_topology_changes(self._schedule_topology_sync)
        await self._sync_topology()

    async def on_connected(self) -> None:
        """Re-announce the current signal state on a fresh (re)connection."""
        await self._report_signal()

    def _on_disconnected(self) -> None:
        # The SourceCapture and its feed task are bound to the now-dead
        # connection and can't be reused; drop them so a subsequent
        # server-requested start (after we reconnect) creates a fresh one
        # instead of being blocked by _ensure_capture_started's guard.
        # _reported_signal is cleared too, since the server forgets our
        # signal state on disconnect and on_connected() must re-announce it.
        if self._feed_task is not None:
            self._feed_task.cancel()
            self._feed_task = None
        self._capture = None
        self._reported_signal = None

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.get_running_loop().create_task(coro)
        task.add_done_callback(_log_task_exception)
        return task

    def _schedule_topology_sync(self) -> None:
        self._spawn(self._sync_topology())

    async def _sync_topology(self) -> None:
        async with self._lock:
            try:
                info = await self._bluealsa.find_source_pcm()
            except UnsupportedPcmFormatError as err:
                logger.error("Ignoring BlueALSA source PCM: %s", err)
                info = None
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
        self._spawn(self._report_signal())

    async def _report_signal(self) -> None:
        async with self._signal_lock:
            connection = _get_signal_connection(self._client)
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
        self._spawn(self._handle_server_command(payload.source.command))

    async def _handle_server_command(self, command: str) -> None:
        async with self._lock:
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
        self._feed_task = self._spawn(self._feed_loop(capture, self._open_pcm.reader, chunk_bytes))

    async def _stop_capture(self) -> None:
        if self._feed_task is not None:
            self._feed_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._feed_task
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
