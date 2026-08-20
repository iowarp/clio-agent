"""Shared runtime base for the GACT server (#714 decomposition).

This module is the cross-concern foundation every module carved out of the
``clio_agent.gact.app`` monolith imports FROM. Folding it out FIRST (leaves-first
strangler) is what lets the rest of the decomposition proceed without anything
importing the 24k-line ``app.py`` -- so the dependency graph stays acyclic.

It owns, as the single source of truth:

* **The ARC singleton + accessors.** ``_PROCESS_ARC`` is the ONE
  :class:`~clio_agent.arc.memory.ARCMemory` per process (ARC is a per-clio-agent
  keystone). ``_set_app_arc`` publishes it and wires the durable-trace op-logger
  + highway-derive sink; ``_process_arc`` lazily constructs it once;
  ``_emit_arc_op`` logs an applied ARC context op to the durable Trace.
* **The semantic-event FUNNEL.** ``_build_semantic_event`` /
  ``_emit_semantic_event`` (+ the react-step / expert-lifecycle wrappers) are the
  60+-callsite choke point through which EVERY semantic event enters ARC, the
  source of the highway.
* **The internal exceptions** used to settle turns / signal terminal workflow
  states, plus ``_not_implemented`` / ``_coerce_error_info``.
* **ID / timestamp generators + the SSE wire formatter** (``_format_sse``).
* **The ``_ctx`` boundary shims** (the ``_CompatVar`` proxies, the tool/app
  context managers). The resolve-once expert caches formerly here are now
  per-app on ``app.state`` (``gact.runtime.app_state.per_app_dict``, #770).

It imports ONLY gact *leaves* (``gact.context``, ``gact.semantic_events``,
``gact.events``, ``gact.types``) + stdlib -- NEVER ``gact.app`` -- so it is
cycle-proof. The three former lazy importers of the funnel
(``config.py``, ``runtime/lm_activity.py``) now point HERE, not at ``app.py``,
which closes a latent import cycle (``app.py`` imported ``config`` at module top
while ``config`` lazy-imported the funnel back from ``app``).
"""

from __future__ import annotations

import contextvars
import json
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterator, Optional

from clio_agent.gact import context as _ctx
from clio_agent.gact.events import Event
from clio_agent.gact.semantic_events import DEFAULT_DETAIL_LEVEL, SemanticEvent
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo
from clio_agent.runtime import trace
from clio_agent.tools.mcp_runtime import wire_value

if TYPE_CHECKING:
    from fastapi import FastAPI


# --------------------------------------------------------------------------- #
# Runtime context (#714 seam)                                                   #
#                                                                               #
# The 11 module-level ContextVars that formerly lived in app.py are replaced by #
# a single object on ONE ContextVar in ``clio_agent.gact.context`` (imported as #
# ``_ctx``). Core call sites use the granular ``_ctx.*`` accessors / mutators.  #
#                                                                               #
# The five legacy names below are kept importable as ``_CompatVar`` proxies so  #
# (a) ``test_import_seams`` getattr checks, (b) cross-module lazy importers,    #
# and (c) any stray ``.get()/.set()/.reset()`` caller keep working with the     #
# CORRECT types -- a proxy ``.get()`` returns the field value (e.g. an app or a #
# session-id str), NOT the whole RuntimeContext. They delegate to ``_ctx``.     #
# --------------------------------------------------------------------------- #


class _CompatVar:
    """Back-compat proxy exposing a ``ContextVar``-shaped ``.get()/.set()/.reset()``.

    The live channel is ``clio_agent.gact.context._RUNTIME``; these proxies are
    no longer it. Each delegates to a granular ``context`` accessor (read) and
    mutator (write) so legacy callers and getattr-importers keep working with
    correct field types. ``reset`` forwards the ``context``-returned token.
    """

    __slots__ = ("_getter", "_setter")

    def __init__(
        self,
        getter: "Callable[[], Any]",
        setter: "Callable[[Any], contextvars.Token[_ctx.RuntimeContext]]",
    ) -> None:
        self._getter = getter
        self._setter = setter

    def get(self) -> Any:
        return self._getter()

    def set(self, value: Any) -> "contextvars.Token[_ctx.RuntimeContext]":
        return self._setter(value)

    def reset(self, token: "contextvars.Token[_ctx.RuntimeContext]") -> None:
        _ctx.reset(token)


_ACTIVE_GACT_APP = _CompatVar(_ctx.active_app, _ctx.set_app)
_ACTIVE_GACT_SESSION_ID = _CompatVar(_ctx.active_session_id, _ctx.set_session_id)
_ACTIVE_GACT_TURN_ID = _CompatVar(_ctx.active_turn_id, _ctx.set_turn_id_token)
_ACTIVE_GACT_TRACE_ID = _CompatVar(_ctx.active_trace_id, _ctx.set_trace_id_token)
_ACTIVE_BLUEPRINT_TOOL_ROWS = _CompatVar(
    _ctx.active_blueprint_tool_rows, _ctx.set_blueprint_tool_rows
)

