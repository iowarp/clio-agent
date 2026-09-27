"""Read-only side sessions: a /btw-style aside with the parent's context.

"More details" on a selected passage opens a side conversation. It carries a
copy of the parent's history so it can answer questions about it, and it can
never change it:

* **Separate session.** The aside is its own session (``parent_session_id``
  names the parent). Its turns, messages and tool calls land in the aside only;
  nothing is appended to, steered into, or projected onto the parent's
  transcript. The parent's history is deep-copied, never aliased.
* **Read-only tool posture.** The aside carries the server-owned
  ``approval_profile`` :data:`READ_ONLY_SIDE_PROFILE` (never settable through a
  public route). The permission gate resolves such a session's calls under the
  built-in :data:`~clio_agent.gact.runtime.plan_acl.READ_ONLY_POLICY_MODE` ACL:
  a tool that DECLARES no side effects (MCP ``readOnlyHint`` without
  ``destructiveHint``, or a native catalog ``read`` tag projected from the same
  annotations) is fast-allowed by :func:`~clio_agent.gact.runtime.grant_resolver.is_read_only`
  as for every session; every other tool, including one with no annotations,
  is denied, above any user policy and regardless of approval mode, with the
  typed reason :data:`REASON_READ_ONLY_SIDE_SESSION`. Native agent tools that do
  not pass through the gate (spawn and workflow control, parent messaging) are
  not offered to the aside at all (:func:`is_read_only_side_session` is the
  predicate the tool assembly consults).
* **Ephemeral.** One aside per parent: opening a new one retires the previous
  one through the ordinary session-delete path, and the client deletes it when
  its panel closes. It is marked ``metadata.side_session.ephemeral`` so it is
  never mistaken for a conversation the person started.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

from clio_agent.runtime import trace

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.types import Session

#: Server-owned approval profile of a read-only side session.
READ_ONLY_SIDE_PROFILE = "read-only-side"

#: Typed audit reason on a tool call the read-only posture denied.
REASON_READ_ONLY_SIDE_SESSION = "read_only_side_session_denied"

#: The metadata key describing an aside.
SIDE_SESSION_METADATA_KEY = "side_session"

#: Longest selection kept on the aside's metadata (it seeds the panel header).
_SELECTION_MAX_CHARS = 4000


def is_read_only_side_session(session: Any) -> bool:
    """True when ``session`` is a read-only side session."""

    return str(getattr(session, "approval_profile", "") or "") == READ_ONLY_SIDE_PROFILE


def policy_mode(session: Any) -> str:
    """The mode the permission resolver evaluates ``session``'s tool calls under.

    A read-only side session resolves under the read-only ACL whatever its
    stored mode; every other session under its own ``mode``.
    """

    from clio_agent.gact.runtime.plan_acl import READ_ONLY_POLICY_MODE  # noqa: PLC0415

    if session is None:
        return ""
    if is_read_only_side_session(session):
        return READ_ONLY_POLICY_MODE
    return str(getattr(session, "mode", "") or "")


def policy_deny_reason(mode: str) -> str:
    """The typed audit reason for a policy deny resolved under ``mode``."""

    from clio_agent.gact.runtime.plan_acl import READ_ONLY_POLICY_MODE  # noqa: PLC0415

    return REASON_READ_ONLY_SIDE_SESSION if mode == READ_ONLY_POLICY_MODE else "policy_deny"


#: Typed refusal of a write route on a read-only side session.
REFUSAL_SIDE_SESSION_READ_ONLY = "side_session_read_only"


def refuse_side_session_write(session: Any, action: str) -> None:
    """Raise a typed 422 when ``session`` is a read-only side session.

    For the user-driven write routes a session exposes (applying the edits its
    agent proposed): an aside's proposals are answers to read, never changes to
    make, so applying them from the aside is refused. The same proposal can be
    made, and applied, in the parent conversation.
    """

    if not is_read_only_side_session(session):
        return
    from fastapi import HTTPException  # noqa: PLC0415

    from clio_agent.gact.types import ErrorEnvelope, ErrorInfo  # noqa: PLC0415

    trace.event(
        "SIDE-SESSION",
        "side_session_write_refused session=%s action=%s",
        getattr(session, "id", ""),
        action,
    )
    raise HTTPException(
        status_code=422,
        detail=ErrorEnvelope(
            error=ErrorInfo(
                error=REFUSAL_SIDE_SESSION_READ_ONLY,
                message=f"A read-only side conversation cannot {action}.",
                details={"session_id": str(getattr(session, "id", ""))},
                recoverable=False,
            )
        ).model_dump(exclude_none=True),
    )


def _selection(raw: Any) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        return {}
    text = str(raw.get("text") or "").strip()[:_SELECTION_MAX_CHARS]
    message_id = str(raw.get("message_id") or "").strip()
    out: dict[str, str] = {}
    if text:
        out["text"] = text
    if message_id:
        out["message_id"] = message_id
    return out


def side_sessions_of(app: "FastAPI", parent_session_id: str) -> list["Session"]:
    """Every live aside of ``parent_session_id``."""

    return [
        session
        for session in app.state.sessions.list()
        if session.parent_session_id == parent_session_id and is_read_only_side_session(session)
    ]


async def open_side_session(
    app: "FastAPI",
    parent: "Session",
    *,
    selection: Any,
    copy_context: Callable[[str, str], Awaitable[int]],
    retire: Callable[[str], Awaitable[Any]],
) -> "Session":
    """Retire the parent's previous aside and open a new one with its context.

    Args:
        app: The GACT app.
        parent: The session the aside is about. Must not itself be an aside.
        selection: ``{"text", "message_id"}`` the aside was opened on.
        copy_context: Copies the parent's messages and context files into the
            new session (the same copy a fork makes); returns the message count.
        retire: Deletes a previous aside through the ordinary delete path.

    Returns:
        The new aside session.
    """

    for previous in side_sessions_of(app, parent.id):
        trace.event(
            "SIDE-SESSION",
            "side_session_retired parent=%s side=%s reason=superseded",
            parent.id,
            previous.id,
        )
        await retire(previous.id)
    chosen = _selection(selection)
    side = app.state.sessions.create(
        workspace_id=parent.workspace_id,
        title=f"More details: {chosen.get('text', parent.title)[:60]}",
        parent_session_id=parent.id,
        model=dict(parent.model or {}),
        agent=dict(parent.agent or {}) or None,
        metadata={
            SIDE_SESSION_METADATA_KEY: {
                "parent_session_id": parent.id,
                "read_only": True,
                "ephemeral": True,
                "selection": chosen,
            }
        },
        approval_profile=READ_ONLY_SIDE_PROFILE,
    )
    count = await copy_context(parent.id, side.id)
    app.state.sessions.update(side.id, message_count=count)
    trace.event(
        "SIDE-SESSION",
        "side_session_opened parent=%s side=%s messages=%d",
        parent.id,
        side.id,
        count,
    )
    return app.state.sessions.get(side.id) or side


__all__ = [
    "READ_ONLY_SIDE_PROFILE",
    "REASON_READ_ONLY_SIDE_SESSION",
    "REFUSAL_SIDE_SESSION_READ_ONLY",
    "SIDE_SESSION_METADATA_KEY",
    "is_read_only_side_session",
    "open_side_session",
    "policy_deny_reason",
    "policy_mode",
    "refuse_side_session_write",
    "side_sessions_of",
]
