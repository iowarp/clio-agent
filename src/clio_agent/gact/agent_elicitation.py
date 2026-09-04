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
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from clio_agent import conf
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

__all__ = [
    "AGENT_AUDIENCE_META_KEY",
    "AGENT_AUDIENCE_VALUE",
    "AGENT_ELICITATION_FALLBACK_DETAILS",
    "AGENT_ELICITATION_REASONS",
    "FALLBACK_REASON",
    "ROUTED_REASON",
    "AgentElicitationDecision",
    "audience_hint",
    "decide_routing",
    "on_question_published",
    "routing_fields",
]

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
FALLBACK_REASON = "agent_elicitation_fallback_to_human"

AGENT_ELICITATION_REASONS: dict[str, str] = {
    ROUTED_REASON: "audience=agent and policy allows; the session's agent is answering",
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

_DEFAULT_MAX_DEPTH = 1
_DEFAULT_TIMEOUT_S = 90.0
_OUTER_TIMEOUT_MARGIN_S = 15.0
_CONTEXT_EXCERPT_MAX_CHARS = 6000


# --------------------------------------------------------------------------- #
# Policy (config-over-env; the house pattern -- clio_agent.conf)              #
# --------------------------------------------------------------------------- #


def _enabled() -> bool:
    """Global on/off for agent-audience routing (default ON, owner-ruled posture)."""

    return conf.resolve(
        "tools.mcp.elicitation.agent_audience.enabled",
        env="CLIO_MCP_ELICITATION_AGENT_AUDIENCE_ENABLED",
        default=True,
        cast=conf.as_bool,
    )


def _denied_servers() -> frozenset[str]:
    """Per-server opt-OUT list (namespace names) -- the policy knob the owner required."""

    default: list[str] = []
    return frozenset(
        conf.resolve(
            "tools.mcp.elicitation.agent_audience.denied_servers",
            env="CLIO_MCP_ELICITATION_AGENT_AUDIENCE_DENIED_SERVERS",
            default=default,
            cast=conf.as_csv,
        )
    )


def _max_depth() -> int:
    """The bounded agent-answer recursion depth (default 1)."""

    return conf.resolve(
        "tools.mcp.elicitation.agent_audience.max_depth",
        env="CLIO_MCP_ELICITATION_AGENT_AUDIENCE_MAX_DEPTH",
        default=_DEFAULT_MAX_DEPTH,
        cast=conf.as_int,
    )


def _timeout_s() -> float:
    """Bounded wall-clock budget for one agent-answer child turn."""

    return conf.resolve(
        "tools.mcp.elicitation.agent_audience.timeout_s",
        env="CLIO_MCP_ELICITATION_AGENT_AUDIENCE_TIMEOUT_S",
        default=_DEFAULT_TIMEOUT_S,
        cast=conf.as_float,
    )


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


@dataclass(frozen=True)
class AgentElicitationDecision:
    """The routing verdict for one freshly-minted elicitation question.

    ``route=False, reason=""`` is the REGRESSION-LOCKED no-hint case: nothing
    downstream (:func:`routing_fields`, :func:`on_question_published`) does
    anything observable. ``reason`` is otherwise always one of
    :data:`ROUTED_REASON` / :data:`FALLBACK_REASON`; ``detail`` names WHY for a
    fallback (a key into :data:`AGENT_ELICITATION_FALLBACK_DETAILS`).
    """

    route: bool
    reason: str = ""
    detail: str = ""
    depth: int = 0


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

    Keys ONLY on the typed ``audience`` value + policy + the structural
    ``mode``/recursion-depth facts below -- never on question/prompt CONTENT
    (superseding principle #1: clio never keyword-matches a model's or a
    server's prose to decide). ``audience`` must be EXACTLY
    :data:`AGENT_AUDIENCE_VALUE`; anything else returns the regression-locked
    no-op decision.
    """

    if audience != AGENT_AUDIENCE_VALUE:
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
    return AgentElicitationDecision(route=True, reason=ROUTED_REASON, depth=depth + 1)


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


# --------------------------------------------------------------------------- #
# The bounded answer invocation (existing spawn/invoker machinery)           #
# --------------------------------------------------------------------------- #


def _flatten_message_text(msg: Any) -> str:
    """Join a message's text parts (mirrors the same idiom used at every other
    "read a message's text back" call site in gact — deliberately small and
    duplicated here rather than reaching into another module's private helper)."""

    parts = getattr(msg, "parts", None) or []
    out: list[str] = []
    for part in parts:
        text = getattr(part, "text", None)
        if text is None and isinstance(part, Mapping):
            text = part.get("text")
        part_type = getattr(part, "type", None) or (
            part.get("type") if isinstance(part, Mapping) else None
        )
        if part_type == "text" and text:
            out.append(str(text))
    return "".join(out).strip()


def _bounded_transcript_excerpt(app: Any, session_id: str) -> str:
    """A bounded, best-effort excerpt of ``session_id``'s own message transcript.

    NOT the ARC context-compilation pipeline (RULE 6): that plane
    (:mod:`clio_agent.arc.context_compiler`) has exactly one production
    consumer in this repo today (``arc/retrieval.py``) and is not wired into
    any gact turn/session call site, so depending on it here would be a new,
    unverified dependency rather than a reuse of "existing invocation
    machinery". This reads the session's own ALREADY-COLLECTED message store
    instead (RULE 4: no new store) and caps it so a long conversation cannot
    blow an unbounded prompt into the bounded answer turn -- the MOST RECENT
    text is kept (a truncated-from-the-front excerpt), since the nonce/fact the
    agent needs to recall is typically closer to the paused tool call.
    """

    messages = getattr(app.state, "messages", {})
    rows = messages.get(session_id, []) if hasattr(messages, "get") else []
    lines: list[str] = []
    for msg in rows:
        role = str(getattr(msg, "role", "") or "")
        if role not in ("user", "assistant"):
            continue
        text = _flatten_message_text(msg)
        if text:
            lines.append(f"{role}: {text}")
    excerpt = "\n".join(lines)
    if len(excerpt) > _CONTEXT_EXCERPT_MAX_CHARS:
        excerpt = excerpt[-_CONTEXT_EXCERPT_MAX_CHARS:]
    return excerpt


def _render_field(field: Mapping[str, Any]) -> str:
    piece = f"- {field.get('name')} ({field.get('type')}{'[]' if field.get('multi') else ''})"
    enum = field.get("enum")
    if enum:
        piece += f", one of {list(enum)}"
    if field.get("required"):
        piece += ", REQUIRED"
    description = field.get("description")
    if description:
        piece += f": {description}"
    return piece


def _build_answer_prompt(question: "UserQuestion", translation: "FormTranslation | None") -> str:
    """The instructional prompt handed to the bounded answer child turn."""

    fields = list(translation.fields) if translation is not None else []
    schema_desc = "\n".join(_render_field(f) for f in fields) or "(no fields declared)"
    return (
        "An MCP tool call in THIS conversation is paused, waiting for an answer that "
        "only you (the assistant, with this conversation's own context) can provide -- "
        "a human is not being asked because the server marked this question for the "
        "agent specifically.\n\n"
        f"Question: {question.prompt}\n\n"
        f"Answer fields:\n{schema_desc}\n\n"
        "Reply with EXACTLY ONE JSON object and nothing else (no prose, no markdown "
        "fences):\n"
        "- If the conversation above already establishes a confident answer, reply "
        '{"answer": {<one key per field name above, with a correctly-typed value>}}\n'
        '- If it does not, reply {"decline": true, "reason": "<short reason>"}\n'
        "Never guess -- decline unless the conversation genuinely established the answer."
    )


def _spawn_agent_answer_turn(
    app: Any,
    *,
    answer_session_id: str,
    prompt: str,
    depth: int,
) -> Any:
    """Spawn (never waits for) the bounded, self-directed, TOOL-LESS answer child.

    Split out from :func:`_run_agent_answer_turn` so the spawn step -- the
    part F1's authority test drives for real -- is independently callable
    without paying for a full answer-turn LM round trip. Reuses the SAME
    invocation machinery every declared child spawn uses
    (:class:`clio_agent.gact.agents.invoker.InProcessExpertInvoker` over
    :func:`clio_agent.gact.turn_spawn.spawn_child_turn_threadsafe`) -- no new
    turn-state machine. ``skip_declared_check=True`` marks this a
    SELF-directed spawn (the answering expert is the session's own, exactly
    like ``spawn_subagent_with_skill``'s self-directed skill-subagent), never
    a routing decision to a different declared capability. ``tool_allowlist=()``
    forces the child to ZERO tools (see
    :func:`clio_agent.gact.agents.resolution._apply_session_tool_allowlist`) --
    stamped onto the child's OWN metadata at MINT time by ``spawn_child_turn``
    itself, so it is true before the child's first turn build, never patched
    in after the fact.

    Returns:
        The :class:`~clio_agent.gact.agents.invoker.TaskHandle` for the caller
        to ``wait``/``cancel``.

    Raises:
        SpawnError: the bounded spawn was refused (global depth cap, cancelled
            parent, or no live expert bound to the answering session).
    """

    from clio_agent.gact.agents.invoker import InProcessExpertInvoker  # noqa: PLC0415
    from clio_agent.gact.agents.spawn_runtime import _current_session_depth  # noqa: PLC0415
    from clio_agent.gact.runtime.globals import _session_agent_id  # noqa: PLC0415
    from clio_agent.gact.spawn_context import bind_task_spec_to_parent  # noqa: PLC0415
    from clio_agent.gact.turn_spawn import SpawnError, TaskSpec  # noqa: PLC0415

    session = app.state.sessions.get(answer_session_id)
    expert_id = _session_agent_id(session) if session is not None else ""
    if session is None or not expert_id:
        raise SpawnError(
            "no live session/expert bound to answer from",
            reason="agent_elicitation_no_expert",
        )

    seed_context = _bounded_transcript_excerpt(app, answer_session_id)
    spawn_depth = _current_session_depth(app, answer_session_id) + 1
    spec = bind_task_spec_to_parent(
        app,
        TaskSpec(
            child_expert_id=expert_id,
            task_text=prompt,
            parent_session_id=answer_session_id,
            requesting_expert_id="agent_elicitation",
            depth=spawn_depth,
            mode="sync",
            skip_declared_check=True,
            seed_context=seed_context,
            run_label="agent-elicitation answer",
            # F1/F3 (owner gate review): both stamped onto the child's OWN
            # metadata at MINT time by spawn_child_turn itself -- true before
            # the child's first turn build, never patched in afterward.
            tool_allowlist=(),
            agent_elicitation_depth=depth,
        ),
    )
    return InProcessExpertInvoker(app).invoke(spec)  # SpawnError propagates, typed


def _run_agent_answer_turn(
    app: Any,
    *,
    answer_session_id: str,
    prompt: str,
    depth: int,
    timeout_s: float,
) -> str:
    """Spawn + wait for the bounded answer child turn; return its raw reply
    text (the bounded ``answer_excerpt``).

    Runs entirely on a worker thread (every call here blocks) -- callers
    dispatch it via ``asyncio.to_thread``, exactly like
    ``gact.runtime.ai_review``'s one-shot reviewer runs its own bounded LM
    call off the event loop.

    Raises:
        SpawnError: see :func:`_spawn_agent_answer_turn`.
        TimeoutError: the answer turn did not reach a terminal state in time
            (the in-flight child is cancelled before this raises).
        RuntimeError: the answer turn reached a non-``completed`` terminal
            status (failed/cancelled).
    """

    from clio_agent.gact.agents.invoker import InProcessExpertInvoker  # noqa: PLC0415

    handle = _spawn_agent_answer_turn(
        app, answer_session_id=answer_session_id, prompt=prompt, depth=depth
    )
    invoker = InProcessExpertInvoker(app)
    result = invoker.wait(handle, timeout_s=timeout_s)
    if not result.is_terminal:
        invoker.cancel(handle)
        raise TimeoutError(f"agent-elicitation answer turn {handle.task_id} timed out")
    if result.status != "completed":
        raise RuntimeError(
            f"agent-elicitation answer turn {handle.task_id} ended {result.status!r}: "
            f"{result.error_reason or 'no reason recorded'}"
        )
    payload = result.result or {}
    answer_text = _answer_field_text(
        app, result.child_session_id, str(payload.get("message_ref", ""))
    )
    if answer_text:
        return answer_text
    # STRUCTURAL fallback (never a silent regression): no ``answer``-field part
    # was found on the child's final message (an unexpected module shape --
    # e.g. a future answer-turn kind that never streams a text `answer` part
    # at all). Fall back to the generic, bounded excerpt exactly as before
    # this fix, so SOME text still reaches _parse_agent_reply's typed
    # unparseable fallback rather than an empty string masquerading as "the
    # model said nothing".
    return str(payload.get("answer_excerpt", ""))


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


async def _dispatch_agent_answer(
    app: Any,
    question: "UserQuestion",
    invocation: "MCPInvocationContext",
    translation: "FormTranslation | None",
    decision: AgentElicitationDecision,
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
    try:
        reply_text = await asyncio.wait_for(
            asyncio.to_thread(
                _run_agent_answer_turn,
                app,
                answer_session_id=answer_session_id,
                prompt=prompt,
                depth=decision.depth,
                timeout_s=timeout_s,
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
    if parsed.get("decline"):
        _fallback(app, question, "agent_declined")
        return
    answer_obj = parsed.get("answer")
    if not isinstance(answer_obj, Mapping):
        _fallback(app, question, "agent_answer_unparseable")
        return

    from clio_agent.gact.elicitation_bridge import (  # noqa: PLC0415
        claim_question_transition,
        resolve_elicitation,
    )
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
    resolve_elicitation(app, updated)
    bus = getattr(app.state, "bus", None)
    if bus is not None:
        from clio_agent.gact.events import Event  # noqa: PLC0415

        bus.publish(
            Event(
                type="user_question.answered",
                session_id=updated.session_id,
                payload=updated.model_dump(exclude_none=True),
            )
        )
