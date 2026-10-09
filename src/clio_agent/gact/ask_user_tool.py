"""Declaration-scoped native questions with explicit blocking or async delivery."""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from clio_agent.gact import context as _ctx
from clio_agent.gact.agents.tool_instrumentation import native_tool
from clio_agent.gact.artifacts.observer_bridge import observer_call_id
from clio_agent.gact.permission_delivery import attended_session_id
from clio_agent.gact.types import UserQuestion, UserQuestionOption
from clio_agent.gact.user_question_ledger import record_user_question

PENDING_ASK_USER_META = "pending_ask_user"
_KINDS = frozenset({"freeform", "choice", "confirmation"})
_INTERNAL_FIELD_MARKER = re.compile(r"\[\[\s*##\s*[A-Za-z_][\w-]*\s*##")

#: Guards lazy creation of the per-app deadline registry (armed from tool threads,
#: cancelled from the answer/cancel routes on the event loop).
_DEADLINE_LOCK = threading.Lock()


class AskUserError(RuntimeError):
    """Raised when ``ask_user`` cannot create a valid pending interaction."""


def _validated_question(question: str) -> str:
    """Return a clean user-facing question or reject model parser residue."""

    prompt = str(question or "").strip()
    if not prompt:
        raise AskUserError("ask_user requires a non-empty question.")
    if _INTERNAL_FIELD_MARKER.search(prompt):
        raise AskUserError(
            "ask_user question contains an internal parser marker; submit a clean "
            "user-facing question."
        )
    return prompt


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


def ask_user_expires_at(requested_s: int) -> str:
    """The deadline for a question, or ``""`` when it has none.

    A question has NO default lifetime (#1448, owner rule: no deterministic
    caps): it stays pending until the user answers or cancels it, or until the
    lifetime the agent explicitly asked for (``expiresInSeconds > 0``) ends.
    """

    if requested_s <= 0:
        return ""
    return (datetime.now(timezone.utc) + timedelta(seconds=requested_s)).isoformat()


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
        surface_id: str = "",
        answer_action: str = "question.submit",
        response_mode: str = "blocking",
    ) -> str:
        """Ask the user for intent, an explanation, or a choice.

        response_mode="blocking" (default) ends this turn when the answer is needed
        to continue. response_mode="async" leaves a question open while you keep
        working; its answer arrives as a normal queued message. Supply options for
        choices, or use freeform for a text answer. Pass surface_id to present an
        existing A2UI surface with images, text, charts, or bound form controls.
        For a visual question, create that surface first using the active catalog,
        then pass its surface ID here so the visual opens with the question.
        Only the named answer_action (default question.submit) submits the surface's
        structured context and input values as the answer. Other visual controls
        do not answer the question. Each open visual question owns its surface;
        omit surface_id for text-only questions.
        Leave pause and queue explanations to the native question UI; the A2UI
        surface supplies visual context and controls, not the question lifecycle.
        """

        app = _ctx.active_app()
        session_id = _ctx.active_session_id()
        if app is None or not session_id:
            raise AskUserError("ask_user requires an active CLIO app/session context.")
        session = app.state.sessions.get(session_id)
        if session is None:
            raise AskUserError("ask_user could not resolve the active session.")
        if response_mode not in {"blocking", "async"}:
            raise AskUserError("response_mode must be blocking or async.")
        answer_action = answer_action.strip() or "question.submit"
        if surface_id:
            surface = app.state.a2ui_store.get(session_id, surface_id)
            if surface is None or surface.state == "deleted":
                raise AskUserError("surface_id must name an existing surface in this session.")
            if any(
                row.session_id == session_id
                and row.status == "pending"
                and row.metadata.get("a2ui_surface_id") == surface_id
                for row in app.state.user_questions.values()
            ):
                raise AskUserError("This surface already belongs to an open question.")
        prompt = _validated_question(question)
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
        expires_at = ask_user_expires_at(int(expiresInSeconds))
        owner = session_id
        attended = attended_session_id(app, owner)
        task_id = _task_id_for_session(app, owner)
        invocation_id = observer_call_id() or (
            f"{_ctx.active_turn_id()}:{getattr(agent_def, 'id', '')}:ask_user"
        )
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
            "tool_name": "ask_user",
            "invocation_id": invocation_id,
            "caller": {"agent_id": str(getattr(agent_def, "id", "") or "")},
            "surfaced": False,
            "a2ui_surface_id": str(surface_id or "").strip(),
            "a2ui_answer_action": answer_action.strip(),
        }
        if response_mode == "async":
            from clio_agent.gact.async_user_question import publish_async_question

            row = publish_async_question(app, pending, _ctx.active_turn_id())
            return (
                f"Async question {row.id} submitted. You can continue working. "
                "The user's answer will arrive through the message queue."
            )
        app.state.sessions.update(owner, metadata_patch={PENDING_ASK_USER_META: pending})
        return (
            "Question submitted to the user. END YOUR TURN now; the exact owning "
            "session will resume when the user responds."
        )

    return native_tool(
        ask_user,
        name="ask_user",
        presentation="specialized",
        domain="interaction",
        desc=ask_user.__doc__,
        title="Ask User",
        args={
            "response_mode": {
                "type": "string",
                "enum": ["blocking", "async"],
                "description": "blocking: pause for the answer (default); async: ask and keep working.",
            },
            "question": {"type": "string", "description": "The user-facing question."},
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
                    "Optional response window in seconds. Omit or 0: the question "
                    "stays open until the user answers or dismisses it."
                ),
            },
            "surface_id": {
                "type": "string",
                "description": (
                    "Optional: an existing A2UI surface in this session to show with the question. "
                    "The named answer_action answers with structured form values."
                ),
            },
            "answer_action": {
                "type": "string",
                "description": "Surface action that submits the answer (default: question.submit). Exploring other controls does not answer.",
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
                    "a2ui_surface_id": str(pending_raw.get("a2ui_surface_id") or ""),
                    "a2ui_answer_action": str(pending_raw.get("a2ui_answer_action") or ""),
                },
            )

        record_user_question(app, question)
        arm_ask_user_deadline(app, question)
        restored += 1
    for question in app.state.user_questions.values():
        if question.status == "pending" and question.response_mode == "async":
            arm_ask_user_deadline(app, question)
    return restored


