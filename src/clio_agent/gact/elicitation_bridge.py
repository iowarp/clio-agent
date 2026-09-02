"""MCP elicitation bridged into the CLIO HITL / questions pipeline (#1113, P1.3).

Server-initiated ``elicitation/create`` (form + url) is a **handshake-era** MCP
capability (the 2026-07-28 era removed the server->client back-channel, SEP-2577,
so ``ctx.elicit`` is only reachable on a legacy connection). This module owns the
whole bridge:

* **Schema translation** (in ``elicitation_schema``) — a flat restricted form schema
  becomes a :class:`~clio_agent.gact.types.UserQuestion`; a non-object / non-flat /
  unsupported schema yields a TYPED degrade (never a crash) and the client declines.
* **Async-safe pause point** — the in-flight tool call parks on a ``shield``-ed
  ``asyncio.Future`` (NOT a ``threading.Event``: the handler fires on the receive
  loop; blocking it would deadlock the answer route). It is resolved cross-loop-safely,
  and a single atomic status transition (:func:`claim_question_transition`) arbitrates
  answer-vs-timeout so exactly one wins.
* **Correlation by protocol identity** — the per-call client binds its invocation at
  construction; shared/cloned clients (gateway/executor) resolve at the receive loop
  via ``elicitation_correlation`` (never a client-keyed registry / ambient state).
* **One surface** — the question lands on the SAME ``app.state.user_questions`` +
  ``pending_user_question_id`` anchor + answer route as native asks (RULE 4, no
  parallel store). A child's question is forwarded to the root attended session.
* **URL trust** — url-mode is NEVER pre-fetched; the full URL is shown for explicit
  consent, an untrusted origin is REJECTED (typed reason), url is advertised only
  where a trust allow-list is configured, and the consenting client MUST render it
  in a non-inspectable isolated container (``metadata.elicitation.container``).

Every degrade emits a typed reason (``stream_fallback`` style) — never silent.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Literal

from clio_agent.gact.elicitation_schema import (
    ELICITATION_QUESTION_SOURCE,
    FormTranslation,
    build_form_content,
    check_elicitation_answer,
    check_url_trust,
    translate_form_schema,
    validate_elicitation_answer,
)
from clio_agent.gact.types import UserQuestion, UserQuestionOption
from clio_agent.tools.mcp_handlers import MCPClientCapabilities, MCPInvocationContext

logger = logging.getLogger(__name__)

__all__ = [
    "ELICITATION_QUESTION_SOURCE",
    "ELICITATION_REASONS",
    "FORWARDED_QUESTION_SOURCE",
    "FormTranslation",
    "check_elicitation_answer",
    "check_url_trust",
    "claim_question_transition",
    "deliver_forwarded_answer",
    "forward_child_question_to_parent",
    "make_elicitation_client",
    "make_elicitation_hook",
    "relay_forwarded_cancel",
    "resolve_answered_question",
    "resolve_cancelled_question",
    "resolve_elicitation",
    "translate_form_schema",
    "validate_elicitation_answer",
]

#: ``UserQuestion.source`` for a paused child's question mirrored to the parent's
#: HITL surface (the parent-forward replacing the child_requires_user_input fail).
FORWARDED_QUESTION_SOURCE = "child_forwarded"

#: Park timeout (permission-gate-style human window); expiry -> typed ``cancel``.
DEFAULT_ELICITATION_TIMEOUT_S = 600.0

#: Typed degrade/reject reason catalog (``stream_fallback`` style) — never silent.
ELICITATION_REASONS: dict[str, str] = {
    "elicitation_no_session": "no CLIO session resolved for the elicitation; declined",
    "elicitation_schema_not_object": "form schema is not a flat JSON object schema; declined",
    "elicitation_schema_not_flat": "form schema nests objects/arrays (not flat); declined",
    "elicitation_unsupported_field_type": "form schema field type is unsupported; declined",
    "elicitation_url_not_declared": "url-mode elicitation arrived but url was not advertised; declined",
    "elicitation_url_untrusted_origin": "url-mode elicitation origin is not on the trust list; declined",
    "elicitation_url_insecure_scheme": "url-mode elicitation is not https; declined",
    "elicitation_wait_timeout": "no answer within the elicitation window; cancelled",
    "elicitation_unknown_mode": "elicitation mode is neither form nor url; declined",
    "child_waiting_without_question": "child paused for input but has no pending question to forward",
    "forwarded_child_question_gone": "forwarded parent answer arrived but the child question is gone",
    "forwarded_child_not_resumable": "forwarded child question is not resumable; task terminated",
}


def _record_reason(reason: str, **fields: Any) -> None:
    """Log a typed elicitation degrade/reject reason (never silent)."""

    detail = ELICITATION_REASONS.get(reason, reason)
    logger.info(
        "elicitation degraded reason=%s detail=%s %s",
        reason,
        detail,
        " ".join(f"{k}={v!r}" for k, v in fields.items()),
    )


# --- Async-safe park / resolve ---


@dataclass(frozen=True)
class ElicitResolution:
    """The user's decision, translated back toward an SDK ``ElicitResult``."""

    action: Literal["accept", "decline", "cancel"]
    content: dict[str, Any] | None = None


