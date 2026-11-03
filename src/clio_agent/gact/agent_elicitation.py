"""Agent-addressed MCP input through ordinary tool-less assignments.

Question commands arbitrate answers. The native SDK callback owns waiting and
protocol replies. No inline inference, alternate turn runner or dispatch pool.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from clio_agent.arc.task_types import CoreReference, QuestionDefinition, QuestionReply
from clio_agent.gact.agent_elicitation_context import _bounded_transcript_excerpt
from clio_agent.gact.agent_elicitation_policy import (
    _default_unhinted,
    _denied_servers,
    _enabled,
    _max_depth,
)
from clio_agent.gact.agent_elicitation_reasons import (
    AGENT_AUDIENCE_META_KEY,
    AGENT_AUDIENCE_VALUE,
    AGENT_ELICITATION_FALLBACK_DETAILS,
    FALLBACK_REASON,
    ROUTED_REASON,
    ROUTED_UNHINTED_REASON,
    AgentElicitationDecision,
)
from clio_agent.gact.agent_elicitation_reply import parse_agent_reply
from clio_agent.gact.elicitation_schema import ELICITATION_QUESTION_SOURCE
from clio_agent.injections import render as render_reminder

if TYPE_CHECKING:
    from clio_agent.gact.types import UserQuestion

logger = logging.getLogger(__name__)


def audience_hint(params: Any) -> str:
    """Return the raw ``_meta[x-clio-agent/audience]`` value on elicitation ``params``.

    Pure and total: a missing/non-mapping ``meta``, or a missing key, returns
    ``""`` -- never raises. The caller decides whether the returned string is
    the one recognized value (:data:`AGENT_AUDIENCE_VALUE`).
    """

    meta = getattr(params, "meta", None)
    if not isinstance(meta, Mapping):
        return ""
    return str(meta.get(AGENT_AUDIENCE_META_KEY) or "").strip().lower()


def _session_agent_elicitation_depth(app: Any, session_id: str) -> int:
    """The CURRENT agent-elicitation recursion depth of ``session_id`` (0 = none)."""

    sessions = getattr(app.state, "sessions", None)
    sess = sessions.get(session_id) if sessions is not None else None
    metadata = getattr(sess, "metadata", None) if sess is not None else None
    if not isinstance(metadata, Mapping):
        return 0
    try:
        return int(metadata.get("agent_elicitation_depth") or 0)
    except (TypeError, ValueError):
        return 0


def decide_routing(
    app: Any,
    *,
    mode: str,
    session_id: str,
    namespace: str,
    audience: str,
) -> AgentElicitationDecision:
    """Decide whether ONE freshly-minted elicitation question routes to the agent.

    Keys on structural facts ONLY -- the typed ``audience`` value, ``mode``,
    policy, and recursion depth -- never on question/prompt CONTENT (superseding
    principle #1: clio never keyword-matches a model's or a server's prose).

    An explicit ``audience == "agent"`` routes. An EMPTY audience also routes by
    default (:func:`_default_unhinted`, typed :data:`ROUTED_UNHINTED_REASON`)
    because fastmcp drops the ``_meta`` tag in transit. An unrecognized non-empty
    audience, or ``url`` mode, never routes.
    """

    explicit_agent = audience == AGENT_AUDIENCE_VALUE
    if not explicit_agent:
        # Unrecognized non-empty value, url mode, or strict routing -> no-op.
        # An empty audience falls through to the same policy gates below.
        if audience or mode == "url" or not _default_unhinted():
            return AgentElicitationDecision(route=False)
    if not session_id:
        return AgentElicitationDecision(route=False, reason=FALLBACK_REASON, detail="no_session")
    if not _enabled():
        return AgentElicitationDecision(
            route=False, reason=FALLBACK_REASON, detail="policy_disabled"
        )
    # F2 (owner gate review): fail CLOSED, never open, on an empty/unknown
    # namespace -- a server identity clio cannot name is a server clio cannot
    # apply the deny-list policy to, so it never routes.
    if not namespace:
        return AgentElicitationDecision(
            route=False, reason=FALLBACK_REASON, detail="unknown_server"
        )
    if namespace in _denied_servers():
        return AgentElicitationDecision(
            route=False, reason=FALLBACK_REASON, detail="policy_denied_server"
        )
    if mode == "url":
        return AgentElicitationDecision(
            route=False, reason=FALLBACK_REASON, detail="url_mode_requires_human_consent"
        )
    depth = _session_agent_elicitation_depth(app, session_id)
    if depth >= _max_depth():
        return AgentElicitationDecision(
            route=False, reason=FALLBACK_REASON, detail="recursion_depth_exceeded"
        )
    reason = ROUTED_REASON if explicit_agent else ROUTED_UNHINTED_REASON
    return AgentElicitationDecision(route=True, reason=reason, depth=depth + 1)


def routing_fields(decision: AgentElicitationDecision) -> dict[str, Any]:
    """The ``UserQuestion`` field patch for ``decision`` (``{}`` when it is a no-op)."""

    if not decision.reason:
        return {}
    fields: dict[str, Any] = {"agent_elicitation_routing": decision.reason}
    if decision.detail:
        fields["agent_elicitation_fallback_detail"] = decision.detail
    return fields


def _publish_routing_event(
    app: Any, question: "UserQuestion", decision: AgentElicitationDecision
) -> None:
    if not decision.reason:
        return
    bus = getattr(app.state, "bus", None)
    detail_message = AGENT_ELICITATION_FALLBACK_DETAILS.get(decision.detail, decision.detail)
    logger.info(
        "agent_elicitation routing reason=%s detail=%s question_id=%s session_id=%s",
        decision.reason,
        decision.detail,
        question.id,
        question.session_id,
    )
    if bus is None:
        return
    from clio_agent.gact.events import Event  # noqa: PLC0415

    bus.publish(
        Event(
            type=decision.reason,
            session_id=question.session_id,
            payload={
                "question_id": question.id,
                "reason": decision.reason,
                "detail": decision.detail,
                "detail_message": detail_message,
            },
        )
    )


async def await_question_input(
    app: Any, question_handle: str, *, namespace: str, audience: str, mode: str
) -> None:
    """Reuse one durable helper and wait for either it or the actual Question."""
    history, owner, questions = app.state.task_history, app.state.conversations, app.state.questions

    def prepare() -> tuple[str, str, str, int] | None:
        with history.serialized():
            question = questions.expire(question_handle)
            if question.raw_status != "pending":
                return None
            definition = QuestionDefinition.model_validate(history.read_content(question.details))
            decision = decide_routing(
                app,
                mode=mode,
                session_id=question.owner.session_id,
                namespace=namespace,
                audience=audience,
            )
            from clio_agent.gact.question_surface import question_row

            _publish_routing_event(app, question_row(app, question_handle), decision)
            if not decision.route:
                questions.annotate(question_handle, routing_fields(decision))
                return None
            invocation = "elicitation-helper:" + question_handle
            helper = (
                "task_"
                + hashlib.sha256(
                    (question.owner.session_id + "\0" + invocation).encode()
                ).hexdigest()
            )
            saved = definition.metadata.get("elicitation_helper_context")
            if saved is None:
                # Fixed bounded context is written before the helper association. The
                # assignment is the reminder ``mcp_elicitation`` (required).
                request = json.dumps(
                    {
                        "request": definition.prompt,
                        "schema": definition.requested_schema,
                        "context": _bounded_transcript_excerpt(app, question.owner.session_id),
                    },
                    ensure_ascii=False,
                )
                text = render_reminder(ELICITATION_QUESTION_SOURCE, {"request": request}) or ""
                reference = history.write_content(question.owner.session_id, text)
            else:
                reference = CoreReference.model_validate(saved)
                text = history.read_content(reference)
                if not isinstance(text, str):
                    raise ValueError("Helper's fixed context is malformed")
            questions.annotate(
                question_handle,
                {
                    **routing_fields(decision),
                    "audience": "agent",
                    "elicitation_helper": helper,
                    "elicitation_helper_context": reference.model_dump(mode="json"),
                },
            )
            return helper, invocation, text, decision.depth

    prepared = await history.io.run(prepare)
    question = await history.io.run(history.task_snapshot, question_handle)
    caller = owner.caller(question.owner.session_id)
    if prepared is None:
        await app.state.task_operations.wait(caller, question_handle)
        return
    helper, invocation, text, depth = prepared

    def fallback(detail: str) -> None:
        questions.annotate(
            question_handle,
            {
                "agent_elicitation_routing": FALLBACK_REASON,
                "agent_elicitation_fallback_detail": detail,
            },
        )

    try:
        session = await history.io.run(app.state.sessions.get, question.owner.session_id)
        if session is None:
            raise ValueError("Answering session is unavailable")
        await owner.spawn(
            caller,
            text,
            str(session.agent.get("id") or "main"),
            invocation,
            parent_handle=question.owner.parent_handle,
            resource_id="elicitation-helper:" + question_handle,
            session_metadata={
                "tool_allowlist": [],
                "agent_elicitation_depth": depth,
                "elicitation_question": question_handle,
            },
        )
    except ValueError:
        await history.io.run(fallback, "spawn_refused")
        await app.state.task_operations.wait(caller, question_handle)
        return

    await app.state.task_operations.wait(caller, [question_handle, helper], return_when="any")

    def propose() -> bool:
        with history.serialized():
            current = questions.expire(question_handle)
            if current.raw_status != "pending":
                return True
            assignment = history.task_snapshot(helper)
            if assignment is None or assignment.status != "completed" or assignment.result is None:
                fallback("agent_answer_error")
                return False
            output = history.read_content(assignment.result)
            parsed = parse_agent_reply(
                str(output.get("answer") or "") if isinstance(output, dict) else str(output)
            )
            if parsed is None:
                fallback("agent_answer_unparseable")
                return False
            if parsed.get("decline") or not isinstance(parsed.get("answer"), dict):
                fallback("agent_declined")
                return False
            try:
                questions.answer(
                    question_handle,
                    QuestionReply(
                        metadata=parsed["answer"], responder_session=assignment.owner.session_id
                    ),
                )
            except ValueError:
                fallback("agent_answer_schema_invalid")
                return False
            return True

    if await history.io.run(propose):
        assignment = await history.io.run(history.task_snapshot, helper)
        if assignment is not None and assignment.terminal_at is None:
            await owner.cancel(caller, helper)
        return
    # Failed/uncertain helpers are historical work, never automatically replaced.
    # The human Question remains usable and shares its original identity.
    await app.state.task_operations.wait(caller, question_handle)
