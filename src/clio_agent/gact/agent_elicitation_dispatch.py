"""The routed background task for agent-driven elicitation (#1309, C1-S7).

Split out of :mod:`clio_agent.gact.agent_elicitation` (the cleanup program's
no-accretion rule, #775: that module is a ratchet-baselined file — new logic
goes in an owner module of its own, not appended past its recorded line
count) as its own small, focused owner for exactly one concern: running the
bounded answer invocation (inline or turn, per policy) and then resolving or
falling back through the SAME atomic primitives the shared answer route
uses. Moved verbatim, no behavior change.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from clio_agent.gact.agent_elicitation_activity import stamp_agent_answer_turn
from clio_agent.gact.agent_elicitation_answer_turn import _run_agent_answer_turn
from clio_agent.gact.agent_elicitation_context import _build_answer_prompt
from clio_agent.gact.agent_elicitation_policy import (
    _OUTER_TIMEOUT_MARGIN_S,
    _answer_mode,
    _timeout_s,
)
from clio_agent.gact.agent_elicitation_reasons import (
    AGENT_ELICITATION_FALLBACK_DETAILS,
    FALLBACK_REASON,
)
from clio_agent.gact.agent_elicitation_reply import parse_agent_reply as _parse_agent_reply

if TYPE_CHECKING:  # pragma: no cover - typing only
    from clio_agent.gact.agent_elicitation_reasons import AgentElicitationDecision
    from clio_agent.gact.elicitation_schema import FormTranslation
    from clio_agent.gact.types import UserQuestion
    from clio_agent.tools.mcp_handlers import MCPInvocationContext

logger = logging.getLogger(__name__)


def _fallback(app: Any, question: "UserQuestion", detail: str, *, extra: str = "") -> None:
    """Record a typed agent-elicitation fallback WITHOUT touching question state.

    The question simply stays ``pending`` -- exactly as if audience routing had
    never applied -- so the human path is untouched: never dropped, never
    looped, the human remains the terminal fallback.
    """

    from clio_agent.gact.elicitation_bridge import stamp_question_routing_fields  # noqa: PLC0415

    stamp_question_routing_fields(
        app,
        question.id,
        agent_elicitation_routing=FALLBACK_REASON,
        agent_elicitation_fallback_detail=detail,
    )
    detail_message = AGENT_ELICITATION_FALLBACK_DETAILS.get(detail, detail)
    if extra:
        detail_message = f"{detail_message} ({extra})"
    logger.info(
        "agent_elicitation fallback reason=%s detail=%s question_id=%s session_id=%s extra=%r",
        FALLBACK_REASON,
        detail,
        question.id,
        question.session_id,
        extra,
    )
    bus = getattr(app.state, "bus", None)
    if bus is None:
        return
    from clio_agent.gact.events import Event  # noqa: PLC0415

    bus.publish(
        Event(
            type=FALLBACK_REASON,
            session_id=question.session_id,
            payload={
                "question_id": question.id,
                "reason": FALLBACK_REASON,
                "detail": detail,
                "detail_message": detail_message,
            },
        )
    )


def _publish_answered(app: Any, question: "UserQuestion") -> None:
    """Publish the ``user_question.answered`` event for an agent-resolved question.

    Shared by the accept and the forwarded-decline paths -- both resolve the
    parked call through the same atomic primitives, so both announce the same way.
    """

    bus = getattr(app.state, "bus", None)
    if bus is None:
        return
    from clio_agent.gact.events import Event  # noqa: PLC0415

    bus.publish(
        Event(
            type="user_question.answered",
            session_id=question.session_id,
            payload=question.model_dump(exclude_none=True),
        )
    )


async def _dispatch_agent_answer(
    app: Any,
    question: "UserQuestion",
    invocation: "MCPInvocationContext",
    translation: "FormTranslation | None",
    decision: "AgentElicitationDecision",
) -> None:
    """The routed background task: run the bounded answer turn, then resolve or fall back.

    Every exit path either (a) resolves the question through the SAME atomic
    primitives the shared answer route uses
    (:func:`clio_agent.gact.elicitation_bridge.claim_question_transition` +
    :func:`clio_agent.gact.elicitation_bridge.resolve_elicitation`), attributed
    ``answered_by="agent"``, or (b) calls :func:`_fallback` with a typed
    detail and returns, leaving the question pending for the human. Never
    raises -- an unexpected exception is caught, logged, and treated as a
    typed fallback (``agent_answer_error``), matching every other elicitation
    degrade in this codebase ("never silent, never a crash").
    """

    from clio_agent.gact.turn_spawn import SpawnError  # noqa: PLC0415

    answer_session_id = str(getattr(invocation, "session_id", "") or "") or question.session_id
    prompt = _build_answer_prompt(question, translation)
    timeout_s = _timeout_s()
    inline = _answer_mode() != "turn"
    try:
        if inline:
            # Deferred + resolved BY ATTRIBUTE on the hub module (never a frozen
            # top-level `from ... import _run_agent_answer_inline`): the hub
            # re-exports this name for exactly this reason, and
            # ``tests/test_gact/test_agent_elicitation.py`` monkeypatches
            # ``agent_elicitation._run_agent_answer_inline`` expecting THIS call
            # to observe the patched version.
            from clio_agent.gact import agent_elicitation as _hub  # noqa: PLC0415

            # Default: one bounded, tool-less completion on the session's own model
            # off the event loop -- cannot deadlock while THIS call is paused.
            reply_text = await asyncio.wait_for(
                asyncio.to_thread(
                    _hub._run_agent_answer_inline,
                    app,
                    answer_session_id=answer_session_id,
                    prompt=prompt,
                ),
                timeout=timeout_s + _OUTER_TIMEOUT_MARGIN_S,
            )
        else:
            reply_text = await asyncio.wait_for(
                asyncio.to_thread(
                    _run_agent_answer_turn,
                    app,
                    answer_session_id=answer_session_id,
                    prompt=prompt,
                    depth=decision.depth,
                    timeout_s=timeout_s,
                    on_spawn=lambda handle: stamp_agent_answer_turn(app, question.id, handle),
                ),
                timeout=timeout_s + _OUTER_TIMEOUT_MARGIN_S,
            )
    except SpawnError as exc:
        _fallback(app, question, "spawn_refused", extra=exc.reason)
        return
    except (asyncio.TimeoutError, TimeoutError):
        _fallback(app, question, "agent_answer_timeout")
        return
    except Exception:  # noqa: BLE001 - the whole point of this dispatcher is to fail safe
        logger.exception(
            "agent_elicitation answer turn raised question_id=%s session_id=%s",
            question.id,
            question.session_id,
        )
        _fallback(app, question, "agent_answer_error")
        return

    parsed = _parse_agent_reply(reply_text)
    if parsed is None:
        _fallback(app, question, "agent_answer_unparseable")
        return
    from clio_agent.gact.elicitation_bridge import (  # noqa: PLC0415
        claim_question_transition,
        resolve_elicitation,
    )
    from clio_agent.gact.elicitation_correlation import (  # noqa: PLC0415
        record_narrowing_disclosure,
    )

    if parsed.get("decline"):
        # A deliberate decline is a valid elicitation RESPONSE: forward it to the
        # server (action="decline") so the server applies its own fallback. The
        # human path stays reserved for genuine NON-answers
        # (error/timeout/unparseable/schema-invalid/spawn-refused).
        declined = claim_question_transition(
            app,
            question.id,
            "answered",
            answer_metadata={"elicitation_action": "decline"},
            answered_by="agent",
        )
        if declined is None:
            return
        record_narrowing_disclosure(
            str(getattr(invocation, "session_id", "") or "") or declined.session_id,
            str(getattr(invocation, "tool_name", "") or ""),
            {"declined": True, "reason": str(parsed.get("reason") or "")},
        )
        resolve_elicitation(app, declined)
        _publish_answered(app, declined)
        return
    answer_obj = parsed.get("answer")
    if not isinstance(answer_obj, Mapping):
        _fallback(app, question, "agent_answer_unparseable")
        return

    from clio_agent.gact.elicitation_schema import validate_elicitation_answer  # noqa: PLC0415

    # THE SEMANTIC FIREWALL: the agent's answer is validated against the
    # SERVER's OWN requestedSchema exactly as a human's answer is -- a value
    # shaped wrong for the server's own declared question NEVER reaches it.
    error = validate_elicitation_answer(
        question, selected_options=[], answer="", answer_metadata=dict(answer_obj)
    )
    if error is not None:
        _fallback(app, question, "agent_answer_schema_invalid", extra=error)
        return

    updated = claim_question_transition(
        app,
        question.id,
        "answered",
        selected_options=[],
        answer_metadata=dict(answer_obj),
        answered_by="agent",
    )
    if updated is None:
        # Lost the atomic transition race: a human/timeout/cancel already
        # claimed the question first. Not a failure of this feature -- the
        # question is already resolved, so there is nothing to fall back on.
        return
    # Record the narrowing BEFORE resolving, so the resumed call's result carries
    # the disclosure at the model-observation seam even if the server never
    # self-discloses.
    record_narrowing_disclosure(
        str(getattr(invocation, "session_id", "") or "") or updated.session_id,
        str(getattr(invocation, "tool_name", "") or ""),
        answer_obj,
    )
    resolve_elicitation(app, updated)
    _publish_answered(app, updated)