#: Guards lazy creation of the per-app waiter registry AND every register/pop, so
#: two worker loops racing the first elicitation cannot create rival dicts (#1113
#: finding 4). The registry is a plain dict once created; all mutation holds this.
_WAITERS_LOCK = threading.Lock()

_WaiterEntry = tuple["asyncio.Future[ElicitResolution]", asyncio.AbstractEventLoop]


def _waiters(app: Any) -> dict[str, _WaiterEntry]:
    """Return the per-app elicitation waiter registry (thread-safe first use).

    Maps a pending question id to the parked future and the loop that awaits it.
    Double-checked locking makes concurrent first registrations converge on ONE
    dict rather than overwriting ``app.state`` with rival dicts (finding 4).
    """

    registry = getattr(app.state, "elicitation_waiters", None)
    if registry is None:
        with _WAITERS_LOCK:
            registry = getattr(app.state, "elicitation_waiters", None)
            if registry is None:
                registry = {}
                app.state.elicitation_waiters = registry
    return registry


def _register_waiter(app: Any, question_id: str, entry: _WaiterEntry) -> None:
    registry = _waiters(app)  # resolve/create OUTSIDE the item lock (non-reentrant)
    with _WAITERS_LOCK:
        registry[question_id] = entry


def _pop_waiter(app: Any, question_id: str) -> _WaiterEntry | None:
    registry = _waiters(app)  # resolve/create OUTSIDE the item lock (non-reentrant)
    with _WAITERS_LOCK:
        return registry.pop(question_id, None)


def _safe_set(future: "asyncio.Future[ElicitResolution]", resolution: ElicitResolution) -> None:
    if not future.done():
        future.set_result(resolution)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


#: Serializes the ONE atomic status transition of any user question (answer / cancel /
#: timeout / forwarded expiry): first out of ``pending`` wins, closing the race where an
#: answer and a timeout both "won" (HTTP 200 while the tool got cancel) — #1113 finding 6.
_QUESTIONS_LOCK = threading.Lock()


def claim_question_transition(
    app: Any,
    question_id: str,
    new_status: str,
    *,
    answer: str = "",
    selected_options: Sequence[str] | None = None,
    answer_metadata: Mapping[str, Any] | None = None,
) -> UserQuestion | None:
    """Atomically transition a PENDING question to ``new_status`` (first-wins).

    The single serialization point for every terminalization. Returns the updated row
    when THIS caller made the ``pending`` -> ``new_status`` transition; ``None`` when
    the question is absent or already non-pending (the loser). Answer fields apply
    inside the same lock, so a timeout cannot overwrite an accepted answer (or v.v.).
    """

    with _QUESTIONS_LOCK:
        questions = getattr(app.state, "user_questions", None)
        if questions is None:
            return None
        row = questions.get(question_id)
        if row is None or row.status != "pending":
            return None
        update: dict[str, Any] = {"status": new_status, "updated_at": _now_iso()}
        if new_status == "answered":
            update["answer"] = answer
            update["selected_options"] = list(selected_options or [])
            update["answer_metadata"] = dict(answer_metadata or {})
        updated = row.model_copy(update=update)
        questions[question_id] = updated
        return updated


