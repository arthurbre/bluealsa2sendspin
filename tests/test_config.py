from pathlib import Path

from aiosendspin.client import SendspinClient
from aiosendspin.models.types import Roles

from bluealsa2sendspin.cli import _source_support
from bluealsa2sendspin.config import load_or_create_identity, open_pairing_store


def test_load_or_create_identity_is_stable_across_calls(tmp_path: Path) -> None:
    first = load_or_create_identity(tmp_path)
    second = load_or_create_identity(tmp_path)
    assert first.peer_id == second.peer_id


async def test_open_pairing_store_backs_a_source_client(tmp_path: Path) -> None:
    identity = load_or_create_identity(tmp_path)
    store = await open_pairing_store(tmp_path)

    client = SendspinClient(
        identity,
        "bluealsa2sendspin-test",
        [Roles.SOURCE],
        pairing_store=store,
        source_support=_source_support(),
    )
    assert client.roles == [Roles.SOURCE]
    assert client.identity.peer_id == identity.peer_id
