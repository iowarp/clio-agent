"""Unattended runs: a session tree started by a script, where no person is watching.

A session created with ``unattended: true`` (``POST /v1/sessions``, what the Python
library sends) and every session spawned beneath it never wait on a person:

* **Questions** (``ask_user``, a flow's ``ctx.ask``, any Question task) are answered
  at once with :data:`NOT_AVAILABLE_REPLY`; the agent goes ahead on its own judgment.
* **Permissions** are all granted: the session runs under ``bypass`` whatever the
  request asked for, and a folder outside the workspace is granted for it. A person's
  explicit deny rules still apply. (A child whose role narrows it, such as a read-only
  reviewer, has its asks denied, typed ``unattended_no_person``, never left waiting.)
* **Plugins** an agent or flow needs are trusted and added, and each trust is recorded
  on the run's root session (``unattended_trusted``) so the script can report it.
* **A tool's own input request** (an MCP form, such as search asking to send queries
  to a source) is accepted with the defaults the tool declares, and recorded on the root (``unattended_approved``). A script run is never
  interactive: a form with no default (a key, an account) is declined with the
  not-available reply, and its value has to come from the environment or settings.

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
APPROVED_KEY = "unattended_approved"
_ELICITATION_SOURCE = "mcp_elicitation"
#: Session metadata only the server sets: a client's ``metadata`` may not carry them
#: (``agent_flow`` comes only from the validated ``flow`` field, so lineage is not forged).
RESERVED_METADATA_KEYS = frozenset(
    {"agent_flow", UNATTENDED_KEY, RUN_LIMITS_KEY, TRUSTED_KEY, LIMIT_REACHED_KEY, APPROVED_KEY}
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
    defaults = _tool_defaults(definition)
    if defaults is not None:
        record_approved(app, task.owner.session_id, definition.prompt, defaults)
        offered = {option.value for option in definition.options}
        chosen = [
            v
            for value in defaults.values()
            for v in (value if isinstance(value, list) else [value])
        ]
        selected = tuple(str(v) for v in chosen if str(v) in offered)
        return QuestionReply(metadata={**defaults}, selected_options=selected, action="accept")
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


def _tool_defaults(definition: Any) -> dict[str, Any] | None:
    """The answer a tool's input request declares as its default, else ``None``.

    Only a tool's own form (an MCP input request) whose every required field carries
    a JSON Schema ``default``: the tool says what proceeding means; CLIO picks nothing.
    """

    schema = definition.requested_schema
    if definition.source != _ELICITATION_SOURCE or not isinstance(schema, dict):
        return None
    fields = schema.get("properties")
    if not isinstance(fields, dict) or not fields:
        return None
    required = schema.get("required") or list(fields)
    if any(
        not isinstance(fields.get(name), dict) or "default" not in fields[name] for name in required
    ):
        return None
    return {
        name: spec["default"]
        for name, spec in fields.items()
        if isinstance(spec, dict) and "default" in spec
    }


def refuse_ask(app: Any, sid: str, name: str, args: Any, approval_mode: str) -> Any:
    """A typed deny for a permission ask in an unattended run, else ``None``.

    A script run runs under ``bypass``, so an ask reaches here only from a child whose
    role narrows it (a read-only reviewer): that limit is the role's, and the call is
    denied rather than left waiting for a person.
    """

    if not is_unattended(app, sid):
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


def grant_folders(app: Any, session_id: str, row: dict[str, Any]) -> bool:
    """Grant a folder-access request in an unattended run, as Allow for workspace does.

    A script run approves every permission (its deny rules were already checked); the
    folders join the workspace's permitted folders and the grant is recorded on the
    run like any approval. ``False`` outside an unattended run.
    """

    if not is_unattended(app, session_id):
        return False
    from clio_agent.gact.folder_access import apply_folder_permission  # noqa: PLC0415

    apply_folder_permission(app, row, "allow_workspace", "user")
    folders = ", ".join(row["access"]["folders"])
    record_approved(app, session_id, f"Folder access: {folders}", {"granted": "workspace"})
    return True


def record_approved(app: Any, session_id: str, prompt: str, answer: dict[str, Any]) -> None:
    """Record on the run's root that a tool's input request was approved with its defaults."""

    root = run_root(app, session_id)
    if root is None:
        return
    question = str(prompt or "").split("\n", 1)[0][:200]
    rows = list((root.metadata or {}).get(APPROVED_KEY) or [])
    rows.append(
        {
            "question": question,
            "answer": answer,
            "session_id": session_id,
            "approved_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    logger.warning("unattended run %s approved %r with %r", root.id, question, answer)
    app.state.sessions.update(root.id, metadata_patch={APPROVED_KEY: rows})


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
    "APPROVED_KEY",
    "LIMIT_REACHED_KEY",
    "NOT_AVAILABLE_REPLY",
    "REASON_NO_PERSON",
    "RESERVED_METADATA_KEYS",
    "RUN_LIMITS_KEY",
    "TRUSTED_KEY",
    "UNATTENDED_KEY",
    "auto_reply",
    "grant_folders",
    "is_unattended",
    "record_approved",
    "record_trusted",
    "refuse_ask",
    "reserved_keys",
    "run_root",
]