def arm_ask_user_deadline(app: Any, question: Any) -> None:
    """Expire one surfaced native question at its declared deadline.

    The question ledger remains authoritative: the timer only attempts the same
    atomic pending-to-terminal transition used by answer/cancel. A forwarded child
    mirror is expired and relayed through the existing child-task cancellation path.

    The timer is RETAINED in the app-scoped ``ask_user_deadlines`` registry and
    cancelled by :func:`cancel_ask_user_deadline` the moment the question settles.
    An unreferenced daemon timer would otherwise stay alive for its whole TTL
    holding a closure over ``app``, and a restart
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
        from clio_agent.gact.agents.variant_close import close_for_question  # noqa: PLC0415

        close_for_question(app, updated)  # a drafts pick expires with its question
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
        from clio_agent.gact.user_question_resume import settled_question_metadata

        metadata_patch = settled_question_metadata(app, updated.session_id, updated)
        other_pending = any(
            row.id != updated.id
            and row.session_id == updated.session_id
            and row.status == "pending"
            and row.response_mode == "blocking"
            for row in app.state.user_questions.values()
        )
        root_owned = updated.owner_session_id == updated.attended_session_id
        next_status = (
            "idle"
            if root_owned
            and not other_pending
            and updated.response_mode == "blocking"
            and getattr(session, "status", "") == "waiting_user"
            else None
        )
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
        if next_status == "idle":
            app.state.redrive_message_queue(updated.session_id)

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
    "ask_user_expires_at",
    "build_ask_user_tool",
    "cancel_ask_user_deadline",
    "restore_pending_ask_user_questions",
]
