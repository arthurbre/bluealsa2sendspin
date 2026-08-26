"""Persisted state: where it lives, and the Sendspin identity kept there."""

from __future__ import annotations

import json
import os
from pathlib import Path

from aiosendspin.noise.keys import Identity, b64url_decode, b64url_encode
from aiosendspin.noise.trust_store import FileClientPairingStore


def default_state_dir() -> Path:
    """Return the XDG state directory for bluealsa2sendspin."""
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg_state_home) if xdg_state_home else Path.home() / ".local" / "state"
    return base / "bluealsa2sendspin"


def load_or_create_identity(state_dir: Path) -> Identity:
    """Load the persisted Sendspin identity from ``state_dir``, creating one if absent."""
    path = state_dir / "identity.json"
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        return Identity.from_private_bytes(b64url_decode(data["private_key_b64u"]))

    identity = Identity.generate()
    state_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"private_key_b64u": b64url_encode(identity.private_bytes)}),
        encoding="utf-8",
    )
    return identity


async def open_pairing_store(state_dir: Path) -> FileClientPairingStore:
    """Open (creating if absent) the persisted Sendspin pairing store."""
    state_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - one-off, at startup only
    return await FileClientPairingStore.open(state_dir / "pairing.json")