# The resolve-once expert caches (declared child ids for the spawn-tool routing
# surface; the orchestrator-identity briefing) no longer live here as process-global dicts
# (#770 unified-concurrency §4 Site 2). They are keyed on the live turn's
# ``app.state`` via ``gact.runtime.app_state.per_app_dict`` so one app's build can
# never leak its value into a sibling app's (first/last-writer-wins), and an
# app-less consume yields a structured empty (deterministic finish) rather than a
# stale cross-app value.


@contextmanager
def _tool_session_context(sid: str) -> Iterator[None]:
    """Bind the tool session id + workspace root + active blueprint for the turn.

    The four tool-runtime hooks are resolved per tool call by the installed
    ``ToolRuntimeHooks`` resolver (``resolve_tool_runtime`` dispatching on the
    keystone-bound ``_ctx.active_app()``), so this no longer binds them. It binds
    the tool session id (read by the permission gate to attribute the call to
    the live turn's session), the workspace root — resolved off the live app so
    the tool executor grounds output artifacts into the bound workspace (#735) —
    and the session's EXPLICITLY-activated Agent Blueprint id (#1232 pt 1), so
    ``ClioAgent._active_tool_executor`` mounts exactly that blueprint's declared
    ``mcp_servers`` (if any) into the per-workspace gateway, never every
    installed blueprint's servers and never at boot.
    """
    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        _runtime_active_agent_blueprint_id,
    )
    from clio_agent.tools.execution import (  # noqa: PLC0415
        tool_blueprint_context,
        tool_workspace_context,
    )

    workspace_root = ""
    app = _ctx.active_app()
    app_state = getattr(app, "state", None) if app is not None else None
    if app_state is not None:
        sessions = getattr(app_state, "sessions", None)
        workspaces = getattr(app_state, "workspaces", None)
        sess = sessions.get(sid) if sessions is not None else None
        workspace_id = str(getattr(sess, "workspace_id", "") or "") if sess is not None else ""
        ws = workspaces.get(workspace_id) if workspaces is not None and workspace_id else None
        workspace_root = str(getattr(ws, "root_path", "") or "")
    blueprint_id = _runtime_active_agent_blueprint_id(app, sid) if app is not None else ""
    token = _ctx.set_tool_session_id(sid)
    # #933: pin the workspace fleet for the WHOLE turn — between-call idleness
    # inside a live turn must not count toward the reaper's TTL.
    agent = getattr(app_state, "agent", None) if app_state is not None else None
    lease = getattr(agent, "lease_workspace_fleet", None)
    try:
        if workspace_root and callable(lease):
            with (
                lease(workspace_root),
                tool_workspace_context(workspace_root),
                tool_blueprint_context(blueprint_id),
            ):
                yield
        else:
            if workspace_root:
                # A rooted turn without a leasable agent runs UNPROTECTED from
                # the #933 reaper — degraded path, so the reason is typed.
                trace.event(
                    "TOOLS",
                    "workspace_lease_unavailable session=%s root=%s reason=agent_has_no_lease_hook",
                    sid,
                    workspace_root,
                )
            with (
                tool_workspace_context(workspace_root),
                tool_blueprint_context(blueprint_id),
            ):
                yield
    finally:
        _ctx.reset(token)


@contextmanager
def _gact_app_context(app: Any) -> Iterator[None]:
    """Bind app state for dynamic agent tool wrappers."""
    token = _ctx.set_app(app)
    try:
        yield
    finally:
        _ctx.reset(token)


def _resolve_tool_session(app: "FastAPI") -> tuple[str, Any | None]:
    """Return the active turn session, falling back to recency for out-of-band calls."""
    sid = _ctx.active_tool_session_id().strip()
    if sid:
        return sid, app.state.sessions.get(sid)
    sessions_by_recency = app.state.sessions.list()
    if sessions_by_recency:
        current = sessions_by_recency[0]
        return current.id, current
    return "", None


def _session_agent_id(sess: Any) -> str:
    """Return the active session agent id from dict or object refs."""

    agent = getattr(sess, "agent", None)
    if isinstance(agent, Mapping):
        return str(agent.get("id") or "").strip()
    return str(getattr(agent, "id", "") or "").strip()


def _format_sse(event: "Event") -> bytes:
    """Render an Event as the SSE wire format (SPEC §7.2)::

    event: <type>
    id: <numeric monotonic id>
    data: <json envelope>
    <blank line>
    """

    payload = json.dumps(event.envelope())
    lines = f"event: {event.type}\nid: {event.id}\ndata: {payload}\n\n"
    return lines.encode("utf-8")