async def _await_answer(app: Any, question: UserQuestion, timeout: float) -> ElicitResolution:
    """Register the waiter, publish the question, then park until it is resolved.

    Async- AND race-safe: the waiter is registered BEFORE publish (no gap), and the
    future is ``shield``-ed so a ``wait_for`` timeout cancels only the wait, not the
    future the answer route cross-loop resolves. The race arbiter is the atomic
    :func:`claim_question_transition`: on timeout, claim ``expired`` — win -> typed
    cancel; lose (answer already claimed ``answered``) -> await the shielded future
    for the delivered result (finding 6).
    """

    loop = asyncio.get_running_loop()
    future: asyncio.Future[ElicitResolution] = loop.create_future()
    _register_waiter(app, question.id, (future, loop))
    _publish_question_created(app, question)
    try:
        return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
    except (TimeoutError, asyncio.TimeoutError):
        if _terminalize_question(app, question.id, "expired", "user_question.expired"):
            _record_reason("elicitation_wait_timeout", question_id=question.id)
            return ElicitResolution(action="cancel")
        # The answer won the atomic transition; its result is (being) delivered onto
        # the shielded, still-live future — await it so the tool gets the real answer.
        return await future
    except asyncio.CancelledError:
        # Outer tool failure / loop teardown / cancel cascade: claim cancelled + re-raise.
        _terminalize_question(app, question.id, "cancelled", "user_question.cancelled")
        raise
    finally:
        _pop_waiter(app, question.id)


def _terminalize_question(app: Any, question_id: str, status: str, event_type: str) -> bool:
    """Atomically move a still-pending question to a terminal state; publish + clear.

    Returns ``True`` when THIS caller won the transition (emitted the typed event +
    cleared the anchor), ``False`` when it lost to a concurrent answer/cancel — so a
    late answer hits the route's 409 guard (finding 6).
    """

    updated = claim_question_transition(app, question_id, status)
    if updated is None:
        return False
    _clear_pending_anchor(app, updated.session_id, only_if=question_id)
    bus = getattr(app.state, "bus", None)
    if bus is not None:
        from clio_agent.gact.events import Event  # noqa: PLC0415

        bus.publish(
            Event(
                type=event_type,
                session_id=updated.session_id,
                payload=updated.model_dump(exclude_none=True),
            )
        )
    return True


def resolve_elicitation(app: Any, question: UserQuestion) -> bool:
    """Resolve a parked elicitation from the shared answer/cancel route.

    Returns ``True`` when ``question`` was an in-flight elicitation whose parked call
    was woken (cross-loop-safe), ``False`` otherwise. The resolution is derived from
    the already-updated row and delivered to the parked future on its owning loop.
    """

    waiter = _pop_waiter(app, question.id)
    if waiter is None:
        return False
    future, loop = waiter
    resolution = _resolution_from_question(question)
    try:
        loop.call_soon_threadsafe(_safe_set, future, resolution)
    except RuntimeError as exc:  # owning loop already closed (worker torn down)
        logger.warning(
            "elicitation resolve dropped reason=owner_loop_closed question_id=%s error=%r",
            question.id,
            exc,
        )
        return False
    _clear_pending_anchor(app, question.session_id, only_if=question.id)
    return True


