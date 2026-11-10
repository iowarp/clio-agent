"""Failure-isolated tool-observer notification and progress correlation."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: The structured-value lane: when a caller binds a list here (a ``run_code`` script's
#: nested call), each completed MCP call appends ``(error, observer_result)``, so the
#: script gets the call's structured content while the model-facing text stays bounded.
RESULT_SINK: ContextVar[list[tuple[str | None, Any]] | None] = ContextVar(
    "clio_tool_result_sink", default=None
)
#: Set only in a background ``run_code`` script's context: an object whose ``observe``
#: records repeated successful nested calls as one counted record per window
#: (:mod:`clio_agent.gact.code_task_records`). Unset, every call is recorded as it happens.
OBSERVATION_GROUP: ContextVar[Any | None] = ContextVar("clio_observation_group", default=None)
#: True while a refused call's completion is reported (:func:`notify_refused_call`), so
#: the observer records it as denied, not as a call that ran and failed.
REFUSED_CALL: ContextVar[bool] = ContextVar("clio_refused_call", default=False)
#: One tool call's identity, shared by its permission request and its observed row. A
#: mutable holder so a copied context (a worker thread) still sees one identity.
_CALL_IDENTITY: ContextVar[dict[str, Any] | None] = ContextVar("clio_call_identity", default=None)


@contextmanager
def call_identity_scope() -> Iterator[None]:
    """Give the tool call made inside this scope one identity from gate to result.

    A permission request raised before the call starts names the identity the call's
    row will carry, so a client can show the waiting call beside its question. Each
    scope is fresh, so a nested call (a script's call, an MCP call under a native one)
    gets its own identity.
    """

    token = _CALL_IDENTITY.set({"id": "", "taken": False})
    try:
        yield
    finally:
        _CALL_IDENTITY.reset(token)


def peek_call_identity() -> str:
    """Return this scope's call identity, minting it on first use (``""`` outside one)."""

    holder = _CALL_IDENTITY.get()
    if holder is None:
        return ""
    if not holder["id"]:
        holder["id"] = f"call_{uuid.uuid4().hex[:12]}"
    return str(holder["id"])


def call_identity_claimed() -> bool:
    """Whether this scope's call has already started (its row exists)."""

    holder = _CALL_IDENTITY.get()
    return bool(holder is not None and holder["taken"])


def claim_call_identity() -> str:
    """Claim this scope's identity for the call's observed row, once.

    Returns the identity a permission request already named, or mints it, so a
    request raised later inside the running call (a script's own approval) names the
    same row. ``""`` outside a scope or once claimed: the caller mints its own.
    """

    holder = _CALL_IDENTITY.get()
    if holder is None or holder["taken"]:
        return ""
    holder["taken"] = True
    return peek_call_identity()


ToolObserver = Callable[
    [str, Mapping[str, Any], Optional[str], Optional[str], Any | None],
    Any | None,
]
LegacyToolObserver = Callable[[str, Mapping[str, Any], Optional[str], Optional[str]], None]


def notify_tool_observer(
    observer: Optional[ToolObserver | LegacyToolObserver],
    name: str,
    args: Mapping[str, Any],
    phase: str,
    error: str | None = None,
    result: Any | None = None,
) -> Any | None:
    """Notify an observer of one tool-call phase.

    The observer records the call in clio-core (the transcript and the UI read it from
    there). A failure to record is NOT swallowed: clio-core would no longer hold what the
    agent did, so it propagates typed to the caller.
    """

    sink = RESULT_SINK.get() if phase == "completed" else None
    if sink is not None:
        sink.append((error, result))
    if observer is None:
        return None
    group = OBSERVATION_GROUP.get()
    if group is not None:  # a background script's nested call: recorded grouped (CT5)
        return group.observe(observer, name, args, phase, error, result)
    if result is None:
        return observer(name, dict(args), phase, error)  # type: ignore[misc, call-arg, no-any-return]
    try:
        return observer(  # type: ignore[misc, call-arg, no-any-return]
            name, dict(args), phase, error, result
        )
    except TypeError:
        return observer(name, dict(args), phase, error)  # type: ignore[misc, call-arg, no-any-return]


def notify_refused_call(
    observer: Optional[ToolObserver | LegacyToolObserver],
    name: str,
    args: Mapping[str, Any],
    reason: str,
) -> PermissionError:
    """Record a call the permission gate or a hook refused; return the error to raise.

    The refused call is part of what the agent did, so the transcript and the UI
    show it as a refused (denied) call rather than leaving only the model's account of it.
    """

    notify_tool_observer(observer, name, args, "started", None)
    token = REFUSED_CALL.set(True)
    try:
        notify_tool_observer(observer, name, args, "completed", reason)
    finally:
        REFUSED_CALL.reset(token)
    return PermissionError(reason)


def observer_progress_handler(
    observer: Optional[ToolObserver | LegacyToolObserver],
    name: str,
    args: Mapping[str, Any],
    observer_handle: object,
) -> Callable[[float, float | None, str | None], Awaitable[None]]:
    """Return an async MCP progress callback carrying the observer's start handle."""

    async def forward(progress: float, total: float | None, message: str | None) -> None:
        notify_tool_observer(
            observer,
            name,
            args,
            "progress",
            result={
                "observer_handle": observer_handle,
                "progress": progress,
                "total": total,
                "message": message,
            },
        )

    return forward


__all__ = [
    "LegacyToolObserver",
    "ToolObserver",
    "call_identity_scope",
    "call_identity_claimed",
    "claim_call_identity",
    "notify_refused_call",
    "notify_tool_observer",
    "observer_progress_handler",
    "peek_call_identity",
]