# ---- ID + timestamp helpers used by the message endpoint ---------
# Kept at module level (not inside build_app) so they're trivially
# importable by future streaming code + easy to mock in tests.


def _new_message_id(role_prefix: str) -> str:
    """Generate a message id. Role prefix ('user' / 'asst' / 'tool')
    makes log scraping + human triage cheaper."""

    return f"msg_{role_prefix}_{uuid.uuid4().hex[:12]}"


def _new_part_id() -> str:
    return f"part_{uuid.uuid4().hex[:12]}"


def _new_cancellation_attempt_id() -> str:
    return f"cancel_{uuid.uuid4().hex[:12]}"


def _new_question_id() -> str:
    return f"ques_{uuid.uuid4().hex[:12]}"


def _new_attempt_id() -> str:
    return f"att_{uuid.uuid4().hex[:12]}"


def _new_context_frame_id() -> str:
    return f"ctx_{uuid.uuid4().hex[:12]}"


def _new_memory_event_id() -> str:
    return f"mem_{uuid.uuid4().hex[:12]}"


def _iso_from_epoch(ts: float) -> str:
    """ISO-8601 UTC with microsecond precision to match the session
    registry's created_at format."""

    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _semantic_trace_id(turn_id: str) -> str:
    return f"trace_{turn_id}" if turn_id else f"trace_{uuid.uuid4().hex[:12]}"


def _active_semantic_turn_id() -> str:
    return _ctx.active_turn_id()


def _active_semantic_trace_id() -> str:
    return _ctx.active_trace_id()


def _llm_provider_payload(app: "FastAPI", agent_id: str = "") -> dict[str, Any]:
    """Build the ``provider`` payload (provider/model/api-base/temperature/max-tokens)
    attached to LM-activity semantic events.

    Reads the effective LM config off ``app.state`` (falling back to the live
    agent's provider config). ``_effective_lm_config`` is owned by the
    provider-bind concern (:mod:`clio_agent.gact.providers.config`); it is
    imported lazily at call time so this funnel base stays free of a module-load
    cycle (the no-cycle invariant of the #714 decomposition).
    """

    from clio_agent.gact.providers.config import _effective_lm_config  # noqa: PLC0415

    cfg = _effective_lm_config(app)
    return {
        "provider_id": str(cfg.get("provider") or ""),
        "model_id": str(cfg.get("model") or ""),
        "api_base": str(cfg.get("api_base") or ""),
        "temperature": cfg.get("temperature"),
        "max_tokens": cfg.get("max_tokens"),
        "agent_id": agent_id,
    }


# The ONE ARC for this process (the keystone is a per-process singleton). Published by
# ``_set_app_arc`` so every emit context — including deep/threaded ones where the request
# app isn't reachable — resolves the SAME ARC and routes through it. ARC is the SOURCE of
# the highway; there is intentionally NO silent fallback if it is absent
# (``_emit_semantic_event`` raises). ``None`` only before any agent/ARC is constructed.
_PROCESS_ARC: Any = None


def _build_semantic_event(
    app: "FastAPI",
    sid: str,
    event_type: str,
    *,
    turn_id: str = "",
    trace_id: str = "",
    parent_span_id: str = "",
    status: str = "completed",
    summary: str = "",
    actor: Optional[dict[str, Any]] = None,
    subject: Optional[dict[str, Any]] = None,
    blueprint: Optional[dict[str, Any]] = None,
    provider: Optional[dict[str, Any]] = None,
    payload: Optional[dict[str, Any]] = None,
    live_observed: bool = True,
    detail_level: Optional[str] = None,
) -> SemanticEvent:
    """Build (but do not route) a fully-populated :class:`SemanticEvent`.

    Shared by ``_emit_semantic_event`` (which routes through ARC, the source) and
    ``_emit_arc_op`` (which derives DIRECTLY to the durable trace + SSE bus, because
    arc.op is the WRITE-LOG of an ARC mutation — a projection of a record, NOT a
    semantic event to feed back through ``arc.record_semantic_event``)."""
    state = getattr(app, "state", None)
    if state is not None and hasattr(state, "sessions"):
        sess = state.sessions.get(sid)
    else:
        sess = None
    workspace_id = str(getattr(sess, "workspace_id", "") or "")
    # The payload rides verbatim — clio does NOT author UI captions. The event's
    # one-line ``summary`` already rides the envelope; the consumer (TUI) decides
    # how to fold the FULL content.
    event_payload = dict(payload or {})
    return SemanticEvent(
        event_type=event_type,
        session_id=sid,
        workspace_id=workspace_id,
        trace_id=trace_id or _semantic_trace_id(turn_id),
        turn_id=turn_id,
        # Auto-nest under the active span (expert/step) unless the caller pins a
        # parent explicitly, so the highway forms the recursive trajectory tree.
        parent_span_id=parent_span_id or _ctx.active_parent_span_id(),
        status=status,
        summary=summary,
        actor=actor or {},
        subject=subject or {},
        blueprint=blueprint or {},
        provider=provider or {},
        payload=event_payload,
        live_observed=live_observed,
        detail_level=(
            detail_level
            if detail_level is not None
            else getattr(app.state, "semantic_trace_detail_level", DEFAULT_DETAIL_LEVEL)
        ),
    )