def resolve_answered_question(app: Any, deps: Any, sid: str, question: UserQuestion) -> bool:
    """Dispatch an answered question that skips the default resume/idle path.

    Owns the two answer-route branches whose resolution is neither a turn resume
    nor an idle transition: a plan-exit approval (mode transition + constraint
    lift + resume, delegated to ``plan_mode``) and an in-flight MCP elicitation
    (wake the parked tool call). Consolidating them here keeps ``routes/sessions``
    off further accretion. Returns ``True`` when it handled the question and
    published ``user_question.answered`` (the route then just returns the row);
    ``False`` leaves the route to run its normal ask-user resolution.
    """

    from clio_agent.gact.events import Event  # noqa: PLC0415

    handled = False
    if question.metadata.get("plan_exit_approval"):
        from clio_agent.gact.plan_mode import resolve_plan_exit_answer  # noqa: PLC0415

        resolve_plan_exit_answer(app, deps, sid, question)
        handled = True
    elif resolve_elicitation(app, question):
        handled = True
    elif question.metadata.get("forwarded_from_question"):
        deliver_forwarded_answer(app, deps, question)
        handled = True
    if handled:
        app.state.bus.publish(
            Event(
                type="user_question.answered",
                session_id=sid,
                payload=question.model_dump(exclude_none=True),
            )
        )
    return handled


def _resolution_from_question(question: UserQuestion) -> ElicitResolution:
    """Map an answered/cancelled UserQuestion to an SDK-shaped resolution."""

    if question.status == "cancelled":
        return ElicitResolution(action="cancel")
    elicitation = question.metadata.get("elicitation") or {}
    # An explicit decline rides the answer metadata (the user rejected the ask).
    if str(question.answer_metadata.get("elicitation_action") or "") == "decline":
        return ElicitResolution(action="decline")
    if elicitation.get("mode") == "url":
        # URL consent carries no form content; accept == the user consented.
        return ElicitResolution(action="accept", content={})
    fields = elicitation.get("fields") or []
    content = build_form_content(
        fields,
        selected_options=question.selected_options,
        answer=question.answer,
        answer_metadata=question.answer_metadata,
    )
    return ElicitResolution(action="accept", content=content)


# --- The handler + client construction ---


def _clear_pending_anchor(app: Any, session_id: str, *, only_if: str = "") -> None:
    """Clear the durable ``pending_user_question_id`` anchor for ``session_id``.

    ``only_if`` guards against clobbering a newer pending question: the anchor is
    cleared only when it still points at ``only_if`` (when given).
    """

    sessions = getattr(app.state, "sessions", None)
    if sessions is None or not session_id:
        return
    if only_if:
        sess = sessions.get(session_id)
        anchor = str((getattr(sess, "metadata", {}) or {}).get("pending_user_question_id") or "")
        if anchor != only_if:
            return
    try:
        sessions.update(session_id, metadata_patch={"pending_user_question_id": ""})
    except Exception as exc:  # noqa: BLE001 - anchor bookkeeping must never fail a resolve
        logger.debug("pending anchor clear skipped session_id=%s error=%r", session_id, exc)


def _publish_question_created(app: Any, question: UserQuestion) -> None:
    """Set the pending anchor + publish ``user_question.created`` (one surface)."""

    app.state.user_questions[question.id] = question
    sessions = getattr(app.state, "sessions", None)
    if sessions is not None:
        try:
            sessions.update(
                question.session_id,
                metadata_patch={"pending_user_question_id": question.id},
            )
        except Exception as exc:  # noqa: BLE001 - anchor bookkeeping is best-effort
            logger.debug(
                "pending anchor set skipped session_id=%s error=%r", question.session_id, exc
            )
    bus = getattr(app.state, "bus", None)
    if bus is not None:
        from clio_agent.gact.events import Event  # noqa: PLC0415

        bus.publish(
            Event(
                type="user_question.created",
                session_id=question.session_id,
                payload=question.model_dump(exclude_none=True),
            )
        )


def _new_question(
    session_id: str,
    *,
    prompt: str,
    kind: str,
    options: list[UserQuestionOption],
    metadata: dict[str, Any],
    source: str = ELICITATION_QUESTION_SOURCE,
    owner_session_id: str = "",
    attended_session_id: str = "",
) -> UserQuestion:
    from clio_agent.gact.runtime.globals import _new_question_id  # noqa: PLC0415

    now_iso = datetime.now(timezone.utc).isoformat()
    return UserQuestion(
        id=_new_question_id(),
        session_id=session_id,
        owner_session_id=owner_session_id or session_id,
        attended_session_id=attended_session_id or session_id,
        prompt=prompt,
        status="pending",
        kind=kind,  # type: ignore[arg-type]
        options=options,
        created_at=now_iso,
        updated_at=now_iso,
        source=source,
        metadata=metadata,
    )


