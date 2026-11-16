"""Unattended runs: a session tree started by a script, where no person is watching.

A session created with ``unattended: true`` (``POST /v1/sessions``, what the Python
library sends) and every session spawned beneath it never wait on a person:

* **Questions** (``ask_user``, a flow's ``ctx.ask``, any Question task) are answered
  at once with :data:`NOT_AVAILABLE_REPLY`; the agent goes ahead on its own judgment.
* **Permission asks** the approval mode would put to a person are denied, typed
  ``unattended_no_person``. (The library's default mode is ``bypass``, which asks
  nothing; a person's deny rules still apply before any mode.)
* **Plugins** an agent or flow needs are trusted and added, and each trust is recorded
  on the run's root session (``unattended_trusted``) so the script can report it.

The flag, the run limits and the trusted list live in the root session's metadata
under keys a client cannot set through ``metadata`` (:data:`RESERVED_METADATA_KEYS`).
Nothing here decides for the model: it reports that no person is there.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

UNATTENDED_KEY = "unattended"
RUN_LIMITS_KEY = "run_limits"
TRUSTED_KEY = "unattended_trusted"
LIMIT_REACHED_KEY = "run_limit_reached"
#: Session metadata only the server sets: a client's ``metadata`` may not carry them
#: (``agent_flow`` comes only from the validated ``flow`` field, so lineage is not forged).
RESERVED_METADATA_KEYS = frozenset(
    {"agent_flow", UNATTENDED_KEY, RUN_LIMITS_KEY, TRUSTED_KEY, LIMIT_REACHED_KEY}
)

NOT_AVAILABLE_REPLY = (
    "The user is not available to answer: this run was started from a script and nobody "
    "is watching it. Go ahead on your best judgment, and say in your answer what you "
    "decided and why."
)
REASON_NO_PERSON = "unattended_no_person"


def reserved_keys(metadata: dict[str, Any] | None) -> list[str]:
    """The reserved keys a client tried to set through ``metadata``."""

    return sorted(RESERVED_METADATA_KEYS.intersection(metadata or {}))


def run_root(app: Any, session_id: str) -> Any:
    """The root session of ``session_id``'s tree (itself when it has no parent)."""

    sessions = getattr(getattr(app, "state", None), "sessions", None)
    if sessions is None or not session_id:
        return None
    current, seen = sessions.get(session_id), set()
    while current is not None and current.parent_session_id and current.id not in seen:
        seen.add(current.id)
        parent = sessions.get(current.parent_session_id)
        if parent is None:
            break
        current = parent
    return current


def is_unattended(app: Any, session_id: str) -> bool:
    """Whether ``session_id`` belongs to an unattended run."""

    root = run_root(app, session_id)
    return bool(root is not None and (root.metadata or {}).get(UNATTENDED_KEY))


def auto_reply(app: Any, task: Any, definition: Any) -> Any:
    """The reply to a Question posted in an unattended run, else ``None``.

    Accepted with the not-available text where the question takes free text; a
    choice-only question is declined with the same text (no option is invented).
    """

    from clio_agent.arc.task_types import QuestionReply  # noqa: PLC0415

    if not is_unattended(app, task.owner.session_id):
        return None
    free = (
        bool(definition.allow_freeform)
        and definition.requested_schema is None
        and not (definition.metadata or {}).get("question_bundle")
    )
    return QuestionReply(
        answer=NOT_AVAILABLE_REPLY,
        metadata={UNATTENDED_KEY: True},
        action="accept" if free else "decline",
    )


def refuse_ask(
    app: Any, sid: str, name: str, args: Any, approval_mode: str, *, person_only: bool = False
) -> Any:
    """A typed deny for a permission ask in an unattended run, else ``None``.

    A tool ask under ``ai-review`` is answered by its reviewer, not a person, so it is
    left to it; ``person_only`` asks (folder access) have no reviewer.
    """

    reviewed = approval_mode == "ai-review" and not person_only
    if reviewed or not is_unattended(app, sid):
        return None
    from clio_agent.gact.permission_delivery import record_resolved_permission  # noqa: PLC0415
    from clio_agent.gact.permission_gate import DenyDecision  # noqa: PLC0415

    record_resolved_permission(
        app,
        session_id=sid,
        tool_name=name,
        args=dict(args or {}),
        status="auto_denied",
        action="deny",
        summary=f"{name!r} needs a person's approval under {approval_mode!r}; nobody is there",
        reason=REASON_NO_PERSON,
    )
    return DenyDecision(
        f"{name} was denied: approval mode {approval_mode!r} would ask a person, and this "
        "run has nobody to ask. Continue without it or choose another approach."
    )


def record_trusted(app: Any, session_id: str, plugin: str, *, marketplace: str = "") -> None:
    """Record on the run's root that ``plugin`` was trusted for it (and log it)."""

    root = run_root(app, session_id)
    if root is None:
        return
    rows = list((root.metadata or {}).get(TRUSTED_KEY) or [])
    if any(row.get("plugin") == plugin for row in rows):
        return
    rows.append(
        {
            "plugin": plugin,
            "marketplace": marketplace or plugin,
            "session_id": session_id,
            "trusted_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    logger.warning(
        "unattended run %s trusted plugin %r (marketplace %r)", root.id, plugin, marketplace
    )
    app.state.sessions.update(root.id, metadata_patch={TRUSTED_KEY: rows})


__all__ = [
    "LIMIT_REACHED_KEY",
    "NOT_AVAILABLE_REPLY",
    "REASON_NO_PERSON",
    "RESERVED_METADATA_KEYS",
    "RUN_LIMITS_KEY",
    "TRUSTED_KEY",
    "UNATTENDED_KEY",
    "auto_reply",
    "is_unattended",
    "record_trusted",
    "refuse_ask",
    "reserved_keys",
    "run_root",
]