def _emit_semantic_event(
    app: "FastAPI",
    sid: str,
    event_type: str,
    *,
    turn_id: str = "",
    trace_id: str = "",
    parent_span_id: str = "",
    status: str = "completed",
    summary: str = "",
    actor: Optional[dict[str, Any]] = None,
    subject: Optional[dict[str, Any]] = None,
    blueprint: Optional[dict[str, Any]] = None,
    provider: Optional[dict[str, Any]] = None,
    payload: Optional[dict[str, Any]] = None,
    live_observed: bool = True,
    detail_level: Optional[str] = None,
) -> dict[str, Any]:
    """Emit one semantic event if the app has the semantic sink wired.

    ``detail_level`` overrides the app-wide SSE detail for this event only.
    Pass ``"off"`` for high-volume durable-only captures (e.g. ``lm.call``):
    the durable backend still records the FULL event, but SSE/hooks are skipped
    so the wire/UI is not flooded.
    """

    state = getattr(app, "state", None)
    sink = getattr(state, "semantic_event_sink", None)
    if sink is None:
        return {}
    event = _build_semantic_event(
        app,
        sid,
        event_type,
        turn_id=turn_id,
        trace_id=trace_id,
        parent_span_id=parent_span_id,
        status=status,
        summary=summary,
        actor=actor,
        subject=subject,
        blueprint=blueprint,
        provider=provider,
        payload=payload,
        live_observed=live_observed,
        detail_level=detail_level,
    )
    # ARC is the SOURCE of the highway: EVERY semantic event MUST enter through ARC, which
    # records it (current-view update) and DERIVES the highway (trace/SSE/hooks) from that
    # record. There is exactly ONE ARC per process; resolve it from the request app, else
    # the process singleton (a deep/threaded emit context may not carry the app). If it is
    # STILL not reachable, FAIL LOUD — never silently fall back to sink.emit, which would
    # feed the trace/UI an event ARC never saw (a hidden split: trace has data ARC doesn't).
    arc = getattr(state, "arc", None) or _PROCESS_ARC
    rec = getattr(arc, "record_semantic_event", None)
    if rec is None:
        msg = (
            f"ARC-as-source violated: no ARC visible when emitting semantic event "
            f"{event_type!r} (session={sid!r}). The highway must derive from ARC; "
            f"refusing to bypass it via a silent fallback."
        )
        trace.event("ARC-AS-SOURCE", "%s", msg)
        raise RuntimeError(msg)
    return rec(event)


def _entry_reasoning_text(entry: dict[str, Any]) -> str:
    """Pull the reasoning-channel text out of one dspy ``lm.history`` entry.

    DSPy stores reasoning per call in ``entry["outputs"]`` (each output dict may
    carry ``reasoning_content``) and on the raw ``entry["response"]``
    (``choices[i].message.reasoning_content``). Most stacks discard this; we
    surface it because the chain-of-thought has scientific value for analysing
    how a model reached an answer.
    """

    parts: list[str] = []
    outputs = entry.get("outputs")
    if isinstance(outputs, list):
        for out in outputs:
            if isinstance(out, dict):
                rc = out.get("reasoning_content")
                if rc:
                    parts.append(str(rc))
    if not parts:
        response = entry.get("response")
        choices = getattr(response, "choices", None)
        if isinstance(choices, list):
            for choice in choices:
                msg = getattr(choice, "message", None)
                rc = getattr(msg, "reasoning_content", None) if msg is not None else None
                if rc:
                    parts.append(str(rc))
    return "\n".join(p for p in parts if p).strip()


