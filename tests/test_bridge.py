"""Unit tests for bridge.py's defensive access to aiosendspin's private connection.

``SourceBridge._report_signal()`` reaches into ``SendspinClient``'s private
``_admitted_connection`` to call ``send_source_signal()``, since aiosendspin 9.1.x
has no public wrapper for it (see the dependency pin comment in pyproject.toml).
These tests pin down that when aiosendspin's private shape doesn't match what
bluealsa2sendspin's ``_get_signal_connection`` helper expects, the failure is a
clear, actionable ``RuntimeError`` raised at that call site -- not a raw
``AttributeError`` bubbling up from unrelated code, and not a silent no-op.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from aiosendspin.client import SendspinClient

from bluealsa2sendspin.bluealsa import BlueAlsaClient
from bluealsa2sendspin.bridge import SourceBridge

# Both stand-ins below are cast to SendspinClient/BlueAlsaClient at the SourceBridge
# call site: SourceBridge only ever touches the attributes these stubs actually
# define, and the point of these tests is to prove that a mismatched *runtime*
# shape is caught explicitly, not that a static type checker would allow it.
_UNUSED_BLUEALSA = cast(BlueAlsaClient, object())


class _ClientWithoutAdmittedConnectionAttr:
    """A ``SendspinClient`` stand-in with no ``_admitted_connection`` attribute at all.

    Simulates aiosendspin renaming/removing that private attribute.
    """


class _ConnectionWithoutSendSourceSignal:
    """An admitted-connection stand-in shaped without a ``send_source_signal`` method.

    Simulates aiosendspin renaming/removing that method on the connection object.
    """


class _ClientWithMisshapenConnection:
    """A ``SendspinClient`` stand-in whose admitted connection lacks ``send_source_signal``."""

    def __init__(self) -> None:
        self._admitted_connection: Any = _ConnectionWithoutSendSourceSignal()


async def test_report_signal_raises_runtimeerror_when_admitted_connection_attr_missing() -> None:
    """If aiosendspin ever renames/removes ``_admitted_connection``, fail loudly, not silently."""
    client = cast(SendspinClient, _ClientWithoutAdmittedConnectionAttr())
    bridge = SourceBridge(client, _UNUSED_BLUEALSA)

    with pytest.raises(RuntimeError, match="_admitted_connection") as exc_info:
        await bridge._report_signal()

    # Must be our own, actionable RuntimeError -- not a bare/confusing AttributeError,
    # and the message must be self-contained enough to act on without reading bridge.py.
    assert not isinstance(exc_info.value, AttributeError)
    assert "aiosendspin" in str(exc_info.value)
    assert "pinned to" in str(exc_info.value)


async def test_report_signal_raises_runtimeerror_when_send_source_signal_missing() -> None:
    """If aiosendspin's connection object ever drops ``send_source_signal``, fail loudly."""
    client = cast(SendspinClient, _ClientWithMisshapenConnection())
    bridge = SourceBridge(client, _UNUSED_BLUEALSA)

    with pytest.raises(RuntimeError, match="send_source_signal") as exc_info:
        await bridge._report_signal()

    assert not isinstance(exc_info.value, AttributeError)
    assert "aiosendspin" in str(exc_info.value)
    assert "pinned to" in str(exc_info.value)
