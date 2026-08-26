"""Command-line entry point: ``bluealsa2sendspin pair`` and ``bluealsa2sendspin run``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import logging
import secrets
import signal
from pathlib import Path

from aiosendspin.client import PairingSupport, SendspinClient
from aiosendspin.models.source import ClientHelloSourceFeatures, ClientHelloSourceSupport
from aiosendspin.models.types import Roles
from aiosendspin.noise.trust_store import PskCategory

from . import __version__
from .bluealsa import BlueAlsaClient
from .bridge import SourceBridge
from .config import default_state_dir, load_or_create_identity, open_pairing_store

logger = logging.getLogger(__name__)

_PIN_DIGITS = 8  # aiosendspin requires the static PIN to be exactly 8 decimal digits
_MAX_RECONNECT_DELAY_S = 30.0
# Matches the client SDK's own pairing-window lifetime: the operator has this long,
# after running this command, to add the source and enter the PIN in Music Assistant.
_PAIRING_TIMEOUT_S = 300.0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    args = _parse_args(argv)
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    )
    if args.command == "pair":
        asyncio.run(_pair(args))
    elif args.command == "run":
        asyncio.run(_run(args))
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="bluealsa2sendspin")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=default_state_dir(),
        help="Where the Sendspin identity and pairing state are stored.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--server-url",
        required=True,
        help="Sendspin server WebSocket URL, e.g. ws://ma-host:8927/sendspin",
    )
    common.add_argument("--client-name", default="bluealsa2sendspin")

    subparsers.add_parser(
        "pair", parents=[common], help="Pair with a Sendspin server using a static PIN."
    )
    subparsers.add_parser("run", parents=[common], help="Run the BlueALSA -> Sendspin bridge.")
    return parser.parse_args(argv)


async def _pair(args: argparse.Namespace) -> None:
    identity = load_or_create_identity(args.state_dir)
    store = await open_pairing_store(args.state_dir)

    config = await store.get_pairing_config()
    if not config.static_pin_enabled:
        await store.store_pairing_config(dataclasses.replace(config, static_pin_enabled=True))
    pin = await store.static_pin()
    if pin is None:
        pin = "".join(secrets.choice("0123456789") for _ in range(_PIN_DIGITS))
        await store.set_static_pin(pin)

    print(f"Client ID: {identity.peer_id}")
    print(f"Pairing PIN: {pin}")
    print("Add this source in Music Assistant now and enter the PIN above.")
    print(f"Waiting up to {_PAIRING_TIMEOUT_S:.0f}s for pairing...")

    client = SendspinClient(
        identity,
        args.client_name,
        [Roles.SOURCE],
        pairing_store=store,
        source_support=_source_support(),
        pairing_support=PairingSupport(offer_static_pin=True, secret_locations=("device",)),
    )
    client.open_pairing_window()
    await client.connect(args.server_url)
    try:
        # Pairing itself isn't necessarily done by the time connect() returns: the
        # operator drives it from Music Assistant's own UI, at their own pace, over
        # the connection we just established.
        if await _wait_for_pairing(client):
            print("Paired successfully.")
        else:
            print("Timed out waiting for pairing; run this command again when you're ready.")
    finally:
        await client.disconnect()


async def _wait_for_pairing(client: SendspinClient) -> bool:
    try:
        async with asyncio.timeout(_PAIRING_TIMEOUT_S):
            while True:
                if (
                    client.noise_psk is not None
                    and client.noise_psk.category is PskCategory.LONG_TERM
                ):
                    return True
                if not client.connected:
                    return False
                await asyncio.sleep(0.5)
    except TimeoutError:
        return False


async def _run(args: argparse.Namespace) -> None:
    identity = load_or_create_identity(args.state_dir)
    store = await open_pairing_store(args.state_dir)

    client = SendspinClient(
        identity,
        args.client_name,
        [Roles.SOURCE],
        pairing_store=store,
        source_support=_source_support(),
    )
    bluealsa = await BlueAlsaClient.connect()
    bridge = SourceBridge(client, bluealsa)
    await bridge.start()

    stop = asyncio.Event()
    disconnected = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    client.add_disconnect_listener(disconnected.set)

    try:
        delay = 1.0
        while not stop.is_set():
            disconnected.clear()
            try:
                await client.connect(args.server_url)
            except Exception:
                logger.exception(
                    "Failed to connect to %s; retrying in %.0fs", args.server_url, delay
                )
                await _sleep_unless_stopped(delay, stop)
                delay = min(delay * 2, _MAX_RECONNECT_DELAY_S)
                continue
            delay = 1.0
            logger.info("Connected to Sendspin server %s", args.server_url)
            await bridge.on_connected()
            await _wait_for_either(stop, disconnected)
            if not stop.is_set():
                logger.warning("Disconnected from %s; reconnecting", args.server_url)
    finally:
        if client.connected:
            await client.disconnect()
        bluealsa.close()


async def _sleep_unless_stopped(delay: float, stop: asyncio.Event) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=delay)


async def _wait_for_either(*events: asyncio.Event) -> None:
    tasks = [asyncio.create_task(event.wait()) for event in events]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()


def _source_support() -> ClientHelloSourceSupport:
    return ClientHelloSourceSupport(features=ClientHelloSourceFeatures(line_sense=True))
