"""BlueALSA D-Bus client: discover and open A2DP-sink source PCM streams.

Talks directly to ``bluealsad`` over D-Bus (``org.bluealsa``), bypassing
PipeWire/PulseAudio entirely. See ``org.bluealsa.Manager1(7)`` and
``org.bluealsa.PCM1(7)`` for the interfaces used here.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast

from dbus_fast import BusType
from dbus_fast.aio import MessageBus, ProxyInterface

logger = logging.getLogger(__name__)

SERVICE_NAME = "org.bluealsa"
ROOT_PATH = "/org/bluealsa"
MANAGER_INTERFACE = "org.bluealsa.Manager1"
PCM_INTERFACE = "org.bluealsa.PCM1"
OBJECT_MANAGER_INTERFACE = "org.freedesktop.DBus.ObjectManager"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"

# The PCM we want: audio arriving FROM a phone (A2DP source role on the phone)
# INTO this host, which BlueALSA exposes as its A2DP-sink transport handing us
# ("source" mode, i.e. BlueALSA is the source of PCM data for us) the samples.
WANTED_TRANSPORT = "A2DP-sink"
WANTED_MODE = "source"

# (bit_depth, byte_width) combinations bluealsa2sendspin knows how to feed
# onward unmodified: standard little-endian signed PCM with no padding.
_SUPPORTED_SAMPLE_LAYOUTS = {(16, 2), (24, 3), (32, 4)}


class UnsupportedPcmFormatError(RuntimeError):
    """Raised when a BlueALSA PCM reports a format bluealsa2sendspin can't feed onward."""


# dbus-fast builds its proxy interfaces' call_*/get_*/on_* methods dynamically from
# runtime introspection, so mypy can't see them on `ProxyInterface` itself. These
# narrow protocols describe just the members bluealsa2sendspin actually calls, and
# `BlueAlsaClient._get_interface()` casts to them at the one point that crosses from
# dynamic to statically-typed code.
class _ManagerProxy(Protocol):
    # Version/Adapters are Manager1 *properties* (org.bluealsa.Manager1(7)), not
    # methods -- dbus-fast exposes properties as get_<name>(), not call_<name>().
    async def get_version(self) -> str: ...
    async def get_adapters(self) -> list[str]: ...


class _ObjectManagerProxy(Protocol):
    async def call_get_managed_objects(
        self, *, unpack_variants: bool = ...
    ) -> dict[str, dict[str, dict[str, Any]]]: ...
    def on_interfaces_added(self, callback: Callable[[str, dict[str, Any]], None]) -> None: ...
    def on_interfaces_removed(self, callback: Callable[[str, list[str]], None]) -> None: ...


class _PcmProxy(Protocol):
    async def call_open(self) -> tuple[int, int]: ...


