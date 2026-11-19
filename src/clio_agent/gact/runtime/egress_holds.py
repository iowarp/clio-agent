"""Network permission answers that hold past the connection they were given for.

A client rarely opens one connection: ssh retries, and a batch scheduler's command line opens
a new connection for every submit, query and status check. Asking the person each time would
repeat the same question many times in one turn, so an answer to a ``network_egress`` ask is
remembered for a while:

* "Deny" holds for that host for the rest of the turn it was given in, so retries fail at once.
* "Allow once" holds for that host and port for the rest of the turn.
* "Allow for this session" holds for that host and port while the same session stays active.
* An "Allow" also covers the child sessions the session started (a delegate, or the agent that
  answers a plugin's question), since they do part of the same work.
* "Allow for this workspace" is a ``host_pattern`` policy (permission_policies), not held here.

A workspace ``host_pattern`` policy is consulted before any held answer, so a policy the person
adds always decides. Nothing here is saved: a server restart forgets every held answer.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

_LOCK = threading.Lock()
#: ``(workspace_id, host, write)`` → the ``(session_id, turn_id)`` the "Deny" belongs to.
_DENIED: dict[tuple[str, str, bool], tuple[str, str]] = {}
#: ``(workspace_id, host, port, write)`` → ``(session_id, turn_id)``; ``turn_id`` is ``None``
#: for an answer that holds for the session.
_ALLOWED: dict[tuple[str, str, int, bool], tuple[str, str | None]] = {}
#: How many parent links :func:`_descends_from` follows (a guard against a cyclic record).
_MAX_LINEAGE = 16


def running_turn_id(app: "FastAPI", session_id: str) -> str:
    """The id of the turn running on ``session_id`` now, or ``""`` when it is idle."""
    runner = getattr(app.state, "turn_runner", None)
    if runner is None or not session_id:
        return ""
    try:
        handle = runner.handle(session_id)
    except Exception:  # noqa: BLE001 - an unreadable runner counts as idle
        return ""
    return str(getattr(handle, "turn_id", "") or "") if handle is not None else ""


def denied_this_turn(app: "FastAPI", workspace_id: str, host: str, write: bool) -> bool:
    """Whether the person denied ``host`` earlier in the turn that is still running."""
    key = (workspace_id, host, write)
    with _LOCK:
        held = _DENIED.get(key)
    if held is None:
        return False
    if running_turn_id(app, held[0]) == held[1]:
        return True
    with _LOCK:
        if _DENIED.get(key) == held:
            _DENIED.pop(key, None)
    return False


def hold_denial(app: "FastAPI", workspace_id: str, host: str, write: bool, session_id: str) -> None:
    """Remember a "Deny" for ``host`` until the turn running on ``session_id`` ends."""
    with _LOCK:
        _DENIED[(workspace_id, host, write)] = (session_id, running_turn_id(app, session_id))


def allowed_still(
    app: "FastAPI", workspace_id: str, host: str, port: int, write: bool, session_id: str
) -> bool:
    """Whether an earlier "Allow" for ``host:port`` still covers this connection.

    ``session_id`` is the session the connection is attributed to now. The answer covers the
    session it was given in and the sessions that session started (a delegated or elicitation
    child doing part of the same work); it ends with its turn, not when another session asks.
    """
    key = (workspace_id, host, port, write)
    with _LOCK:
        held = _ALLOWED.get(key)
    if held is None:
        return False
    held_session, held_turn = held
    if held_turn is not None and running_turn_id(app, held_session) != held_turn:
        with _LOCK:
            if _ALLOWED.get(key) == held:
                _ALLOWED.pop(key, None)
        return False
    return session_id == held_session or _descends_from(app, session_id, held_session)


def _descends_from(app: "FastAPI", session_id: str, ancestor: str) -> bool:
    """Whether ``ancestor`` is on ``session_id``'s chain of parent sessions."""
    sessions = getattr(app.state, "sessions", None)
    current = session_id
    for _ in range(_MAX_LINEAGE):
        if sessions is None or not current:
            return False
        try:
            record = sessions.get(current)
        except Exception:  # noqa: BLE001 - an unreadable record ends the chain
            return False
        current = str(getattr(record, "parent_session_id", "") or "") if record else ""
        if current and current == ancestor:
            return True
    return False


def hold_allow(
    app: "FastAPI",
    workspace_id: str,
    host: str,
    port: int,
    write: bool,
    session_id: str,
    *,
    for_session: bool,
) -> None:
    """Remember an "Allow" for ``host:port``: for the running turn, or for the session."""
    turn = None if for_session else running_turn_id(app, session_id)
    with _LOCK:
        _ALLOWED[(workspace_id, host, port, write)] = (session_id, turn)


def clear() -> None:
    """Forget every held answer (test isolation seam)."""
    with _LOCK:
        _DENIED.clear()
        _ALLOWED.clear()


__all__ = [
    "allowed_still",
    "clear",
    "denied_this_turn",
    "hold_allow",
    "hold_denial",
    "running_turn_id",
]