def invocation_with_request_correlation(
    invocation: MCPInvocationContext, request_context: Any
) -> MCPInvocationContext:
    """Add authoritative SEP-2663 task/input identity carried by a callback request.

    ``mcp_tasks`` names task elicitation requests ``task-{task_id}-{input_key}``.
    Task ids may contain dashes, so this resolves the prefix against the existing
    durable task store instead of splitting an ambiguous string heuristically.
    """

    request_id = str(getattr(request_context, "request_id", "") or "")
    if not request_id.startswith("task-") or not invocation.session_id:
        return invocation
    try:
        from clio_agent.tools.mcp_task_records import iter_task_records  # noqa: PLC0415

        candidates = [
            record
            for record in iter_task_records()
            if record.session_id == invocation.session_id
            and request_id.startswith(f"task-{record.task_id}-")
        ]
    except Exception:  # noqa: BLE001 - correlation enrichment must not reject elicitation
        return invocation
    if len(candidates) != 1:
        return invocation
    task_id = candidates[0].task_id
    return replace(
        invocation,
        task_id=task_id,
        input_key=request_id.removeprefix(f"task-{task_id}-") or None,
    )


def _attended_session(app: Any, session_id: str) -> str:
    """Walk the ``parent_session_id`` chain to the top human-attended session.

    A spawned child cannot answer its own HITL prompt, so a child question is
    surfaced on the ROOT session a human is attending. Cycle-guarded; returns
    ``session_id`` unchanged when there is no parent chain or no session store.
    """

    sessions = getattr(app.state, "sessions", None)
    if sessions is None:
        return session_id
    seen: set[str] = set()
    sid = session_id
    while sid and sid not in seen:
        seen.add(sid)
        sess = sessions.get(sid)
        parent = str(getattr(sess, "parent_session_id", "") or "") if sess is not None else ""
        if not parent:
            return sid
        sid = parent
    return sid


def _pending_question_for(app: Any, session_id: str) -> UserQuestion | None:
    """Return a session's pending question (anchor first, else newest pending)."""

    questions = getattr(app.state, "user_questions", {}) or {}
    sess = app.state.sessions.get(session_id) if getattr(app.state, "sessions", None) else None
    anchor = str((getattr(sess, "metadata", {}) or {}).get("pending_user_question_id") or "")
    row = questions.get(anchor) if anchor else None
    if row is not None and row.status == "pending":
        return row
    pending = [
        q for q in questions.values() if q.session_id == session_id and q.status == "pending"
    ]
    pending.sort(key=lambda q: q.created_at, reverse=True)
    return pending[0] if pending else None


def forward_child_question_to_parent(app: Any, task: Any, child_sid: str) -> str | None:
    """Forward a paused child's pending question to the parent's HITL surface.

    Mirrors an unattended child's pending question onto the parent's (root attended)
    session, linked back so :func:`deliver_forwarded_answer` / :func:`relay_forwarded_cancel`
    relay the resolution. Returns the forwarded question id, or ``None`` (typed reason)
    when the child had none — the caller then terminates the task typed (replaces the
    deleted ``child_requires_user_input`` fail path).
    """

    child_q = _pending_question_for(app, child_sid)
    if child_q is None:
        _record_reason("child_waiting_without_question", child=child_sid)
        return None
    attended = _attended_session(app, getattr(task, "parent_session_id", "") or child_sid)
    child_elicitation = child_q.metadata.get("elicitation")
    child_elicitation = child_elicitation if isinstance(child_elicitation, Mapping) else {}
    forwarded = _new_question(
        attended,
        prompt=child_q.prompt,
        kind=child_q.kind,
        options=list(child_q.options),
        source=FORWARDED_QUESTION_SOURCE,
        metadata={
            "forwarded_from_session": child_sid,
            "forwarded_from_question": child_q.id,
            "task_id": getattr(task, "task_id", ""),
            "invocation_id": child_q.metadata.get("invocation_id", "")
            or child_elicitation.get("invocation_id", ""),
        },
        owner_session_id=child_sid,
        attended_session_id=attended,
    )
    _publish_question_created(app, forwarded)
    bus = getattr(app.state, "bus", None)
    if bus is not None:
        from clio_agent.gact.events import Event  # noqa: PLC0415

        bus.publish(
            Event(
                type="user_question.forwarded",
                session_id=attended,
                payload={
                    "question_id": forwarded.id,
                    "forwarded_from_session": child_sid,
                    "forwarded_from_question": child_q.id,
                    "task_id": getattr(task, "task_id", ""),
                },
            )
        )
    return forwarded.id


