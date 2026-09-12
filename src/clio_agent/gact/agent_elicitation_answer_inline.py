"""The inline (default) answer mechanism for agent-driven elicitation (#1309, C1-S7).

Split out of :mod:`clio_agent.gact.agent_elicitation` (the cleanup program's
no-accretion rule, #775: that module is a ratchet-baselined file — new logic
goes in an owner module of its own, not appended past its recorded line
count) as its own small, focused owner for exactly one concern: ONE bounded,
tool-less completion on the session's own model (``answer_mode="inline"``,
the default -- see the parent module's docstring for why the child-turn
mechanism deadlocks and this one cannot). Moved verbatim, no behavior change.
"""

from __future__ import annotations

import logging
from typing import Any

from clio_agent.gact.agent_elicitation_context import _bounded_transcript_excerpt

logger = logging.getLogger(__name__)


def _resolve_answer_lm(app: Any) -> tuple[Any, Any]:
    """Resolve the session's OWN model + adapter for the inline answer.

    Mirrors :func:`clio_agent.gact.runtime.ai_review._resolve_reviewer_lm`. The
    inline answer runs on a fresh thread that does not inherit the parent turn's
    thread-locals, so the app's accepted main identity is the explicit fallback.
    """

    import dspy  # noqa: PLC0415

    from clio_agent.gact.runtime.ambient_lm import active_lm  # noqa: PLC0415

    caller, ambient = active_lm()
    owner = getattr(app.state, "agent", None)
    adapter = getattr(dspy.settings, "adapter", None)
    if ambient or caller is None:
        caller = getattr(owner, "_main_lm", None)
        adapter = getattr(owner, "_dspy_adapter", None)
    return caller, adapter


def _run_agent_answer_inline(app: Any, *, answer_session_id: str, prompt: str) -> str:
    """Answer the paused elicitation with ONE bounded, tool-less completion on the
    session's OWN model.

    Runs on a worker thread (dispatched via ``asyncio.to_thread``), bounded by
    the caller's ``asyncio.wait_for``. Same transcript excerpt and same
    reply/parse/validate/resolve path as the turn answerer -- only the
    fulfillment mechanism differs, so it cannot deadlock while the parent tool
    call is paused on this session.
    """

    import dspy  # noqa: PLC0415

    lm, _adapter = _resolve_answer_lm(app)
    if lm is None:
        raise RuntimeError("no LM resolved for inline agent-elicitation answer")
    seed = _bounded_transcript_excerpt(app, answer_session_id)

    # ChainOfThought, not Predict: a reasoning model needs a reasoning output
    # field, or its whole output is prose and ``answer_json`` never gets filled.

    class _AgentAnswer(dspy.Signature):
        """Answer a paused MCP tool's typed question using ONLY this conversation's
        own context. Never guess -- decline unless the conversation established it."""

        conversation: str = dspy.InputField(
            desc="Bounded excerpt of THIS session's own transcript."
        )
        instruction: str = dspy.InputField(
            desc="The paused tool's question, its answer fields, and the reply format."
        )
        answer_json: str = dspy.OutputField(
            desc='Exactly one JSON object and nothing else: {"answer": {<one key per '
            'field>}} or {"decline": true, "reason": "..."}.'
        )

    cot = dspy.ChainOfThought(_AgentAnswer)
    logger.info(
        "agent_elicitation inline answer START lm.model=%r seed_len=%d",
        getattr(lm, "model", lm),
        len(seed),
    )
    with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
        result = cot(conversation=seed, instruction=prompt)
    reply = str(getattr(result, "answer_json", "") or "").strip()
    logger.info(
        "agent_elicitation inline answer DONE reply_len=%d reply_head=%r reasoning_head=%r",
        len(reply),
        reply[:200],
        str(getattr(result, "reasoning", "") or "")[:120],
    )
    return reply