def _active_lm_last_reasoning() -> str:
    """Best-effort: the reasoning-channel text (chain-of-thought) of the most recent
    call on the active dspy LM — i.e. the call that just produced a ReAct step or the
    extract. Empty for content-channel models (e.g. gemma, whose reasoning is parsed
    into ``next_thought``) or when unavailable. MUST be read immediately after the
    LM call and before any tool runs (a delegation tool runs a child whose LM call
    would otherwise become ``history[-1]``).

    ONE capture per call: the ``IOLoggingLM`` boundary already reads ``history[-1]``
    once per call (``config._clio_log_last_call``) and stashes the reasoning on the LM
    as ``_clio_last_reasoning``. Reuse that read so the same buffer is not parsed a
    second time. Falls back to a direct ``history[-1]`` read only for an LM that is not
    our boundary subclass (e.g. a test DummyLM, which carries no reasoning channel)."""

    try:
        from clio_agent.gact.runtime.ambient_lm import resolve_active_lm  # noqa: PLC0415

        # Inside the expert/main ``dspy.context`` this is the bound profile LM whose
        # call just ran (the normal path). Outside one it falls through to the boot
        # default AND records an ``ambient_lm_default`` reason so the miss is
        # queryable rather than a silent ambient read (#818).
        lm = resolve_active_lm(site="globals._active_lm_last_reasoning")
        if lm is None:
            return ""
        stashed = getattr(lm, "_clio_last_reasoning", None)
        if stashed is not None:
            return str(stashed)
        history = getattr(lm, "history", None)
        if history:
            return _entry_reasoning_text(history[-1])
    except Exception:  # noqa: BLE001 - capture is best-effort, never break the loop
        return ""
    return ""


def _emit_react_step_event(
    *,
    expert_id: str,
    expert_span_id: str,
    step_span_id: str,
    step_index: int,
    thought: Any,
    reasoning: Any,
    tool_name: Any,
    tool_args: Any,
    observation: Any,
    is_finish: bool,
) -> None:
    """Put ONE ReAct Step (LLM response + tool act/observe) on the core highway.

    A ReAct Step is the atom of an expert trajectory: the LLM's response (its
    ``thought`` + the tool it chose) plus the resulting tool calling that ``act``s
    on or ``observe``s the environment. Stock dspy discards every step but the
    final ``extract``; this surfaces each one so the full per-turn trajectory rides
    the highway.

    Capture-only and UNCAPPED (per the trajectory ontology): the highway carries
    everything; per-consumer filtering happens downstream (the trace/TUI apply no
    filter, the parent filters to the extract). ``thought``/``tool_*``/``observation``
    are not in ``SENSITIVE_KEYS``; ``reasoning`` IS, but is allowed through the SSE
    projection for THIS event type via ``SSE_KEEP_KEYS_BY_EVENT`` (so the model's
    chain-of-thought reaches the live UI here while staying redacted on lm.call /
    lm.token.delta / raw-prompt events). Steps of one expert share
    ``expert_span_id``. Best-effort: capture must never break the expert loop.
    """

    app = _ctx.active_app()
    sid = _ctx.active_session_id()
    if app is None or not sid:
        return
    try:
        _emit_semantic_event(
            app,
            sid,
            "react.step.completed",
            turn_id=_active_semantic_turn_id(),
            trace_id=_active_semantic_trace_id(),
            parent_span_id=expert_span_id,
            status="completed",
            summary=(
                f"{expert_id or 'expert'} ReAct step {step_index}: {str(tool_name) or 'finish'}"
            ),
            actor={"agent_id": expert_id, "role": "expert"},
            payload={
                "expert_id": expert_id,
                "expert_span_id": expert_span_id,
                # This step's span: the lm.call (self.react) and tool.call (act/
                # observe) of this step carry parent_span_id == step_span_id, so a
                # consumer links them to this step.
                "step_span_id": step_span_id,
                "step_index": step_index,
                # ``thought`` = DSPy's parsed next_thought (good for content-channel
                # models like gemma). ``reasoning`` = the raw reasoning channel
                # (chain-of-thought) for reasoning models — distinct from thought.
                # Allowed through the SSE projection only for this event type.
                "thought": wire_value(thought, mode="gact_runtime"),
                "reasoning": wire_value(reasoning, mode="gact_runtime"),
                "tool_name": str(tool_name or ""),
                "tool_args": wire_value(tool_args, mode="gact_runtime"),
                "observation": wire_value(observation, mode="gact_runtime"),
                "is_finish": bool(is_finish),
            },
        )
    except Exception:  # noqa: BLE001,S110 - capture must never break the expert loop
        pass


def _emit_expert_lifecycle_event(
    event_type: str,
    *,
    expert_id: str,
    expert_span_id: str,
    status: str,
    payload: dict[str, Any],
) -> None:
    """Mark an expert-lifecycle boundary on the highway.

    An expert lifecycle starts when a parent delegates to it and ends with the
    ``dspy.extract`` output it returns. ``expert.lifecycle.started`` is emitted
    BEFORE the active span is switched to this expert (so it nests under the
    delegating scope); ``expert.extract.completed`` is emitted while the active
    span IS this expert (so it nests under the lifecycle). The extract output is
    carried FULL/uncapped — what the parent ultimately filters to is a downstream
    projection (#710), not a capture-time loss. Best-effort.
    """

    app = _ctx.active_app()
    sid = _ctx.active_session_id()
    if app is None or not sid:
        return
    try:
        _emit_semantic_event(
            app,
            sid,
            event_type,
            turn_id=_active_semantic_turn_id(),
            trace_id=_active_semantic_trace_id(),
            status=status,
            summary=f"expert {expert_id or '?'} {event_type.rsplit('.', 1)[-1]}",
            actor={"agent_id": expert_id, "role": "expert"},
            payload={"expert_id": expert_id, "expert_span_id": expert_span_id, **payload},
        )
    except Exception:  # noqa: BLE001,S110 - capture must never break the expert loop
        pass