def relay_forwarded_cancel(
    app: Any, forwarded: UserQuestion, *, reason: str = "child_forward_declined"
) -> bool:
    """Relay a parent-side cancel of a forwarded question down to the child + task.

    Cancels the mirrored child question and fails the bound AgentTask with a typed
    ``reason``, so a declined/cancelled/expired forward never leaves the child waiting
    or its slot pinned. Returns ``True`` when the row was a forwarded mirror.
    """

    child_qid = str(forwarded.metadata.get("forwarded_from_question") or "")
    task_id = str(forwarded.metadata.get("task_id") or "")
    if not child_qid and not task_id:
        return False
    if child_qid:
        claim_question_transition(app, child_qid, "cancelled")  # atomic, no-op if resolved
    if task_id:
        from clio_agent.gact.child_forward import fail_forwarded_child_task  # noqa: PLC0415

        fail_forwarded_child_task(app, task_id, reason)
    return True


def resolve_cancelled_question(app: Any, question: UserQuestion) -> bool:
    """Resolve a cancelled question that is an elicitation or a forwarded mirror.

    Shared cancel route: an in-flight elicitation wakes its parked call (typed cancel);
    a forwarded mirror relays the cancel to the child + fails the task. Returns ``True``
    when handled (route skips the idle transition), ``False`` for an ordinary ask.
    """

    if resolve_elicitation(app, question):
        return True
    if (
        question.metadata.get("forwarded_from_question")
        or question.source == FORWARDED_QUESTION_SOURCE
    ):
        return relay_forwarded_cancel(app, question)
    return False


def deliver_forwarded_answer(app: Any, deps: Any, forwarded: UserQuestion) -> None:
    """Relay a forwarded parent answer to the child question via the OWNER path.

    Applies the answer atomically then dispatches through the SAME
    :func:`resolve_answered_question` the route uses (plan-exit -> mode switch,
    elicitation -> wake parked call, ordinary ask -> resume). The task is then bound to
    the outcome via :func:`~clio_agent.gact.child_forward.settle_or_attach_forwarded_task`
    (turn -> settle at its completion; no turn / exit_only -> SUCCESS terminal + admit).
    Every unresumable edge terminalizes the task typed — the slot is never leaked (finding 5).
    """

    child_sid = str(forwarded.metadata.get("forwarded_from_session") or "")
    child_qid = str(forwarded.metadata.get("forwarded_from_question") or "")
    task_id = str(forwarded.metadata.get("task_id") or "")

    def _terminate(reason: str) -> None:
        _record_reason(reason, child=child_sid, question=child_qid)
        if task_id:
            from clio_agent.gact.child_forward import fail_forwarded_child_task  # noqa: PLC0415

            fail_forwarded_child_task(app, task_id, "child_forward_not_resumable")

    def _settle_or_attach(tid: str) -> None:
        if tid:
            from clio_agent.gact.child_forward import (
                settle_or_attach_forwarded_task,  # noqa: PLC0415
            )

            settle_or_attach_forwarded_task(app, tid)

    answered = claim_question_transition(
        app,
        child_qid,
        "answered",
        answer=forwarded.answer,
        selected_options=list(forwarded.selected_options),
        answer_metadata=dict(forwarded.answer_metadata),
    )
    if answered is None:
        _terminate("forwarded_child_question_gone")
        return
    # Owner-specific resolution (plan-exit / elicitation) — same dispatcher as the route.
    if resolve_answered_question(app, deps, child_sid, answered):
        # Bind the task to the outcome: if a child turn launched (resume / plan-exit
        # that resumes) settle at its completion; if none launched (plan-exit exit_only —
        # answered + honored, child idle) terminalize SUCCESS + admit, never leak the slot.
        _settle_or_attach(task_id)
        return
    child_sess = app.state.sessions.get(child_sid) if child_sid else None
    if child_sess is None or not answered.metadata.get("resume_on_answer"):
        _terminate("forwarded_child_not_resumable")
        return
    app.state.sessions.update(child_sid, metadata_patch={"pending_user_question_id": ""})
    deps.start_background_user_turn(
        child_sid,
        child_sess,
        deps.ask_user_resume_text(answered),
        metadata={
            "ask_user_question_id": answered.id,
            "ask_user_answer": answered.answer,
            "ask_user_selected_options": answered.selected_options,
            "ask_user_resume": True,
        },
        prev_status=getattr(child_sess, "status", "waiting_user"),
    )
    _settle_or_attach(task_id)  # attach _on_child_done to the resumed turn (never strand)


