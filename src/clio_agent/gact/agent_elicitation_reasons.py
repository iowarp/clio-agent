"""Shared constants + the routing verdict type for agent-driven elicitation (#1309, C1-S7).

Split out of :mod:`clio_agent.gact.agent_elicitation` (the cleanup program's
no-accretion rule, #775: that module is a ratchet-baselined file — new logic
goes in an owner module of its own, not appended past its recorded line
count) as its own small, dependency-free owner for exactly one concern: the
typed reason catalog and the :class:`AgentElicitationDecision` verdict type
every other module in this family (the hub, the dispatcher, both answer
mechanisms) reads. Kept free of any import on a sibling ``agent_elicitation_*``
module so it can sit underneath all of them without a cycle. Moved verbatim,
no behavior change.
"""

from __future__ import annotations

from dataclasses import dataclass

#: clio's convention for the "this question is for the agent" hint (a
#: reverse-DNS vendor key on the ElicitRequest params' ``_meta``, mirroring the
#: existing ``x-clio-agent/*`` convention this repo already uses for its own
#: non-standard extensions/meta keys). Deliberately NOT the reserved
#: ``io.modelcontextprotocol/*`` namespace, which the spec owns.
AGENT_AUDIENCE_META_KEY = "x-clio-agent/audience"

#: The one recognized value. Anything else -- absent, empty, a typo, a future
#: value clio doesn't understand yet -- behaves exactly like "absent" (never
#: guessed, never partially honored).
AGENT_AUDIENCE_VALUE = "agent"

#: Typed reason a routing decision is recorded under (stream_fallback style —
#: catalog keys, never a bare/unexplained field flip). Named per the owner's
#: 2026-09-03 vocabulary refinement -- never "sampling"/"agent-fulfilled MRTR".
ROUTED_REASON = "elicitation_routed_to_agent"
#: A form-mode elicitation routed to the agent WITHOUT an explicit ``_meta`` tag:
#: fastmcp's InputRequiredResult round-trip drops the nested ``_meta``, so the
#: tag cannot survive MRTR. clio advertises the agent-driven-elicitation
#: extension on every execution client, so under that contract an unhinted form
#: elicitation reaching the handler is treated as agent-directed.
ROUTED_UNHINTED_REASON = "elicitation_routed_to_agent_unhinted"
FALLBACK_REASON = "agent_elicitation_fallback_to_human"

AGENT_ELICITATION_REASONS: dict[str, str] = {
    ROUTED_REASON: "audience=agent and policy allows; the session's agent is answering",
    ROUTED_UNHINTED_REASON: (
        "no surviving audience tag (fastmcp drops InputRequiredResult _meta); routed to "
        "the agent by the negotiated agent-driven-elicitation contract, policy allowing"
    ),
    FALLBACK_REASON: "an agent-audience elicitation question fell back to the human path",
}

#: Sub-reasons carried alongside :data:`FALLBACK_REASON` on
#: ``UserQuestion.agent_elicitation_fallback_detail`` -- WHY the fallback
#: happened, never a silent/unexplained downgrade.
AGENT_ELICITATION_FALLBACK_DETAILS: dict[str, str] = {
    "policy_disabled": "agent-audience routing is disabled by config",
    "unknown_server": "no server namespace was identified; fails closed, never open",
    "policy_denied_server": "this server is on the agent-audience deny list",
    "url_mode_requires_human_consent": "url-mode consent must come from a human, never the agent",
    "recursion_depth_exceeded": "the bounded agent-answer recursion depth was reached",
    "no_session": "no CLIO session resolved for the elicitation",
    "spawn_refused": "the bounded answer invocation could not be spawned",
    "agent_declined": "the agent explicitly declined to answer (insufficient context)",
    "agent_answer_unparseable": "the agent's reply was not the required JSON answer shape",
    "agent_answer_schema_invalid": "the agent's answer failed the server's own requestedSchema",
    "agent_answer_timeout": "the bounded answer invocation did not finish in time",
    "agent_answer_error": "the bounded answer invocation raised an unexpected error",
}


@dataclass(frozen=True)
class AgentElicitationDecision:
    """The routing verdict for one freshly-minted elicitation question.

    ``route=False, reason=""`` is the REGRESSION-LOCKED no-hint case: nothing
    downstream (:func:`clio_agent.gact.agent_elicitation.routing_fields`,
    :func:`clio_agent.gact.agent_elicitation.on_question_published`) does
    anything observable. ``reason`` is otherwise always one of
    :data:`ROUTED_REASON` / :data:`FALLBACK_REASON`; ``detail`` names WHY for a
    fallback (a key into :data:`AGENT_ELICITATION_FALLBACK_DETAILS`).
    """

    route: bool
    reason: str = ""
    detail: str = ""
    depth: int = 0
