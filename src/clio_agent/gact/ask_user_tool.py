"""Declaration-scoped native ``ask_user`` turn-ending runtime tool."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from clio_agent.gact import context as _ctx
from clio_agent.gact.agents.tool_instrumentation import native_tool
from clio_agent.gact.permission_delivery import attended_session_id

PENDING_ASK_USER_META = "pending_ask_user"
_KINDS = frozenset({"freeform", "choice", "confirmation"})


class AskUserError(RuntimeError):
    """Raised when ``ask_user`` cannot create a valid pending interaction."""


def _task_id_for_session(app: Any, session_id: str) -> str:
    """Return the spawned-task identity owning ``session_id``, when present."""

    registry = getattr(app.state, "agent_task_registry", None)
    if registry is None:
        return ""
    matches = [
        task
        for task in registry.snapshot()
        if str(getattr(task, "child_session_id", "") or "") == session_id
    ]
    matches.sort(key=lambda task: str(getattr(task, "created_at", "") or ""), reverse=True)
    return str(getattr(matches[0], "task_id", "") or "") if matches else ""


def _normalize_options(options: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Validate and normalize the public option objects accepted by the tool."""

    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for option in options:
        if not isinstance(option, Mapping):
            raise AskUserError("ask_user options must be objects.")
        label = str(option.get("label") or "").strip()
        value = str(option.get("value") or label).strip()
        if not label or not value:
            raise AskUserError("ask_user option label and value must be non-empty.")
        if value in seen:
            raise AskUserError(f"ask_user option value is duplicated: {value!r}.")
        seen.add(value)
        normalized.append(
            {
                "label": label,
                "value": value,
                "description": str(option.get("description") or "").strip(),
            }
        )
    return normalized


def build_ask_user_tool(agent_def: Any) -> Any:
    """Build the native ask tool for an agent that explicitly declared it."""

    def ask_user(
        question: str,
        kind: str = "freeform",
        options: list[dict[str, Any]] | None = None,
        reason: str = "",
        expiresInSeconds: int = 600,  # noqa: N803 - public tool schema is camelCase
    ) -> str:
        """Ask the user one necessary question and end this turn.

        Use only when progress genuinely requires user intent or a tradeoff. Supply
        ``options`` for choice questions. This ends the turn; do not call more tools
        or continue the answer after this succeeds. The exact owning child/task is
        resumed when the attended user responds.
        """

        app = _ctx.active_app()
        session_id = _ctx.active_session_id()
        if app is None or not session_id:
            raise AskUserError("ask_user requires an active CLIO app/session context.")
        session = app.state.sessions.get(session_id)
        if session is None:
            raise AskUserError("ask_user could not resolve the active session.")
        prompt = str(question or "").strip()
        if not prompt:
            raise AskUserError("ask_user requires a non-empty question.")
        normalized_kind = str(kind or "freeform").strip().lower()
        if normalized_kind not in _KINDS:
            raise AskUserError("ask_user kind must be freeform, choice, or confirmation.")
        normalized_options = _normalize_options(list(options or []))
        if normalized_kind == "choice" and not normalized_options:
            raise AskUserError("ask_user choice questions require at least one option.")
        if normalized_kind == "confirmation" and not normalized_options:
            normalized_options = [
                {"label": "Yes", "value": "yes", "description": ""},
                {"label": "No", "value": "no", "description": ""},
            ]
        ttl = max(1, min(int(expiresInSeconds), 86_400))
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat()
        owner = session_id
        attended = attended_session_id(app, owner)
        task_id = _task_id_for_session(app, owner)
        pending = {
            "action": "ask_user",
            "question": prompt,
            "kind": normalized_kind,
            "choices": normalized_options,
            "reason": str(reason or "").strip(),
            "expires_at": expires_at,
            "owner_session_id": owner,
            "attended_session_id": attended,
            "task_id": task_id,
            "invocation_id": f"{_ctx.active_turn_id()}:{getattr(agent_def, 'id', '')}:ask_user",
            "caller": {"agent_id": str(getattr(agent_def, "id", "") or "")},
            "surfaced": False,
        }
        app.state.sessions.update(owner, metadata_patch={PENDING_ASK_USER_META: pending})
        return (
            "Question submitted to the user. END YOUR TURN now; the exact owning "
            "session will resume when the user responds."
        )

    return native_tool(
        ask_user,
        name="ask_user",
        desc=ask_user.__doc__,
        title="Ask User",
        args={
            "question": {"type": "string", "description": "The necessary user-facing question."},
            "kind": {
                "type": "string",
                "description": "Question kind: freeform, choice, or confirmation.",
            },
            "options": {
                "type": "array",
                "description": "Choice options with label, value, and optional description.",
                "items": {"type": "object"},
            },
            "reason": {"type": "string", "description": "Why this input is required."},
            "expiresInSeconds": {
                "type": "integer",
                "description": "Response window in seconds, clamped to 1..86400.",
            },
        },
    )