def _build_elicit_result(resolution: ElicitResolution) -> Any:
    from fastmcp.client.elicitation import ElicitResult  # noqa: PLC0415

    return ElicitResult(action=resolution.action, content=resolution.content)


async def handle_elicitation(
    app: Any,
    invocation: MCPInvocationContext,
    message: str,
    params: Any,
    *,
    url_trusted_origins: Sequence[str] = (),
    timeout: float = DEFAULT_ELICITATION_TIMEOUT_S,
) -> Any:
    """Mint a UserQuestion for an elicitation, park the call, return the result.

    The single body behind the wired hook. A schema/url degrade returns a typed
    decline; a timeout returns cancel; an answer returns accept content. Never raises.
    """

    session_id = invocation.session_id or ""
    if not session_id:
        _record_reason("elicitation_no_session", tool=invocation.tool_name)
        return _build_elicit_result(ElicitResolution(action="decline"))

    # Child forwarding: an unattended child cannot answer its own elicitation, so the
    # question is minted on the ROOT attended session (the parked future stays keyed
    # by question id, so the parent user's answer wakes THIS child's tool call).
    attended = _attended_session(app, session_id)
    forwarded_from = session_id if attended != session_id else ""

    mode = str(getattr(params, "mode", "") or "")
    if mode == "url":
        url = str(getattr(params, "url", "") or "")
        reject = check_url_trust(url, url_trusted_origins)
        if reject is not None:
            _record_reason(reject, url=url, tool=invocation.tool_name)
            return _build_elicit_result(ElicitResolution(action="decline"))
        question = _new_question(
            attended,
            prompt=f"{message}\n\nOpen this URL to continue: {url}",
            kind="confirmation",
            options=[],
            metadata={
                "elicitation": {
                    "mode": "url",
                    "url": url,
                    # Client MUST render in an isolated, non-inspectable container
                    # (ephemeral, no shared session/referrer) — see module docstring.
                    "container": "isolated",
                    "request_id": getattr(params, "request_id", None),
                    "namespace": invocation.namespace,
                    "tool_name": invocation.tool_name,
                    "invocation_id": invocation.invocation_id,
                    "task_id": invocation.task_id,
                    "input_key": invocation.input_key,
                    "forwarded_from_session": forwarded_from,
                },
            },
            owner_session_id=session_id,
            attended_session_id=attended,
        )
    elif mode == "form":
        translation = translate_form_schema(getattr(params, "requested_schema", {}) or {})
        if translation.degrade is not None:
            _record_reason(translation.degrade, tool=invocation.tool_name)
            return _build_elicit_result(ElicitResolution(action="decline"))
        question = _new_question(
            attended,
            prompt=message,
            kind=translation.kind,
            options=translation.options,
            metadata={
                "elicitation": {
                    "mode": "form",
                    "fields": translation.fields,
                    "additional_properties": translation.additional_properties,
                    "request_id": getattr(params, "request_id", None),
                    "namespace": invocation.namespace,
                    "tool_name": invocation.tool_name,
                    "invocation_id": invocation.invocation_id,
                    "task_id": invocation.task_id,
                    "input_key": invocation.input_key,
                    "forwarded_from_session": forwarded_from,
                },
            },
            owner_session_id=session_id,
            attended_session_id=attended,
        )
    else:
        _record_reason("elicitation_unknown_mode", mode=mode, tool=invocation.tool_name)
        return _build_elicit_result(ElicitResolution(action="decline"))

    resolution = await _await_answer(app, question, timeout)
    return _build_elicit_result(resolution)


