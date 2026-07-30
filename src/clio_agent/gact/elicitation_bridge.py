"""MCP elicitation bridged into the CLIO HITL / questions pipeline (#1113, P1.3).

Server-initiated ``elicitation/create`` (form + url) is a **handshake-era** MCP
capability (the 2026-07-28 era removed the server->client back-channel, SEP-2577,
so ``ctx.elicit`` is only reachable on a legacy connection). This module owns the
whole bridge:

* **Schema translation** — a form-mode flat restricted JSON Schema (string /
  number / integer / boolean / enum + defaults) becomes a
  :class:`~clio_agent.gact.types.UserQuestion` (kind + options + a ``fields``
  descriptor). A non-object / non-flat / unsupported schema yields a TYPED degrade
  (never a crash) and the client declines.
* **Async-safe pause point** — the in-flight tool call parks on an
  ``asyncio.Future`` (NOT a ``threading.Event``: the handler fires on the client's
  receive loop; blocking it would deadlock the answer route that resolves it). The
  future is resolved cross-loop-safely, so a tool call on a worker-thread loop
  (``_run_external_mcp_tool_sync`` -> ``asyncio.run``) is woken by the answer route
  on the serving loop.
* **Correlation by protocol identity** — the handler is bound, PER TOOL CALL, to
  the :class:`~clio_agent.tools.mcp_handlers.MCPInvocationContext` captured where
  the call is issued (one client per call): never a client-keyed registry (proxy
  clones defeat that) nor ambient fire-time state (P1-IMPLEMENTER mandate).
* **One surface** — the question lands on the SAME ``app.state.user_questions`` +
  ``pending_user_question_id`` anchor + answer route as native asks (RULE 4, no
  parallel store); answering / cancelling resolves the parked future. A child's
  question is forwarded to the root attended session.
* **URL trust** — url-mode is NEVER pre-fetched; the full URL is shown for explicit
  consent, an untrusted origin is REJECTED (typed reason), url is advertised only
  where a trust allow-list is configured, and the consenting client MUST render it
  in a non-inspectable isolated container (``metadata.elicitation.container``).

Every degrade emits a typed reason (``stream_fallback`` style) — never silent.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlsplit

from clio_agent.gact.types import UserQuestion, UserQuestionOption
from clio_agent.tools.mcp_handlers import MCPClientCapabilities, MCPInvocationContext

logger = logging.getLogger(__name__)

__all__ = [
    "ELICITATION_QUESTION_SOURCE",
    "ELICITATION_REASONS",
    "FORWARDED_QUESTION_SOURCE",
    "FormTranslation",
    "check_url_trust",
    "deliver_forwarded_answer",
    "forward_child_question_to_parent",
    "make_elicitation_client",
    "make_elicitation_hook",
    "resolve_answered_question",
    "resolve_elicitation",
    "translate_form_schema",
]

#: ``UserQuestion.source`` stamped on every elicitation-derived question, so the
#: shared answer route can recognise an elicitation and resolve its parked call.
ELICITATION_QUESTION_SOURCE = "mcp_elicitation"

#: ``UserQuestion.source`` for a paused child's question mirrored to the parent's
#: HITL surface (the parent-forward replacing the child_requires_user_input fail).
FORWARDED_QUESTION_SOURCE = "child_forwarded"

#: Park timeout (permission-gate-style human window); expiry -> typed ``cancel``.
DEFAULT_ELICITATION_TIMEOUT_S = 600.0

#: Typed degrade/reject reason catalog (``stream_fallback`` style): a queryable
#: reason recorded when CLIO (not the user) declines/cancels — never silent.
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
}

#: JSON-Schema scalar types this form translator accepts (flat, restricted set).
_SUPPORTED_SCALAR_TYPES = frozenset({"string", "number", "integer", "boolean"})


def _record_reason(reason: str, **fields: Any) -> None:
    """Log a typed elicitation degrade/reject reason (never silent)."""

    detail = ELICITATION_REASONS.get(reason, reason)
    logger.info(
        "elicitation degraded reason=%s detail=%s %s",
        reason,
        detail,
        " ".join(f"{k}={v!r}" for k, v in fields.items()),
    )


# --- Schema translation (form mode) ---


@dataclass(frozen=True)
class FormTranslation:
    """The result of translating a form-mode elicitation schema.

    ``degrade`` is a key in :data:`ELICITATION_REASONS` when the schema cannot be
    served (non-object / non-flat / unsupported field); all other fields are then
    empty and the caller declines the elicitation with that typed reason.
    """

    kind: Literal["freeform", "choice", "confirmation"] = "freeform"
    options: list[UserQuestionOption] = field(default_factory=list)
    fields: list[dict[str, Any]] = field(default_factory=list)
    degrade: str | None = None


def _enum_options(values: Sequence[Any], names: Sequence[Any] | None) -> list[UserQuestionOption]:
    """Build one :class:`UserQuestionOption` per enum value (label from enumNames)."""

    options: list[UserQuestionOption] = []
    for index, value in enumerate(values):
        label = str(names[index]) if names and index < len(names) else str(value)
        options.append(UserQuestionOption(label=label, value=str(value)))
    return options


def translate_form_schema(requested_schema: Mapping[str, Any]) -> FormTranslation:
    """Translate a flat restricted JSON Schema into a UserQuestion shape.

    Accepts the MCP form-mode subset: a top-level ``{"type": "object",
    "properties": {...}}`` whose property values are scalar (string / number /
    integer / boolean) or enum, optionally with ``default`` / ``title`` /
    ``description``. Nesting (object / array property values) or an unsupported
    field type returns a typed :attr:`FormTranslation.degrade` instead of raising.

    Kind selection: a single boolean field -> ``confirmation``; a single enum
    field -> ``choice`` (options are the enum values); anything else ->
    ``freeform`` (the UI renders the multi-field / scalar form from the
    ``fields`` descriptor carried in the question metadata).
    """

    if not isinstance(requested_schema, Mapping) or requested_schema.get("type") != "object":
        return FormTranslation(degrade="elicitation_schema_not_object")
    properties = requested_schema.get("properties")
    if not isinstance(properties, Mapping):
        return FormTranslation(degrade="elicitation_schema_not_object")
    required = set(requested_schema.get("required") or [])

    fields: list[dict[str, Any]] = []
    for name, spec in properties.items():
        if not isinstance(spec, Mapping):
            return FormTranslation(degrade="elicitation_schema_not_flat")
        field_type = spec.get("type")
        enum = spec.get("enum")
        # A nested object/array (with or without a declared type) is not flat.
        if field_type in {"object", "array"} or "properties" in spec or "items" in spec:
            return FormTranslation(degrade="elicitation_schema_not_flat")
        if enum is None and field_type not in _SUPPORTED_SCALAR_TYPES:
            return FormTranslation(degrade="elicitation_unsupported_field_type")
        fields.append(
            {
                "name": str(name),
                "type": str(field_type or ("string" if enum is not None else "")),
                "enum": [str(v) for v in enum]
                if isinstance(enum, Sequence) and enum is not None
                else None,
                "default": spec.get("default"),
                "title": str(spec.get("title") or name),
                "description": str(spec.get("description") or ""),
                "required": str(name) in required,
            }
        )

    if len(fields) == 1:
        only = fields[0]
        if only["enum"]:
            names = properties[only["name"]].get("enumNames")
            return FormTranslation(
                kind="choice",
                options=_enum_options(only["enum"], names if isinstance(names, Sequence) else None),
                fields=fields,
            )
        if only["type"] == "boolean":
            return FormTranslation(kind="confirmation", fields=fields)
    return FormTranslation(kind="freeform", fields=fields)


# --- URL trust (url mode) ---


def _origin(url: str) -> str:
    """Return the scheme://host[:port] origin of ``url`` (lower-cased)."""

    parts = urlsplit(url)
    netloc = parts.netloc.lower()
    return f"{parts.scheme.lower()}://{netloc}" if netloc else ""


