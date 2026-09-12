"""Agent-driven elicitation (#1309, C1-S7): the session's agent can answer a
typed MCP elicitation question, not only the human.

OWNER REQUIREMENT (verbatim intent): "I need my mcps being able to request
things from the agent." An MCP server, mid-flow, returns ``input_required``
(the v2 MRTR shape carried through :mod:`clio_agent.gact.elicitation_bridge`)
and the thing that can answer is the SESSION'S AGENT -- running on the user's
chosen provider, holding the session's own conversation context -- with the
human remaining the terminal fallback.

DESIGN INVARIANT -- THE SEMANTIC FIREWALL (owner ruling, 2026-09-03): this is
**NOT sampling** and must never become an inference channel. The MCP server
gets no model access, no free-form completions, and no prompt control -- it
gets exactly what elicitation has always given it: a typed, schema-validated
answer to its OWN declared question, with the session's agent as a permitted
answerer alongside the human. The ``requestedSchema`` validation
(:func:`clio_agent.gact.elicitation_schema.validate_elicitation_answer`) is
applied to an agent's answer EXACTLY as it is to a human's -- an agent answer
that fails it never reaches the server; it falls back to the human path,
typed (:data:`AGENT_ELICITATION_FALLBACK_DETAILS`). This module therefore adds
NO ``createMessage``/sampling vocabulary anywhere (the F7 obligations-doc
ratchet, ``tests/test_tools/test_mcp_era_gated_removals.py``, must stay green)
-- naming here is deliberately "agent_fulfillment"/"audience", never "sampling".

THE SERVER'S REQUEST SIGNAL. clio's convention (deliberately chosen, not the
reserved ``io.modelcontextprotocol/*`` namespace): a server marks a
form-mode ``ElicitRequest`` as agent-answerable with a reverse-DNS ``_meta``
key, mirroring the existing ``x-clio-agent/*`` vendor-extension convention
already used elsewhere in this repo (``mcp_exerciser.SYNTHETIC_EXTENSION_ID``,
its ``x-clio-agent/unknown`` tolerate-unknown-metadata probe)::

    {"_meta": {"x-clio-agent/audience": "agent"}, "mode": "form", ...}

Absence of the key, or any value other than exactly ``"agent"``, is IDENTICAL
to today's behavior -- the human is asked, nothing here fires, nothing new is
recorded (regression lock: :func:`decide_routing` returns a no-route,
no-reason decision and every other function in this module is then a no-op).

CLIENT POLICY GATES IT. Per-server opt-in lives on the config seam (the house
config-over-env pattern, :mod:`clio_agent.conf`) rather than on
:class:`~clio_agent.tools.mcp_config.MCPServerSpec` -- no declared-spec plumbing
touch needed for a policy this narrow. A global enable flag defaults ON (the
owner's posture: the capability existing is the point) with a per-server deny
list as the opt-out knob; EITHER way the routing decision is a TYPED, recorded
event (:func:`on_question_published` publishes :data:`ROUTED_REASON` /
:data:`FALLBACK_REASON` on the session's event bus) -- never a silent decision.
Routing keys ONLY on the typed ``audience`` field + this policy, never on
question CONTENT (superseding principle #1: no keyword/phrase matching).

FULFILLMENT = AN OBSERVABLE AGENT STEP. A routed question is answered by a
REAL, bounded child turn of the SAME session's own expert (self-directed --
``skip_declared_check=True``, mirroring ``spawn_subagent_with_skill``) spawned
through the EXISTING invocation machinery
(:class:`clio_agent.gact.agents.invoker.InProcessExpertInvoker` over
:func:`clio_agent.gact.turn_spawn.spawn_child_turn_threadsafe` -- no new
turn-state machine), seeded with a bounded excerpt of the answering session's
own transcript so it genuinely runs "through the normal loop" on the user's
provider, not a bare side-model call. **The answer turn is deliberately
TOOL-LESS** (:func:`clio_agent.gact.agents.resolution._apply_session_tool_allowlist`
forces zero tools -- declared, auto-attached, and skill -- via
``TaskSpec.tool_allowlist=()`` stamped at spawn mint time): the server's own
``message`` rides into the answer prompt VERBATIM, so a prompt-injected
elicitation can only ever produce a schema-validated value, never drive a
tool call -- the injection surface collapses entirely into the firewall
below. Its answer feeds the EXISTING atomic answer primitives
(:func:`clio_agent.gact.elicitation_bridge.claim_question_transition`
+ :func:`clio_agent.gact.elicitation_bridge.resolve_elicitation`) -- the SAME
ones the shared answer route drives -- so the MRTR retry resumes the server
unchanged and every answer is transcript-visible with typed attribution
(``UserQuestion.answered_by == "agent"``).

ANSWER STRATEGY. The default answerer is an inline bounded completion on the
session's own model (:func:`_run_agent_answer_inline`, ``answer_mode="inline"``).
A child-turn spawn (:func:`_run_agent_answer_turn`, behind ``answer_mode="turn"``)
deadlocks: the parent tool call is paused on the same session and still holds
the turn slot, so the child never runs and the call hangs to the MCP backstop.
The inline answerer is the same model, same bounded transcript excerpt, same
schema-validated JSON, still tool-less -- one completion off the event loop
(mirroring :mod:`clio_agent.gact.runtime.ai_review`), never the MCP sampling
channel.

THE RESIDUAL CHANNEL, NAMED HONESTLY: a server-declared ``{"type": "string"}``
field (no ``enum``) is still an agent-authored free-text value -- the widest
shape ``requestedSchema`` permits. ``validate_elicitation_answer``
(:mod:`clio_agent.gact.elicitation_schema`) now enforces the schema's own
declared ``minLength``/``maxLength`` on such a field, identically for a human
and an agent answer, so an over-long value is rejected typed rather than
silently accepted -- but an in-bounds string is still arbitrary agent-composed
text. This is why the tool-less answer turn above and the bounded context
excerpt are LOAD-BEARING, not incidental: they are what keeps that residual
channel confined to "a value shaped like the server's own declared schema,"
never a side channel for anything else.

RECURSION/CONVERGENCE SAFETY. A bounded ``agent_elicitation_depth`` rides the
answering child session's metadata (stamped at the SAME mint point as the
tool allowlist, never patched in after the fact). With the answer turn now
tool-less, a NESTED agent-audience question genuinely cannot arise from
within it today -- but the depth guard stays as defense in depth (never
assume a future change to the answer turn's shape can't reintroduce a tool),
routing only while depth stays under :func:`_max_depth` (default 1), else
falling back to the human, typed (``recursion_depth_exceeded``). Every other
failure mode -- decline, an unparseable/schema-invalid reply, a spawn
refusal, a timeout, an unexpected error -- ALSO falls back to the human,
typed, and NEVER drops or loops the question: the human remains the terminal
fallback, exactly as today.

url-mode elicitation is explicitly OUT OF SCOPE for agent routing regardless
of the audience hint (``url_mode_requires_human_consent``): opening a URL is a
human-consent action (see the elicitation bridge's own URL-trust docstring),
never something an LM's typed answer should be able to grant on the user's
behalf.

#1331 REVIEW ROUND SPLIT: this module crossed the flat 800-line cap (1001
lines) with no ``RATCHET_BASELINE`` entry, and had grown one hidden
``dspy.Signature`` class inside a function -- both were owner debts, not
things to baseline/ratchet-exempt. The module is now split by behavior into
owner modules (moved code byte-equal, no logic change): policy knobs
(:mod:`clio_agent.gact.agent_elicitation_policy`), the shared reason catalog
+ :class:`AgentElicitationDecision` type (:mod:`clio_agent.gact.agent_elicitation_reasons`),
the shared prompt/context helpers (:mod:`clio_agent.gact.agent_elicitation_context`),
the two answer mechanisms (:mod:`clio_agent.gact.agent_elicitation_answer_turn`,
:mod:`clio_agent.gact.agent_elicitation_answer_inline` -- the latter now the
``_AgentAnswer`` signature's home at module scope), and the routed dispatch
task (:mod:`clio_agent.gact.agent_elicitation_dispatch`). This module keeps
the routing DECISION (:func:`decide_routing`, :func:`routing_fields`,
:func:`audience_hint`) and the publish entry point (:func:`on_question_published`)
-- its own cohesive behavior -- and re-exports every name a caller or test
imported from here before the split, so no import path or monkeypatch target
moved.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from clio_agent.gact.agent_elicitation_answer_inline import _run_agent_answer_inline
from clio_agent.gact.agent_elicitation_answer_turn import (
    _run_agent_answer_turn,
    _spawn_agent_answer_turn,
)
from clio_agent.gact.agent_elicitation_dispatch import _dispatch_agent_answer
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
    AGENT_ELICITATION_REASONS,
    FALLBACK_REASON,
    ROUTED_REASON,
    ROUTED_UNHINTED_REASON,
    AgentElicitationDecision,
)
from clio_agent.gact.agent_elicitation_reply import (
    answer_field_text as _answer_field_text,
)
from clio_agent.gact.agent_elicitation_reply import (
    parse_agent_reply as _parse_agent_reply,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from clio_agent.gact.elicitation_schema import FormTranslation
    from clio_agent.gact.types import UserQuestion
    from clio_agent.tools.mcp_handlers import MCPInvocationContext

logger = logging.getLogger(__name__)

# Re-exported for callers/tests that historically imported these names off
# THIS module (before the #1331 review-round split moved their definitions
# to the owner modules imported above) -- every name below keeps working
# unchanged as `clio_agent.gact.agent_elicitation.<name>`, including the
# underscore-prefixed ones tests call directly (never monkeypatched cross-
# module, so a plain re-export is sufficient; see the split modules for the
# real definitions).
__all__ = [
    "AGENT_AUDIENCE_META_KEY",
    "AGENT_AUDIENCE_VALUE",
    "AGENT_ELICITATION_FALLBACK_DETAILS",
    "AGENT_ELICITATION_REASONS",
    "FALLBACK_REASON",
    "ROUTED_REASON",
    "ROUTED_UNHINTED_REASON",
    "AgentElicitationDecision",
    "_answer_field_text",
    "_dispatch_agent_answer",
    "_parse_agent_reply",
    "_run_agent_answer_inline",
    "_run_agent_answer_turn",
    "_spawn_agent_answer_turn",
    "audience_hint",
    "decide_routing",
    "on_question_published",
    "routing_fields",
]


# --------------------------------------------------------------------------- #
# The signal + the routing decision                                          #
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# Publish + dispatch (called once, right after the question is stored)       #
# --------------------------------------------------------------------------- #


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


def _track_task(app: Any, task: "asyncio.Task[None]") -> None:
    """Keep a strong reference to a fire-and-forget dispatch task until it settles.

    Without this, ``asyncio.create_task``'s result can be garbage-collected
    mid-flight (a well-known asyncio footgun) -- the standard fix, a per-app
    set with a self-discarding done callback.
    """

    tasks = getattr(app.state, "agent_elicitation_tasks", None)
    if not isinstance(tasks, set):
        tasks = set()
        app.state.agent_elicitation_tasks = tasks
    tasks.add(task)
    task.add_done_callback(tasks.discard)


def on_question_published(
    app: Any,
    question: "UserQuestion",
    invocation: "MCPInvocationContext",
    translation: "FormTranslation | None",
    decision: AgentElicitationDecision,
) -> None:
    """Fire the routing event and, when routed, schedule the bounded agent answer.

    Called from :func:`clio_agent.gact.elicitation_bridge._await_answer`'s
    ``on_published`` hook -- AFTER the question is in the store and its waiter
    is registered (never before: a dispatch that raced the publish could read/
    resolve a row that does not exist yet, or wake a waiter that was never
    registered). A no-route decision (including the regression-locked no-hint
    case) makes this a pure no-op beyond the (skipped, since ``reason==""``)
    event publish.
    """

    _publish_routing_event(app, question, decision)
    if not decision.route:
        return
    task = asyncio.create_task(
        _dispatch_agent_answer(app, question, invocation, translation, decision)
    )
    _track_task(app, task)