def make_elicitation_hook(
    app: Any,
    invocation: MCPInvocationContext,
    *,
    url_trusted_origins: Sequence[str] = (),
) -> Any:
    """Build the elicitation hook bound to ONE invocation (correlation identity).

    Matches :class:`~clio_agent.tools.mcp_handlers.ElicitationHook`; ignores the
    dispatcher's ``context`` because this closure already carries its invocation (one
    client per call) — the correct correlation on the per-call execution path.
    """

    async def hook(
        context: Any,
        message: str,
        response_type: Any,
        params: Any,
        request_context: Any,
    ) -> Any:
        correlated = invocation_with_request_correlation(invocation, request_context)
        return await handle_elicitation(
            app, correlated, message, params, url_trusted_origins=url_trusted_origins
        )

    return hook


def _resolve_trusted_origins(explicit: Sequence[str] | None) -> tuple[str, ...]:
    """Resolve the url-mode trust allow-list (explicit arg else config)."""

    if explicit is not None:
        return tuple(explicit)
    from clio_agent import conf  # noqa: PLC0415

    default: list[str] = []
    configured = conf.resolve(
        "tools.mcp.elicitation.url_trusted_origins",
        env="CLIO_MCP_ELICITATION_URL_TRUSTED_ORIGINS",
        default=default,
        cast=conf.as_csv,
    )
    return tuple(configured or ())


def make_elicitation_client(
    app: Any,
    transport: Any,
    namespace: str = "",
    tool_name: str = "",
    *,
    invocation: MCPInvocationContext | None = None,
    url_trusted_origins: Sequence[str] | None = None,
    client_cls: Any = None,
) -> Any:
    """Build a per-call, elicitation-capable execution client (#1113).

    Wires the handler bound to ``invocation`` (per-call correlation); declares the
    capability at served granularity (form always; url only with a configured trust
    list). Keeps NORMAL (auto) era negotiation -- a legacy server negotiates legacy
    and the handler fires (SEP-2577). A DIRECT, unmirrored connect (#1201): classified
    + recorded under ``namespace`` the moment it connects (see mcp_connection_era.py).
    """

    from clio_agent.tools.mcp_runtime import MCPClientHandlers, make_mcp_client  # noqa: PLC0415

    origins = _resolve_trusted_origins(url_trusted_origins)
    if invocation is None:
        from clio_agent.gact.runtime.globals import _resolve_tool_session  # noqa: PLC0415

        try:
            sid = _resolve_tool_session(app)[0]
        except Exception:  # noqa: BLE001 - app-less/minimal caller: no session ctx
            sid = ""  # downstream hook emits the typed ``elicitation_no_session``
        invocation = MCPInvocationContext(
            invocation_id=f"{namespace}.{tool_name}" if namespace or tool_name else "elicit",
            session_id=sid,
            namespace=namespace or None,
            tool_name=tool_name or None,
        )
    hook = make_elicitation_hook(app, invocation, url_trusted_origins=origins)
    capabilities = MCPClientCapabilities(elicitation_form=True, elicitation_url=bool(origins))
    return make_mcp_client(
        transport,
        handlers=MCPClientHandlers(elicitation=hook),
        capabilities=capabilities,
        client_cls=client_cls,
        server_id=namespace,
    )