def check_url_trust(url: str, trusted_origins: Sequence[str]) -> str | None:
    """Return a typed reject reason for ``url``, or ``None`` when it is trusted.

    Trust is decided WITHOUT fetching the URL: only its origin is inspected. A
    non-https URL is rejected outright; an origin absent from ``trusted_origins``
    (a configured allow-list) is rejected. An empty allow-list means the url
    trust flow is not configured, so every url is rejected as not-declared.
    """

    if not trusted_origins:
        return "elicitation_url_not_declared"
    parts = urlsplit(url)
    if parts.scheme.lower() != "https":
        return "elicitation_url_insecure_scheme"
    allowed = {_origin(o) if "://" in o else o.lower() for o in trusted_origins}
    origin = _origin(url)
    host = parts.netloc.lower()
    if origin not in allowed and host not in allowed:
        return "elicitation_url_untrusted_origin"
    return None


# --- Async-safe park / resolve ---


@dataclass(frozen=True)
class ElicitResolution:
    """The user's decision, translated back toward an SDK ``ElicitResult``."""

    action: Literal["accept", "decline", "cancel"]
    content: dict[str, Any] | None = None


def _waiters(
    app: Any,
) -> dict[str, tuple["asyncio.Future[ElicitResolution]", asyncio.AbstractEventLoop]]:
    """Return the per-app elicitation waiter registry, created on first use.

    Lazily attached to ``app.state`` (no ``build_app`` edit needed): maps a
    pending question id to the parked future and the loop that awaits it, so a
    resolution scheduled from any thread reaches the right loop.
    """

    registry = getattr(app.state, "elicitation_waiters", None)
    if registry is None:
        registry = {}
        app.state.elicitation_waiters = registry
    return registry


