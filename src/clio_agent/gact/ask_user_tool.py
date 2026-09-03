"""Declaration-scoped native ``ask_user`` turn-ending runtime tool."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from clio_agent.gact import context as _ctx
from clio_agent.gact.agents.tool_instrumentation import native_tool
from clio_agent.gact.permission_delivery import attended_session_id
from clio_agent.gact.types import UserQuestion, UserQuestionOption

PENDING_ASK_USER_META = "pending_ask_user"
_KINDS = frozenset({"freeform", "choice", "confirmation"})

#: In-code fallbacks for the response window. Both are config-resolved
#: (``gact.ask_user.ttl_s`` / ``gact.ask_user.max_ttl_s``) so an operator can widen
#: or tighten the window without a redeploy, mirroring
#: ``agents.child_forward_deadline_s`` in :mod:`clio_agent.gact.child_forward`.
_DEFAULT_ASK_USER_TTL_S = 600
_DEFAULT_ASK_USER_MAX_TTL_S = 86_400

#: Guards lazy creation of the per-app deadline registry (armed from tool threads,
#: cancelled from the answer/cancel routes on the event loop).
_DEADLINE_LOCK = threading.Lock()


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


def ask_user_ttl_bounds() -> tuple[int, int]:
    """Return the configured ``(default, maximum)`` response window in seconds."""

    from clio_agent import conf  # noqa: PLC0415 - avoid an import cycle at module load

    default = conf.resolve(
        "gact.ask_user.ttl_s",
        env="CLIO_ASK_USER_TTL_S",
        default=_DEFAULT_ASK_USER_TTL_S,
        cast=conf.as_int,
    )
    maximum = conf.resolve(
        "gact.ask_user.max_ttl_s",
        env="CLIO_ASK_USER_MAX_TTL_S",
        default=_DEFAULT_ASK_USER_MAX_TTL_S,
        cast=conf.as_int,
    )
    maximum = max(1, maximum)
    return max(1, min(default, maximum)), maximum


def _deadline_registry(app: Any) -> dict[str, threading.Timer]:
    """Return the app-scoped ``question_id -> Timer`` registry, creating it once."""

    with _DEADLINE_LOCK:
        registry = getattr(app.state, "ask_user_deadlines", None)
        if not isinstance(registry, dict):
            registry = {}
            app.state.ask_user_deadlines = registry
        return registry


def cancel_ask_user_deadline(app: Any, question_id: str) -> bool:
    """Cancel and forget one armed expiry timer. Returns whether one was armed.

    Called from the single terminalization point
    (:func:`~clio_agent.gact.elicitation_bridge.claim_question_transition`), so an
    answered / cancelled / expired question never leaves a live ``threading.Timer``
    holding a closure over the app for the rest of its TTL.
    """

    if not question_id:
        return False
    with _DEADLINE_LOCK:
        registry = getattr(app.state, "ask_user_deadlines", None)
        timer = registry.pop(question_id, None) if isinstance(registry, dict) else None
    if timer is None:
        return False
    timer.cancel()
    return True


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
        allowFreeform: bool = False,  # noqa: N803 - public tool schema is camelCase
        reason: str = "",
        expiresInSeconds: int = 0,  # noqa: N803 - public tool schema is camelCase
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
        default_ttl, max_ttl = ask_user_ttl_bounds()
        requested_ttl = int(expiresInSeconds)
        ttl = default_ttl if requested_ttl <= 0 else max(1, min(requested_ttl, max_ttl))
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat()
        owner = session_id
        attended = attended_session_id(app, owner)
        task_id = _task_id_for_session(app, owner)
        pending = {
            "action": "ask_user",
            "question": prompt,
            "kind": normalized_kind,
            "choices": normalized_options,
            "allow_freeform": bool(allowFreeform),
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
            "allowFreeform": {
                "type": "boolean",
                "description": "Allow a free-form answer alongside supplied choices.",
            },
            "reason": {"type": "string", "description": "Why this input is required."},
            "expiresInSeconds": {
                "type": "integer",
                "description": (
                    "Response window in seconds; 0 uses the server default "
                    "(gact.ask_user.ttl_s) and any value is clamped to "
                    "gact.ask_user.max_ttl_s."
                ),
            },
        },
    )


def restore_pending_ask_user_questions(app: Any) -> int:
    """Rehydrate surfaced native questions from durable session metadata.

    ``user_questions`` remains the live authoritative ledger.  The surfaced
    question snapshot stored on its owning session is the crash-recovery seam:
    it restores the same question identity after a process restart instead of
    manufacturing a forwarded copy or losing the interaction entirely.
    """

    restored = 0
    terminal_statuses = {"answered", "cancelled", "expired"}
    for session in app.state.sessions.list():
        session_metadata = session.metadata if isinstance(session.metadata, Mapping) else {}
        pending_raw = session_metadata.get(PENDING_ASK_USER_META)
        if not isinstance(pending_raw, Mapping) or not pending_raw.get("surfaced"):
            continue
        if str(pending_raw.get("resolved_status") or "") in terminal_statuses:
            continue
        question_id = str(
            pending_raw.get("question_id") or session_metadata.get("pending_user_question_id") or ""
        )
        if not question_id or question_id in app.state.user_questions:
            continue

        snapshot = pending_raw.get("question_record")
        question: UserQuestion | None = None
        if isinstance(snapshot, Mapping):
            try:
                question = UserQuestion.model_validate(snapshot)
            except ValueError:
                question = None
        if question is None:
            prompt = str(pending_raw.get("question") or "").strip()
            if not prompt:
                continue
            kind_raw = str(pending_raw.get("kind") or "freeform")
            kind = kind_raw if kind_raw in _KINDS else "freeform"
            options: list[UserQuestionOption] = []
            for raw_option in pending_raw.get("choices") or []:
                if not isinstance(raw_option, Mapping):
                    continue
                label = str(raw_option.get("label") or "").strip()
                if not label:
                    continue
                options.append(
                    UserQuestionOption(
                        label=label,
                        value=str(raw_option.get("value") or label),
                        description=str(raw_option.get("description") or ""),
                    )
                )
            caller = pending_raw.get("caller")
            caller = caller if isinstance(caller, Mapping) else {}
            created_at = str(
                pending_raw.get("created_at") or session.updated_at or session.created_at
            )
            question = UserQuestion(
                id=question_id,
                session_id=session.id,
                owner_session_id=str(pending_raw.get("owner_session_id") or session.id),
                attended_session_id=str(pending_raw.get("attended_session_id") or session.id),
                prompt=prompt,
                kind=kind,  # type: ignore[arg-type]
                options=options,
                allow_freeform=bool(pending_raw.get("allow_freeform", False)),
                created_at=created_at,
                updated_at=created_at,
                expires_at=str(pending_raw.get("expires_at") or ""),
                source="native",
                metadata={
                    "reason": str(pending_raw.get("reason") or ""),
                    "caller": dict(caller),
                    "task_id": str(pending_raw.get("task_id") or ""),
                    "invocation_id": str(pending_raw.get("invocation_id") or ""),
                    "resume_on_answer": True,
                    "selected_agent": str(caller.get("agent_id") or ""),
                },
            )

        app.state.user_questions[question.id] = question
        arm_ask_user_deadline(app, question)
        restored += 1
    return restored


def arm_ask_user_deadline(app: Any, question: Any) -> None:
    """Expire one surfaced native question at its declared deadline.

    The question ledger remains authoritative: the timer only attempts the same
    atomic pending-to-terminal transition used by answer/cancel. A forwarded child
    mirror is expired and relayed through the existing child-task cancellation path.

    The timer is RETAINED in the app-scoped ``ask_user_deadlines`` registry and
    cancelled by :func:`cancel_ask_user_deadline` the moment the question settles.
    An unreferenced daemon timer would otherwise stay alive for its whole TTL (up
    to ``gact.ask_user.max_ttl_s``) holding a closure over ``app``, and a restart
    that rehydrates surfaced questions would arm one more per question.
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
        )
        from clio_agent.gact.elicitation_forwarding import (  # noqa: PLC0415
            relay_forwarded_cancel,
        )
        from clio_agent.gact.events import Event  # noqa: PLC0415

        # Drop this timer's own registry slot first: the claim below cancels a
        # timer that has already fired (a harmless no-op) but the entry itself
        # must not outlive the question.
        cancel_ask_user_deadline(app, question.id)
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
    registry = _deadline_registry(app)
    with _DEADLINE_LOCK:
        previous = registry.pop(question.id, None)
        registry[question.id] = timer
    if previous is not None:
        # Re-arming the SAME question (a restart replay reaching an already-armed
        # id) must not leave the earlier timer running.
        previous.cancel()
    timer.start()


__all__ = [
    "AskUserError",
    "PENDING_ASK_USER_META",
    "arm_ask_user_deadline",
    "ask_user_ttl_bounds",
    "build_ask_user_tool",
    "cancel_ask_user_deadline",
    "restore_pending_ask_user_questions",
]