# The single new event type for ARC live-context-plane mutations. event_type is a
# free string (no enum/registry); the replay reader dispatches on this literal.
ARC_OP_EVENT_TYPE = "arc.op"


def _emit_arc_op(
    app: "FastAPI",
    op: str,
    session_id: str,
    scope: str,
    *,
    logical_time: int,
    step: Optional[int] = None,
    position: Optional[int] = None,
    segments_written: Optional[list[dict[str, Any]]] = None,
    segments_tombstoned: Optional[list[str]] = None,
    derived_from: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Log ONE applied ARC context op to the durable Trace.

    Returns the emitted event dict; its ``event_id`` is stamped onto the written
    segments' ``trace_ref`` (and re-derived identically on replay). ``segments_written``
    carry FULL segment dicts so replay can reconstruct ARC without prior state;
    ``content`` / ``args`` / ``text`` are in ``SENSITIVE_KEYS`` so SSE redacts them
    while the durable trace keeps them FULL. ``detail_level="off"`` => durable-only
    (these are high-volume, per-segment).

    This is the injected ``op_logger`` for ``SegmentStore`` — wired in ``build_app``
    so ``arc/`` never imports ``gact/``.

    arc.op is the WRITE-LOG of an ARC mutation — it is DERIVED from a segment write,
    not a semantic event to fold back into ARC. So it routes DIRECTLY to the durable
    trace + SSE bus via the sink, NOT through ``_emit_semantic_event``/``arc.record``.
    Feeding it back through ``arc.record_semantic_event`` would re-enter the op-logger
    (record persists a segment -> op-logger -> arc.op -> record ...) — the circularity
    that previously forced a thread-local re-entrancy guard. By deriving directly, that
    loop simply does not exist (no guard needed).
    """
    state = getattr(app, "state", None)
    sink = getattr(state, "semantic_event_sink", None)
    if sink is None:
        return {}
    event = _build_semantic_event(
        app,
        session_id,
        ARC_OP_EVENT_TYPE,
        turn_id=_ctx.active_turn_id(),
        trace_id=_ctx.active_trace_id(),
        status=op,  # append | insert | delete | summarize
        summary=f"arc {op} @{scope} (lt={logical_time})",
        actor={"role": "runtime", "component": "arc", "scope": scope},
        subject={
            "scope": scope,
            "logical_time": logical_time,
            "step": step,
            "position": position,
        },
        payload={
            "op": op,
            "scope": scope,
            "logical_time": logical_time,
            "step": step,
            "position": position,
            "segments_written": segments_written or [],
            "segments_tombstoned": segments_tombstoned or [],
            "derived_from": derived_from or [],
        },
        detail_level="off",  # durable-only (high volume); SSE goes via the publish below
    )
    # Derive DIRECTLY to the durable trace (the sink writes trace_backend; detail_level
    # "off" makes it skip the bus). NOT via arc.record — arc.op is a projection of a
    # write, never an ARC input.
    emitted = sink.emit(event)
    # The durable event is detail_level="off" (lean trace), so SemanticEventSink skips
    # the bus. Every op is captured FULL on the trace above. The SERVED UI wire only
    # wants CONTEXT MUTATIONS — when clio edits the agent's own context (compaction /
    # eviction / replace) — which is real observability ("context compacted here").
    # Plain ``append`` is per-segment write-log bookkeeping (the bulk: ~190 frames on a
    # single EarthScope turn) the UI does not render, so it stays OFF the served wire
    # (still on the durable trace). Allow-list ids/kinds/token_count only — never the
    # segment content/args/text. Observability: never break an op.
    mutates_context = op != "append" or bool(segments_tombstoned)
    if mutates_context:
        try:
            bus = getattr(app.state, "bus", None)
            if bus is not None:
                bus.publish(
                    Event(
                        type="arc.op",
                        session_id=session_id,
                        payload={
                            "op": op,
                            "scope": scope,
                            "logical_time": logical_time,
                            "step": step,
                            "position": position,
                            "segments_written": [
                                {
                                    "id": s.get("id"),
                                    "kind": s.get("kind"),
                                    "token_count": s.get("token_count"),
                                }
                                for s in (segments_written or [])
                            ],
                            "segments_tombstoned": segments_tombstoned or [],
                        },
                    )
                )
        except Exception as exc:  # noqa: BLE001 - SSE streaming is observability, never fatal
            trace.event("ARC-OP", "arc.op bus publish failed: %r", exc)
    return emitted


def _wire_arc_op_logger(app: "FastAPI") -> None:
    """Wire the durable-Trace op-logger into ``app.state.arc``'s segment store.

    Each applied context op (add/insert/remove/replace/compress) is mirrored to the
    Trace + SSE as an ``arc.op`` event; ``arc/`` stays free of any ``gact/`` import
    via this injected closure. Best-effort + idempotent.

    MUST be called whenever ``app.state.arc`` is (re)assigned — in particular the
    ASYNC ``_construct_agent_async`` path: the real agent's ``ARCMemory`` is built
    AFTER ``build_app`` runs, so the build-time wiring saw ``arc=None`` and never
    wired it. Without this, ARC writes happen (the loop's live context plane works)
    but emit NO ``arc.op`` -> the Trace/highway/interface never see the writes.
    """
    arc = getattr(getattr(app, "state", None), "arc", None)
    if arc is not None and hasattr(arc, "set_segment_op_logger"):
        try:
            arc.set_segment_op_logger(
                lambda op, session_id, scope, **kw: _emit_arc_op(app, op, session_id, scope, **kw)
            )
        except Exception as exc:  # noqa: BLE001 - observability wiring is best-effort
            trace.event("ARC-OP", "arc op-logger wiring failed: %r", exc)


def _set_app_arc(app: "FastAPI", arc: Any) -> None:
    """The single choke point for (re)assigning ``app.state.arc``.

    Sets it AND wires the arc.op op-logger onto it, so ARC writes are ALWAYS
    observable on the Trace/highway no matter which path swapped the arc (initial
    build, async agent construction, or an LM re-bind). ``arc`` may be None (no-op
    wiring). A guardrail test asserts there is no raw ``app.state.arc =`` outside
    this helper, so a future arc-swap site cannot silently drop the op-logger.

    Also publishes the ONE process-wide ARC singleton (``_PROCESS_ARC``) so every emit
    context — including deep/threaded ones that can't reach the request app — resolves the
    SAME ARC and routes through it (ARC is the source; no silent bypass).
    """
    global _PROCESS_ARC
    app.state.arc = arc
    if arc is not None:
        _PROCESS_ARC = arc
    _wire_arc_op_logger(app)
    # ARC-as-source: wire the highway-derive sink onto the arc so a recorded semantic
    # event fans out to the durable trace / SSE / hooks AFTER ARC persists+folds it.
    # The closure reads app.state.semantic_event_sink at CALL time (robust to build/
    # async/bind ordering — the sink may be constructed after this wiring runs). The
    # sink itself carries NO arc consumer (see build_app), so arc.record -> sink.emit
    # has no path back into arc.record: no recursion.
    if arc is not None and hasattr(arc, "set_highway_sink"):
        try:
            arc.set_highway_sink(
                lambda e: (
                    app.state.semantic_event_sink.emit(e)
                    if getattr(app.state, "semantic_event_sink", None) is not None
                    else {}
                )
            )
        except Exception as exc:  # noqa: BLE001 - highway wiring is best-effort
            trace.event("ARC-AS-SOURCE", "arc highway-sink wiring failed: %r", exc)


def _process_arc(app: "FastAPI") -> Any:
    """Return the ONE ARCMemory for this clio-agent, constructing it once on first use.

    ARC is a per-clio-agent keystone: exactly one per process (one ARC per clio-agent,
    N clio-agents per node, one clio-core per node). The gact server OWNS that single
    ARC's lifecycle so that every agent build/bind reuses the SAME instance — the agent
    no longer mints a fresh ARC per build (which stranded already-recorded events on an
    orphaned ARC while the shared durable trace kept them: the trace ⊋ ARC split).

    Stored on ``app.state.arc`` via ``_set_app_arc`` so a single, fail-loud path reaches
    it; rebuilt only if the app has none yet (first build).
    """
    arc = getattr(getattr(app, "state", None), "arc", None)
    if arc is not None:
        return arc
    from clio_agent.arc.memory import ARCMemory  # noqa: PLC0415
    from clio_agent.arc.storage import make_arc_store  # noqa: PLC0415

    data_dir = ".clio/agent/arc"
    arc = ARCMemory(data_dir=data_dir, cache_capacity=1000, store=make_arc_store(data_dir=data_dir))
    _set_app_arc(app, arc)
    return arc


def _coerce_error_info(value: Any) -> Optional["ErrorInfo"]:
    """Normalize agent/provider error payloads into the GACT error model."""

    if value is None:
        return None
    if isinstance(value, ErrorInfo):
        return value
    if isinstance(value, Mapping):
        raw_details = value.get("details", {})
        if isinstance(raw_details, Mapping):
            details = dict(raw_details)
        elif raw_details is None:
            details = {}
        else:
            details = {"details": raw_details}
        retry_after_raw = value.get("retry_after_s")
        retry_after_s: Optional[int] = None
        if retry_after_raw is not None:
            try:
                retry_after_s = int(retry_after_raw)
            except (TypeError, ValueError):
                details["retry_after_s"] = retry_after_raw
        return ErrorInfo(
            error=str(value.get("error") or "agent_error"),
            message=str(value.get("message") or value.get("error") or "Agent returned an error."),
            details=details,
            recoverable=bool(value.get("recoverable", True)),
            retry_after_s=retry_after_s,
        )
    return ErrorInfo(
        error="agent_error",
        message=str(value),
        recoverable=True,
    )


class _UnsupportedSessionAgent(RuntimeError):
    """Raised when a session selects an agent CLIO cannot execute yet."""

    def __init__(
        self,
        agent_id: str,
        *,
        reason: str = "unknown_or_non_executable_agent",
        tools: list[str] | None = None,
    ) -> None:
        super().__init__(agent_id)
        self.agent_id = agent_id
        self.reason = reason
        self.tools = tools or []


class _NoResolvableAgent(RuntimeError):
    """Raised when a default/main session resolves NO executable agent.

    #948 S4b: the legacy planner is DELETED, and a BARE session (nothing activated)
    runs the in-code builtin react main (``catalog._builtin_main_agent``) — so this
    marks the remaining hole: an EXPLICITLY activated blueprint (session id/path
    set) that resolves no executable agent. Fail TYPED — never a legacy pathway,
    never a silent builtin-main substitute; maps to ``no_resolvable_agent``.
    """

    def __init__(self, agent_id: str = "") -> None:
        super().__init__(agent_id)
        self.agent_id = agent_id


class _BlueprintRootDisabled(RuntimeError):
    """Raised when the active Agent Blueprint's declared root expert is disabled.

    #948 S4: a disabled root (validation errors — e.g. a pre-migration pack whose
    chain_of_thought main declares children) must fail the turn TYPED. It must
    never silently substitute another enabled expert as the root, and never fall
    through to the legacy planner pathway (both observed live before this guard).
    """

    def __init__(
        self,
        root_id: str,
        *,
        blueprint_id: str = "",
        validation_errors: list[str] | None = None,
    ) -> None:
        super().__init__(root_id)
        self.root_id = root_id
        self.blueprint_id = blueprint_id
        self.validation_errors = validation_errors or []


class _ContextFileAccessError(RuntimeError):
    """Raised when a requested session context file cannot be prepared."""

    def __init__(self, error_info: "ErrorInfo") -> None:
        super().__init__(error_info.message)
        self.error_info = error_info


class _TurnCancelled(RuntimeError):
    """Raised internally to settle a turn as cancelled without running forward."""

    def __init__(self, error_info: "ErrorInfo") -> None:
        super().__init__(error_info.message)
        self.error_info = error_info


class _TurnTimedOut(RuntimeError):
    """Raised internally when an agent turn makes no observable progress.

    ``timeout_s`` is the no-progress window (the max allowed gap between
    published progress events), not a cap on total turn duration.
    """

    def __init__(self, timeout_s: float) -> None:
        super().__init__(f"agent turn made no progress for {timeout_s:g}s")
        self.timeout_s = timeout_s


class _BlueprintTerminalWorkflowState(BaseException):
    """Raised internally when a blueprint tool observation settles a workflow.

    DSPy ReAct treats normal tool exceptions as recoverable observations, so a
    terminal typed workflow state needs to bypass that catch path and settle at
    the blueprint module boundary.
    """

    def __init__(self, result: Mapping[str, Any]) -> None:
        super().__init__("blueprint tool returned terminal workflow state")
        self.result = dict(result)


def _not_implemented(capability: str) -> ErrorEnvelope:
    """Build the v0.2 error envelope for a 501 response."""

    return ErrorEnvelope(
        error=ErrorInfo(
            error="config_error",
            message=f"capability not yet implemented: {capability}",
            details={
                "capability": capability,
                "note": (
                    "This endpoint is stubbed; it will "
                    "be wired in a follow-on iteration. See "
                    "gact-tui/PLAN.md for the roadmap."
                ),
            },
            recoverable=False,
        )
    )


def _cancelled_error_info(
    sid: str,
    *,
    execution_cancellation: str,
    executor_work_may_continue: bool,
) -> "ErrorInfo":
    """Return the structured ``ErrorInfo`` for a client-cancelled turn."""
    return ErrorInfo(
        error="cancelled",
        message="turn cancelled by client",
        details={
            "session_id": sid,
            "execution_cancellation": execution_cancellation,
            "executor_work_may_continue": executor_work_may_continue,
        },
        recoverable=True,
    )
