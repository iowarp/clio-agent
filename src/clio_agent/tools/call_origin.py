"""Whose call it is, carried to the server in the request's ``_meta`` (FP-D7).

A call a view makes (the person's click in it, a dashboard's linked chart moving the
time) runs through the same executor as the agent's own calls. A server that keeps a
change log ("by the agent", "by the person") cannot tell them apart from the arguments,
so CLIO marks the view's calls: ``_meta["ai.iowarp.clio/origin"] = "view"``. The agent's
calls carry no mark. The mark is set for the duration of one view call by
:func:`scope` and added to the ``tools/call`` request by the product client.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Literal

#: The ``_meta`` key a server reads (contract: ``mcp-apps-contracts.md``, "Call origin").
ORIGIN_META_KEY = "ai.iowarp.clio/origin"

Origin = Literal["view"]

_ORIGIN: ContextVar[Origin | None] = ContextVar("clio_call_origin", default=None)


@contextmanager
def scope(origin: Origin) -> Iterator[None]:
    """Mark every ``tools/call`` sent inside this block as ``origin``'s."""

    token = _ORIGIN.set(origin)
    try:
        yield
    finally:
        _ORIGIN.reset(token)


def current() -> Origin | None:
    """The origin of the call being sent, or ``None`` for the agent's own."""

    return _ORIGIN.get()


def with_origin(meta: Mapping[str, Any] | None) -> dict[str, Any]:
    """``meta`` with the origin mark added when a view's call is being sent."""

    out = dict(meta or {})
    origin = _ORIGIN.get()
    if origin is not None:
        out[ORIGIN_META_KEY] = origin
    return out