def arm_ask_user_deadline(app: Any, question: Any) -> None:
    """Expire one surfaced native question at its declared deadline.

    The question ledger remains authoritative: the timer only attempts the same
    atomic pending-to-terminal transition used by answer/cancel. A forwarded child
    mirror is expired and relayed through the existing child-task cancellation path.
    """

    raw_deadline = str(getattr(question, "expires_at", "") or "")
    if not raw_deadline:
        return
    try:
        deadline = datetime.fromisoformat(raw_deadline.replace("Z", "+00:00"))
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
    except ValueError:
        return
    delay = max(
        0.0, (deadline.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()
    )

    def expire() -> None:
        from clio_agent.gact.elicitation_bridge import (  # noqa: PLC0415
            claim_question_transition,
            relay_forwarded_cancel,
        )
        from clio_agent.gact.events import Event  # noqa: PLC0415

        updated = claim_question_transition(app, question.id, "expired")
        if updated is None:
            return
        forwarded = [
            row
            for row in app.state.user_questions.values()
            if row.status == "pending"
            and str(row.metadata.get("forwarded_from_question") or "") == question.id
        ]
        for mirror in forwarded:
            expired_mirror = claim_question_transition(app, mirror.id, "expired")
            if expired_mirror is not None:
                relay_forwarded_cancel(app, expired_mirror, reason="user_question_expired")
                app.state.bus.publish(
                    Event(
                        type="user_question.expired",
                        session_id=expired_mirror.session_id,
                        payload=expired_mirror.model_dump(exclude_none=True),
                    )
                )
        session = app.state.sessions.get(updated.session_id)
        metadata = getattr(session, "metadata", {}) or {}
        metadata_patch: dict[str, Any] = {"pending_user_question_id": ""}
        pending = metadata.get(PENDING_ASK_USER_META)
        if isinstance(pending, Mapping) and pending.get("question_id") == question.id:
            metadata_patch[PENDING_ASK_USER_META] = {
                **pending,
                "resolved_status": "expired",
            }
        other_pending = any(
            row.id != updated.id
            and row.session_id == updated.session_id
            and row.status == "pending"
            for row in app.state.user_questions.values()
        )
        root_owned = updated.owner_session_id == updated.attended_session_id
        next_status = "idle" if root_owned and not other_pending else None
        app.state.sessions.update(
            updated.session_id,
            status=next_status,
            metadata_patch=metadata_patch,
        )
        app.state.bus.publish(
            Event(
                type="user_question.expired",
                session_id=updated.session_id,
                payload=updated.model_dump(exclude_none=True),
            )
        )

    timer = threading.Timer(delay, expire)
    timer.daemon = True
    timer.start()


__all__ = [
    "AskUserError",
    "PENDING_ASK_USER_META",
    "arm_ask_user_deadline",
    "build_ask_user_tool",
]
