"""Unit tests for bridge.py's defensive access to aiosendspin's private connection.

``_get_signal_connection()`` reaches into ``SendspinClient``'s private
``_admitted_connection`` to reach ``send_source_signal()``, since aiosendspin 9.1.x
has no public wrapper for it (see the dependency pin comment in pyproject.toml).
These tests pin down that when aiosendspin's private shape doesn't match what
``_get_signal_connection`` expects, the failure is a clear, actionable
``RuntimeError`` -- not a raw ``AttributeError`` bubbling up from unrelated code,
and not a silent no-op.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from aiosendspin.client import SendspinClient

from bluealsa2sendspin.bridge import _get_signal_connection


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


def test_get_signal_connection_raises_runtimeerror_when_admitted_connection_attr_missing() -> None:
    """If aiosendspin ever renames/removes ``_admitted_connection``, fail loudly, not silently."""
    client = cast(SendspinClient, _ClientWithoutAdmittedConnectionAttr())

    with pytest.raises(RuntimeError, match="_admitted_connection") as exc_info:
        _get_signal_connection(client)

    # Must be our own, actionable RuntimeError -- not a bare/confusing AttributeError,
    # and the message must be self-contained enough to act on without reading bridge.py.
    assert not isinstance(exc_info.value, AttributeError)
    assert "aiosendspin" in str(exc_info.value)
    assert "pinned to" in str(exc_info.value)


def test_get_signal_connection_raises_runtimeerror_when_send_source_signal_missing() -> None:
    """If aiosendspin's connection object ever drops ``send_source_signal``, fail loudly."""
    client = cast(SendspinClient, _ClientWithMisshapenConnection())

    with pytest.raises(RuntimeError, match="send_source_signal") as exc_info:
        _get_signal_connection(client)

    assert not isinstance(exc_info.value, AttributeError)
    assert "aiosendspin" in str(exc_info.value)
    assert "pinned to" in str(exc_info.value)