def _safe_set(future: "asyncio.Future[ElicitResolution]", resolution: ElicitResolution) -> None:
    if not future.done():
        future.set_result(resolution)


async def _await_answer(app: Any, question: UserQuestion, timeout: float) -> ElicitResolution:
    """Register the waiter, publish the question, then park until it is resolved.

    Async-safe: the handler runs on the client's receive loop; this yields it (so
    the answer route can run and resolve the future) instead of blocking. The
    waiter is registered BEFORE publish, closing the race where an answer between
    publish and registration would miss it. Timeout -> fail-safe typed cancel.
    """

    loop = asyncio.get_running_loop()
    future: asyncio.Future[ElicitResolution] = loop.create_future()
    _waiters(app)[question.id] = (future, loop)
    _publish_question_created(app, question)
    try:
        return await asyncio.wait_for(future, timeout=timeout)
    except (TimeoutError, asyncio.TimeoutError):
        _record_reason("elicitation_wait_timeout", question_id=question.id)
        return ElicitResolution(action="cancel")
    finally:
        _waiters(app).pop(question.id, None)


def resolve_elicitation(app: Any, question: UserQuestion) -> bool:
    """Resolve a parked elicitation from the shared answer/cancel route.

    Returns ``True`` when ``question`` was an in-flight elicitation whose parked
    tool call was woken (cross-loop-safe), ``False`` otherwise (the caller then
    runs its normal, non-elicitation resolution). The resolution is derived from
    the already-updated question row (status + selected options / answer /
    metadata), then delivered to the parked future on its owning loop.
    """

    waiter = _waiters(app).pop(question.id, None)
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
    _clear_pending_anchor(app, question.session_id)
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
    content = _build_form_content(fields, question)
    return ElicitResolution(action="accept", content=content)