class _PropertiesProxy(Protocol):
    def on_properties_changed(
        self, callback: Callable[[str, dict[str, Any], list[str]], None]
    ) -> None: ...
    def off_properties_changed(
        self, callback: Callable[[str, dict[str, Any], list[str]], None]
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class PcmFormat:
    """Decoded ``PCM1.Format`` plus the sibling ``Channels``/``Rate`` properties."""

    sample_rate: int
    channels: int
    bit_depth: int
    signed: bool
    big_endian: bool
    byte_width: int

    @classmethod
    def decode(cls, fmt: int, rate: int, channels: int) -> PcmFormat:
        """Decode BlueALSA's 16-bit packed ``Format`` identifier.

        Bit layout: bit 15 signedness, bit 14 endianness, bits 13-8 byte width,
        bits 7-0 bit width (see ``org.bluealsa.PCM1(7)``).
        """
        return cls(
            sample_rate=rate,
            channels=channels,
            bit_depth=fmt & 0xFF,
            signed=bool(fmt & 0x8000),
            big_endian=bool(fmt & 0x4000),
            byte_width=(fmt >> 8) & 0x3F,
        )

    def require_supported(self) -> None:
        """Raise ``UnsupportedPcmFormatError`` unless this is plain signed little-endian PCM."""
        if not self.signed or self.big_endian:
            raise UnsupportedPcmFormatError(
                f"BlueALSA PCM format is signed={self.signed} big_endian={self.big_endian}; "
                "bluealsa2sendspin only supports signed little-endian PCM"
            )
        layout = (self.bit_depth, self.byte_width)
        if layout not in _SUPPORTED_SAMPLE_LAYOUTS:
            raise UnsupportedPcmFormatError(
                f"BlueALSA PCM reports {self.bit_depth}-bit samples packed in "
                f"{self.byte_width} bytes; supported layouts are "
                f"{sorted(_SUPPORTED_SAMPLE_LAYOUTS)}"
            )

    @property
    def frame_bytes(self) -> int:
        """Bytes per PCM frame (one sample per channel)."""
        return self.byte_width * self.channels


@dataclass(frozen=True, slots=True)
class PcmInfo:
    """A discovered BlueALSA PCM object matching :data:`WANTED_TRANSPORT`/:data:`WANTED_MODE`."""

    object_path: str
    device_path: str
    pcm_format: PcmFormat
    running: bool  # as of discovery only; OpenPcm.running is the live value once opened


def _select_source_pcm(managed_objects: dict[str, dict[str, dict[str, Any]]]) -> PcmInfo | None:
    candidates = [
        (path, props[PCM_INTERFACE])
        for path, props in managed_objects.items()
        if PCM_INTERFACE in props
        and props[PCM_INTERFACE].get("Transport") == WANTED_TRANSPORT
        and props[PCM_INTERFACE].get("Mode") == WANTED_MODE
    ]
    if not candidates:
        return None
    if len(candidates) > 1:
        logger.warning(
            "%d BlueALSA source PCMs are present at once; using the most recently connected",
            len(candidates),
        )
    path, props = max(candidates, key=lambda item: item[1]["Sequence"])
    pcm_format = PcmFormat.decode(
        fmt=props["Format"], rate=props["Rate"], channels=props["Channels"]
    )
    pcm_format.require_supported()
    return PcmInfo(
        object_path=path,
        device_path=props["Device"],
        pcm_format=pcm_format,
        running=props["Running"],
    )


class OpenPcm:
    """An opened BlueALSA PCM stream: its audio pipe plus a live property watch."""

    def __init__(
        self,
        info: PcmInfo,
        properties: _PropertiesProxy,
        reader: asyncio.StreamReader,
        transport: asyncio.ReadTransport,
        ctl_fd: int,
    ) -> None:
        self.info = info
        self.running = info.running
        self._properties = properties
        self.reader = reader
        self._transport = transport
        self._ctl_fd = ctl_fd
        self._running_callbacks: list[Callable[[bool], None]] = []
        self._properties.on_properties_changed(self._on_properties_changed)

    def on_running_changed(self, callback: Callable[[bool], None]) -> None:
        """Register ``callback(running)``, called whenever ``Running`` changes."""
        self._running_callbacks.append(callback)

    def _on_properties_changed(
        self,
        interface_name: str,
        changed: dict[str, Any],
        _invalidated: list[str],
    ) -> None:
        if interface_name != PCM_INTERFACE or "Running" not in changed:
            return
        self.running = changed["Running"].value
        for callback in self._running_callbacks:
            callback(self.running)

    def close(self) -> None:
        """Close the PCM pipe and controller socket."""
        self._properties.off_properties_changed(self._on_properties_changed)
        self._transport.close()
        os.close(self._ctl_fd)


class BlueAlsaClient:
    """Discovers and opens BlueALSA A2DP-sink source PCM streams over D-Bus."""

    def __init__(self, bus: MessageBus) -> None:
        self._bus = bus
        self._object_manager: _ObjectManagerProxy | None = None

    @classmethod
    async def connect(cls) -> BlueAlsaClient:
        """Connect to the system D-Bus and confirm ``bluealsad`` is reachable."""
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        client = cls(bus)
        try:
            manager = cast(_ManagerProxy, await client._get_interface(ROOT_PATH, MANAGER_INTERFACE))
            version = await manager.get_version()
            adapters = await manager.get_adapters()
        except Exception as err:
            bus.disconnect()
            raise RuntimeError(
                "Could not reach bluealsad on the system D-Bus "
                f"(service {SERVICE_NAME!r}); is it installed and running?"
            ) from err
        logger.info("Connected to bluealsad %s (adapters: %s)", version, ", ".join(adapters))
        return client

    def close(self) -> None:
        """Disconnect from D-Bus."""
        self._bus.disconnect()

    async def _get_interface(self, path: str, interface_name: str) -> ProxyInterface:
        introspection = await self._bus.introspect(SERVICE_NAME, path)
        obj = self._bus.get_proxy_object(SERVICE_NAME, path, introspection)
        return obj.get_interface(interface_name)

    async def _get_object_manager(self) -> _ObjectManagerProxy:
        if self._object_manager is None:
            raw = await self._get_interface(ROOT_PATH, OBJECT_MANAGER_INTERFACE)
            self._object_manager = cast(_ObjectManagerProxy, raw)
        return self._object_manager

    async def find_source_pcm(self) -> PcmInfo | None:
        """Return the currently connected phone's source PCM, if any."""
        manager = await self._get_object_manager()
        managed_objects = await manager.call_get_managed_objects(unpack_variants=True)
        return _select_source_pcm(managed_objects)

    async def watch_topology_changes(self, on_change: Callable[[], None]) -> None:
        """Call ``on_change`` whenever a PCM object appears or disappears under BlueALSA."""
        object_manager = await self._get_object_manager()

        def _added(_path: str, _interfaces: dict[str, Any]) -> None:
            on_change()

        def _removed(_path: str, _interfaces: list[str]) -> None:
            on_change()

        object_manager.on_interfaces_added(_added)
        object_manager.on_interfaces_removed(_removed)

    async def open(self, info: PcmInfo) -> OpenPcm:
        """Open ``info``'s PCM pipe and start watching its ``Running`` property."""
        pcm = cast(_PcmProxy, await self._get_interface(info.object_path, PCM_INTERFACE))
        properties = cast(
            _PropertiesProxy, await self._get_interface(info.object_path, PROPERTIES_INTERFACE)
        )
        pcm_fd, ctl_fd = await pcm.call_open()
        pipe = os.fdopen(pcm_fd, "rb", buffering=0)
        try:
            loop = asyncio.get_running_loop()
            reader = asyncio.StreamReader(loop=loop)
            protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
            transport, _ = await loop.connect_read_pipe(lambda: protocol, pipe)
        except Exception:
            pipe.close()
            os.close(ctl_fd)
            raise
        return OpenPcm(
            info=info, properties=properties, reader=reader, transport=transport, ctl_fd=ctl_fd
        )