def _coerce(value: Any, field_type: str) -> Any:
    """Coerce a string-ish answer to the field's JSON-Schema scalar type."""

    if field_type in {"number", "integer"} and isinstance(value, str):
        try:
            return int(value) if field_type == "integer" else float(value)
        except ValueError:
            return value
    if field_type == "boolean" and isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1", "on", "y"}
    return value


def _build_form_content(
    fields: Sequence[Mapping[str, Any]], question: UserQuestion
) -> dict[str, Any]:
    """Build the accept ``content`` dict from the answer + the field descriptors.

    Precedence per field: an explicit value in ``answer_metadata`` (a multi-field
    form submits ``{field: value}`` there), else the single-field shorthand
    (``selected_options`` for a choice, ``answer`` for freeform/confirmation),
    else the schema default. Values are coerced to the declared scalar type.
    """

    supplied = question.answer_metadata
    content: dict[str, Any] = {}
    single = len(fields) == 1
    for spec in fields:
        name = str(spec["name"])
        field_type = str(spec.get("type") or "string")
        if name in supplied:
            content[name] = _coerce(supplied[name], field_type)
        elif single and question.selected_options:
            content[name] = _coerce(question.selected_options[0], field_type)
        elif single and question.answer != "":
            content[name] = _coerce(question.answer, field_type)
        elif spec.get("default") is not None:
            content[name] = spec["default"]
    return content


# --- The handler + client construction ---


def _clear_pending_anchor(app: Any, session_id: str) -> None:
    """Clear the durable ``pending_user_question_id`` anchor for ``session_id``."""

    sessions = getattr(app.state, "sessions", None)
    if sessions is None or not session_id:
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
) -> UserQuestion:
    from clio_agent.gact.runtime.globals import _new_question_id  # noqa: PLC0415

    now_iso = datetime.now(timezone.utc).isoformat()
    return UserQuestion(
        id=_new_question_id(),
        session_id=session_id,
        prompt=prompt,
        status="pending",
        kind=kind,  # type: ignore[arg-type]
        options=options,
        created_at=now_iso,
        updated_at=now_iso,
        source=source,
        metadata=metadata,
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


def forward_child_question_to_parent(app: Any, task: Any, child_sid: str) -> bool:
    """Forward a paused child's pending question to the parent's HITL surface.

    Replaces the deleted ``child_requires_user_input`` fail path: an unattended
    child whose turn paused for user input has its pending question mirrored onto
    the parent's (root attended) session so a human can answer. The forwarded
    question links back to the child so :func:`deliver_forwarded_answer` can relay
    the answer and resume the child. Returns ``True`` when a question was
    forwarded, ``False`` (with a typed reason) when the child had none.
    """

    child_q = _pending_question_for(app, child_sid)
    if child_q is None:
        _record_reason("child_waiting_without_question", child=child_sid)
        return False
    attended = _attended_session(app, getattr(task, "parent_session_id", "") or child_sid)
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
        },
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
    return True


def deliver_forwarded_answer(app: Any, deps: Any, forwarded: UserQuestion) -> None:
    """Relay a forwarded parent answer to the child question and resume the child.

    The parent-facing forwarded question has been answered; copy that answer onto
    the child's pending question and, when it was a resumable ask, kick the child's
    resume turn through the SAME ``start_background_user_turn`` seam the native
    answer route uses (no duplicate turn machinery).
    """

    child_sid = str(forwarded.metadata.get("forwarded_from_session") or "")
    child_qid = str(forwarded.metadata.get("forwarded_from_question") or "")
    child_q = app.state.user_questions.get(child_qid) if child_qid else None
    if child_q is None:
        _record_reason("forwarded_child_question_gone", child=child_sid, question=child_qid)
        return
    answered = child_q.model_copy(
        update={
            "status": "answered",
            "answer": forwarded.answer,
            "selected_options": list(forwarded.selected_options),
            "answer_metadata": dict(forwarded.answer_metadata),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    app.state.user_questions[child_qid] = answered
    child_sess = app.state.sessions.get(child_sid) if child_sid else None
    if child_sess is None or not answered.metadata.get("resume_on_answer"):
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

    The single body behind the wired elicitation hook. ``params`` is the SDK
    ``ElicitRequestFormParams`` or ``ElicitRequestURLParams``. A schema/url
    degrade returns a typed decline; a timeout returns a cancel; a user answer
    returns the accept content. Never raises for a serveable request.
    """

    session_id = invocation.session_id or ""
    if not session_id:
        _record_reason("elicitation_no_session", tool=invocation.tool_name)
        return _build_elicit_result(ElicitResolution(action="decline"))

    # Child forwarding (adopted default): an unattended spawned child cannot answer
    # its own elicitation, so the question is minted on the ROOT attended session's
    # HITL surface. The parked future stays keyed by the question id, so the parent
    # user's answer wakes THIS child's tool call (no client-keyed registry).
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
                    # The client MUST render this in an isolated, non-inspectable
                    # container (ephemeral profile, no shared cookies/session, no
                    # referrer) so the server learns nothing from the rendering.
                    "container": "isolated",
                    "request_id": getattr(params, "request_id", None),
                    "namespace": invocation.namespace,
                    "tool_name": invocation.tool_name,
                    "invocation_id": invocation.invocation_id,
                    "forwarded_from_session": forwarded_from,
                },
            },
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
                    "request_id": getattr(params, "request_id", None),
                    "namespace": invocation.namespace,
                    "tool_name": invocation.tool_name,
                    "invocation_id": invocation.invocation_id,
                    "forwarded_from_session": forwarded_from,
                },
            },
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

    The returned coroutine matches
    :class:`~clio_agent.tools.mcp_handlers.ElicitationHook`; the ``context``
    argument the dispatcher supplies is ignored because this closure already
    carries its invocation context (one client per tool call), which is the
    correct correlation on the per-call execution path.
    """

    async def hook(
        context: Any,
        message: str,
        response_type: Any,
        params: Any,
        request_context: Any,
    ) -> Any:
        return await handle_elicitation(
            app, invocation, message, params, url_trusted_origins=url_trusted_origins
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


def _set_legacy_mode(client: Any) -> None:
    """Connect the elicitation client in the handshake era.

    Server-initiated elicitation is unavailable on the 2026-07-28 era (SEP-2577
    removed the back-channel), so a client that wires an elicitation handler must
    speak the legacy handshake protocol or the handler can never fire. This is a
    deliberate, documented era selection, not a silent fallback.
    """

    if hasattr(client, "mode"):
        client.mode = "legacy"


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

    Wires the elicitation handler (bound to ``invocation`` — the correlation
    identity), declares the elicitation capability at exactly the served
    granularity (form always; url only when a trust allow-list is configured, so
    url is never over-advertised), and connects in the handshake era so the
    server can actually elicit. When ``invocation`` is omitted, it is built from
    ``namespace`` / ``tool_name`` and the session currently driving the turn.
    """

    from clio_agent.tools.mcp_runtime import MCPClientHandlers, make_mcp_client  # noqa: PLC0415

    origins = _resolve_trusted_origins(url_trusted_origins)
    if invocation is None:
        from clio_agent.gact.runtime.globals import _resolve_tool_session  # noqa: PLC0415

        sid, _current = _resolve_tool_session(app)
        invocation = MCPInvocationContext(
            invocation_id=f"{namespace}.{tool_name}" if namespace or tool_name else "elicit",
            session_id=sid,
            namespace=namespace or None,
            tool_name=tool_name or None,
        )
    hook = make_elicitation_hook(app, invocation, url_trusted_origins=origins)
    capabilities = MCPClientCapabilities(elicitation_form=True, elicitation_url=bool(origins))
    client = make_mcp_client(
        transport,
        handlers=MCPClientHandlers(elicitation=hook),
        capabilities=capabilities,
        client_cls=client_cls,
    )
    _set_legacy_mode(client)
    return client
