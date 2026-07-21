"""Process-wide LM-call activity tracker.

A DSPy callback marks when a ``dspy.LM.__call__`` is in flight so the GACT
no-progress watchdog can treat an *actively generating* model as progress.

Why this exists: a deep-reasoning model (qwopus, nemotron, …) streams its
chain-of-thought on a separate ``reasoning_content`` channel and can emit tens
of thousands of reasoning tokens with ZERO answer-content tokens before it
finishes. DSPy's stream listeners only watch answer content, and an expert child
runs the LM call *synchronously* in an executor (no live deltas at all), so the
turn publishes no bus events for the whole think -- and the no-progress watchdog
wrongly kills a model that is working as hard as it can (the EarthScope resolver
hang: >27k reasoning tokens, killed at the 900s window while still generating).

The tracker attributes each in-flight call to the session that owns it: the
``note_lm_*`` callbacks run in the turn/executor context where the GACT session
id is set (:func:`clio_agent.gact.context.active_session_id`), so the watchdog
can ask "is THIS session's LM call generating right now" rather than "is ANY
call generating anywhere". Per-session attribution is required: a global-any
signal let a busy neighbor session B keep a genuinely wedged session A alive
forever, disabling the stuck-turn guardrail exactly when a second session was
active (iowarp/clio-agent#761 defect 2). Calls made off-turn (CLI/optimizer,
no session in context) land in the unattributed ``""`` bucket; a session-scoped
query never sees another session's — or the unattributed — bucket.

A per-call ceiling / inter-token idle gate still bounds a genuinely wedged call
within its own bucket, so the watchdog fires when a provider truly hangs (no
tokens, dead socket) even for the session that owns the call.
"""

from __future__ import annotations

import asyncio
import threading
import time
from contextvars import ContextVar
from typing import Any

from clio_agent import conf
from clio_agent.runtime import trace
from clio_agent.runtime.stream_audit import stream_audit

_LOCK = threading.Lock()
# Per-session in-flight tracker. Keyed by GACT session id; the ``""`` key holds
# calls made off-turn (CLI/optimizer) or before a session is bound. Each bucket
# is an independent ``{inflight, started, last}`` record so one session's call
# never counts as another's progress (iowarp/clio-agent#761 defect 2).
_STATE: dict[str, dict[str, float]] = {}


def _new_bucket() -> dict[str, float]:
    """A fresh, empty in-flight record for one session."""
    return {"inflight": 0.0, "started": 0.0, "last": 0.0}


def _bucket(session_id: str) -> dict[str, float]:
    """Return the mutable in-flight record for ``session_id`` (created on demand).

    Callers must hold ``_LOCK``.
    """
    st = _STATE.get(session_id)
    if st is None:
        st = _new_bucket()
        _STATE[session_id] = st
    return st


def _active_lm_session() -> str:
    """Resolve the GACT session that owns the current LM call.

    Reads the turn/executor context var set by the GACT turn machinery. The
    import is deferred because ``clio_agent.gact`` transitively imports this
    module (the turn wires in the DSPy callback), so a module-level import would
    cycle; by the time any ``note_lm_*`` fires during a turn, ``gact`` is already
    loaded and this is a cheap ``sys.modules`` hit. Off-turn callers (CLI,
    optimizer) have no session bound and fall to the unattributed ``""`` bucket.
    """
    try:
        from clio_agent.gact.context import active_session_id  # noqa: PLC0415

        return active_session_id() or ""
    except Exception:  # noqa: BLE001 - context unavailable off-turn -> unattributed
        return ""


# --- Unified LM token highway (#693) -----------------------------------------
# The single LM-stream tap (config.IOLoggingLM._clio_streamed_call) feeds the live
# answer text to the SAME chat publisher (_emit_chunk) so a blueprint/expert call
# running in an executor thread streams to the UI exactly like a chat turn — no
# separate streaming path. The turn sets (loop, async _emit_chunk) here; the tap
# schedules the answer delta onto that loop. ContextVar so it's copied into the
# executor that runs the expert (and is naturally absent off-turn = no-op).
_LIVE_CHUNK_EMITTER: ContextVar[tuple[Any, Any, Any] | None] = ContextVar(
    "clio_live_chunk_emitter", default=None
)


def set_live_chunk_emitter(loop: Any, emit_coro: Any, record_dedup: Any = None) -> None:
    """Bind the turn's (event loop, async answer-chunk publisher, tap-dedup recorder).

    ``record_dedup`` is the turn transcript's synchronous
    ``record_streamed_field_text`` (#732): the tap calls it IN-THREAD before
    scheduling the cross-thread emit, so the same-thread tool observer's
    thought-dedup gate reads a source with a real happens-before instead of
    racing the loop's asynchronous ledger append.

    The binding is a ContextVar set in the turn's context: it is copied into the
    executor that runs the expert and dies with the turn's context — no explicit
    reset is needed (or provided)."""
    _LIVE_CHUNK_EMITTER.set((loop, emit_coro, record_dedup))


def note_suppressed_extract_field(
    field: str, text: str, *, agent_id: str, kind: str, session_id: str = ""
) -> None:
    """Record a ``kind: react`` EXTRACT-field suppression reason (#878).

    Shared by both visible-emit seams (this module's live tap and
    ``streaming._emit_visible_chunk``) so the no-silent-fallback record is emitted
    identically wherever a react ``reasoning``/``answer`` field is dropped.
    """
    stream_audit(
        "bridge.contract_field",
        agent_id=agent_id or "",
        session_id=session_id,
        field=field,
        chunk_len=len(text),
        visible=False,
        duplicate_suppressed=True,
        duplicate_reason="react_extract_field_suppressed",
        module_kind=kind,
        head=text[:120],
        full_text=text[:12000],
    )


def note_lm_answer_delta(text: str, *, field: str = "answer") -> None:
    """Stream a generated output-field delta to the live UI via the turn publisher.

    Scheduled cross-thread onto the turn's loop (the tap runs in an executor), so
    it reuses _emit_chunk's message/part bookkeeping and the final message
    reconciles to ONE assistant message. No-op off-turn or when nothing is bound.
    The text is not interpreted, summarized, or hidden here; ``field`` is carried
    as metadata so renderers can distinguish workflow/status output from final
    prose without losing the raw generated tokens.
    """
    if not text:
        return
    emitter = _LIVE_CHUNK_EMITTER.get()
    if not emitter:
        return
    loop, emit_coro, record_dedup = emitter
    # Attribute this delta to the expert whose LM call produced it. The tap runs in
    # the executor thread where the expert's react scope contextvar is set, so the
    # author is known here; the chat publisher splits parts when it changes (WS3).
    agent_id = ""
    try:
        from clio_agent.gact.context import (  # noqa: PLC0415
            active_react_kind,
            active_react_scope,
            active_visible_answer_stream,
            react_extract_field_suppressed,
        )

        agent_id = active_react_scope()
        kind = active_react_kind()
        # No-silent-fallback: a react scope is active but its kind never resolved.
        # Do NOT assume not-react (would leak a react EXTRACT) nor blanket-suppress
        # (would delete a CoT expert's visible reasoning). Keep today's safe
        # CoT-visible behavior below and record the resolution miss so it surfaces
        # at the seam rather than hiding (#878).
        if agent_id and not kind:
            stream_audit(
                "bridge.contract_field",
                agent_id=agent_id,
                field=field,
                chunk_len=len(text),
                visible=False,
                normalized_event="",
                duplicate_suppressed=False,
                duplicate_reason="react_kind_unresolved",
                head=text[:120],
                full_text=text[:12000],
            )
        # #878: a `kind: react` expert's redundant EXTRACT reasoning/answer is
        # gated STRUCTURALLY on the declared module.kind (never the field name).
        # answer_is_deliverable=False here: a nested react expert's answer VALUE
        # flows to the delegation return contract, and a top-level react
        # responder's deliverable is re-added once at finalize via answer_channel —
        # so the tap copy is always redundant.
        if react_extract_field_suppressed(kind, field, answer_is_deliverable=False):
            note_suppressed_extract_field(field, text, agent_id=agent_id, kind=kind)
            return
        visible_fields = {"reasoning", "next_thought"}
        if field == "answer" and active_visible_answer_stream():
            visible_fields.add("answer")
        if field not in visible_fields:
            stream_audit(
                "bridge.contract_field",
                agent_id=agent_id or "",
                field=field,
                chunk_len=len(text),
                visible=False,
                normalized_event="",
                duplicate_suppressed=True,
                duplicate_reason="nonvisible_contract_field",
                head=text[:120],
                full_text=text[:12000],
            )
            trace.HF_ON and trace.hot(
                "STREAM-SSE",
                "record_nonvisible_contract_field agent=%s field=%s len=%d head=%r",
                agent_id or "",
                field,
                len(text),
                text[:80],
            )
            return
    except Exception:  # noqa: BLE001 - scope unavailable off-turn (CLI/optimizer)
        agent_id = ""
    try:
        # #732 (S2): record the streamed field text into the turn transcript's
        # tap-dedup buffer SYNCHRONOUSLY, in THIS executor thread, before the
        # cross-thread emit is scheduled below. RELOCATED here (was unconditional,
        # before the visible_fields gate) so the tap records presence IFF this field
        # is actually emitted as a VISIBLE text row: a non-visible contract field
        # returns above and never reaches here. This ties tap-presence to visible
        # emission — tap(agent,field) non-empty ⟺ a visible row was emitted — so the
        # tool observer's op-identity thought-dedup gate can never clear a thought
        # whose next_thought has no visible row (the attempt-1 vanish). The tool
        # observer runs in this SAME executor thread when a tool fires, so it reads a
        # source written before the tool call — the happens-before the retired
        # ``live_streamed_field_text`` buffer provided. Skipped off-scope (agent_id
        # empty), matching the old buffer's guard.
        if record_dedup is not None and agent_id:
            try:
                # #953: the DEDUP key is run-partitioned under an in-process variant
                # (BestOfN/Refine try N gets its own tap bucket, never fed try N-1's
                # thoughts) — the tool observer reads the same run-keyed scope. The
                # VISIBLE emit below keeps the BARE agent_id for attribution.
                from clio_agent.gact.context import run_keyed_scope  # noqa: PLC0415

                record_dedup(run_keyed_scope(agent_id), field, text)
            except Exception:  # noqa: BLE001,S110 - dedup capture is best-effort
                pass
        stream_audit(
            "bridge.contract_field",
            agent_id=agent_id or "",
            field=field,
            chunk_len=len(text),
            visible=True,
            normalized_event="turn.text.delta",
            duplicate_suppressed=False,
            head=text[:120],
            full_text=text[:12000],
        )
        trace.HF_ON and trace.hot(
            "STREAM-SSE",
            "publish_contract_field agent=%s field=%s len=%d head=%r",
            agent_id or "",
            field,
            len(text),
            text[:80],
        )
        asyncio.run_coroutine_threadsafe(emit_coro(text, agent_id or None, field), loop)
    except Exception:  # noqa: BLE001,S110 - live streaming is best-effort, never break the call
        pass


def note_lm_provider_thinking_delta(text: str, *, provider: str = "") -> None:
    """Stream provider-internal thinking/debug deltas as a collapsed thinking part.

    This is intentionally separate from DSPy contract fields. For Claude Code SDK,
    ``thinking_delta`` is hidden provider thinking, while the visible contract prose
    arrives later on the text channel as ``[[ ## reasoning ## ]]`` or
    ``[[ ## next_thought ## ]]``.
    """
    if not text:
        return
    emitter = _LIVE_CHUNK_EMITTER.get()
    if not emitter:
        return
    loop, emit_coro, _record_dedup = emitter
    agent_id = ""
    try:
        from clio_agent.gact.context import active_react_scope  # noqa: PLC0415

        agent_id = active_react_scope()
    except Exception:  # noqa: BLE001 - scope unavailable off-turn
        agent_id = ""
    field = f"provider_thinking:{provider or 'provider'}"
    try:
        stream_audit(
            "bridge.provider_aux",
            agent_id=agent_id or "",
            provider=provider or "provider",
            field=field,
            chunk_len=len(text),
            normalized_event="turn.trace.delta",
            duplicate_suppressed=False,
            head=text[:120],
            full_text=text[:12000],
        )
        asyncio.run_coroutine_threadsafe(emit_coro(text, agent_id or None, field), loop)
    except Exception:  # noqa: BLE001,S110 - live streaming is best-effort, never break the call
        pass


def note_lm_token_event(content: str, reasoning: str, *, field: str = "answer") -> None:
    """Emit an ``lm.token.delta`` semantic event onto the highway (durable trace +
    ARC live fold). Answer text rides ``delta`` (reaches SSE); reasoning rides the
    redacted ``reasoning`` key (heartbeat on SSE, full in trace). Best-effort."""
    if not content and not reasoning:
        return
    try:
        from clio_agent.gact.context import (  # noqa: PLC0415
            active_app,
            active_session_id,
            active_trace_id,
            active_turn_id,
        )
        from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415
        from clio_agent.gact.semantic_events import (  # noqa: PLC0415
            LM_TOKEN_DELTA,
            lm_token_delta_payload,
        )
    except Exception:  # noqa: BLE001 - app unavailable (CLI/optimizer paths)
        return
    app = active_app()
    sid = active_session_id()
    if app is None or not sid:
        return
    try:
        _emit_semantic_event(
            app,
            sid,
            LM_TOKEN_DELTA,
            turn_id=active_turn_id(),
            trace_id=active_trace_id(),
            status="running",
            summary="LM token delta.",
            payload=lm_token_delta_payload(content=content, reasoning=reasoning, field=field),
            detail_level="semantic",
        )
    except Exception:  # noqa: BLE001,S110 - capture must never break a call
        pass


# Hard ceiling on how long a single in-flight LM call is trusted as "progress".
# Past this, the call is assumed wedged and stops refreshing the watchdog so the
# turn can be aborted. Generous by default: a local 9B reasoning model generating
# toward a large max_tokens at ~30 tok/s can legitimately run ~15-20 min.
_DEFAULT_MAX_LM_CALL_S = 1800.0

# Inter-token idle ceiling: when the call STREAMS (token-liveness on), a call is
# trusted only while a token arrived within this window. Generous enough to cover
# large-context first-token (prefill) latency on a local card, but far below the
# turn no-progress window so a true 0-token stall is caught fast.
_DEFAULT_INTER_TOKEN_IDLE_S = 120.0


def _max_lm_call_seconds() -> float:
    try:
        value = conf.resolve(
            "limits.lm_call_s",
            env="CLIO_MAX_LM_CALL_S",
            default=_DEFAULT_MAX_LM_CALL_S,
            cast=conf.as_float,
        )
    except (ValueError, TypeError):
        return _DEFAULT_MAX_LM_CALL_S
    return value if value > 0 else _DEFAULT_MAX_LM_CALL_S


def _inter_token_idle_seconds() -> float:
    try:
        value = conf.resolve(
            "limits.lm_inter_token_idle_s",
            env="CLIO_LM_INTER_TOKEN_IDLE_S",
            default=_DEFAULT_INTER_TOKEN_IDLE_S,
            cast=conf.as_float,
        )
    except (ValueError, TypeError):
        return _DEFAULT_INTER_TOKEN_IDLE_S
    return value if value > 0 else _DEFAULT_INTER_TOKEN_IDLE_S


def _emit_lm_call_started(call_id: Any, instance: Any, inputs: Any) -> None:
    """Emit a durable ``lm.call.started`` BEFORE the call returns.

    The S4b ``lm.call`` capture (config.IOLoggingLM) logs in ``finally`` AFTER
    the call returns, so a HUNG call (LM Studio stall) is never recorded -- a
    hole in the canonical trace. This start-marker carries the full request
    ``messages`` up front, so a stall shows up as a ``lm.call.started`` with no
    matching ``lm.call`` (completed): the wedged call and its exact input are
    first-class in the trace, not reconstructed. Durable-only (detail off),
    best-effort, never breaks a call.
    """

    try:
        from clio_agent.gact.context import (  # noqa: PLC0415
            active_app,
            active_session_id,
            active_trace_id,
            active_turn_id,
        )
        from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - app may be unavailable (CLI/optimizer paths)
        return
    app = active_app()
    sid = active_session_id()
    if app is None or not sid:
        return
    model = str(getattr(instance, "model", "") or "")
    messages: Any = None
    if isinstance(inputs, dict):
        messages = inputs.get("messages") or inputs.get("prompt")
    try:
        _emit_semantic_event(
            app,
            sid,
            "lm.call.started",
            turn_id=active_turn_id(),
            trace_id=active_trace_id(),
            status="running",
            summary=f"LM call started ({model or 'lm'}).",
            provider={"model_id": model},
            payload={"call_id": str(call_id), "model": model, "messages": messages},
            detail_level="off",
        )
    except Exception:  # noqa: BLE001,S110 - capture must never break a call
        pass


def note_lm_start() -> None:
    """Register an LM call as in flight, attributed to the active session."""
    key = _active_lm_session()
    with _LOCK:
        st = _bucket(key)
        st["inflight"] += 1
        now = time.monotonic()
        st["started"] = now
        st["last"] = now


def note_lm_activity() -> None:
    """Refresh the active session's last-activity timestamp -- called per streamed
    token/chunk.

    When token-liveness streaming is on, this turns ``lm_call_in_flight`` into an
    inter-token-idle gate: a slow-but-generating reasoning model keeps refreshing
    the watchdog on every chunk, while a genuinely frozen call (0 tokens) stops
    refreshing and is aborted fast.
    """
    key = _active_lm_session()
    with _LOCK:
        _bucket(key)["last"] = time.monotonic()


def note_lm_end() -> None:
    """Release the active session's in-flight count for one completed LM call.

    When the count drains to zero the bucket is DROPPED (it is recreated on the
    next ``note_lm_start``), so ``_STATE`` cannot grow unbounded across the
    process lifetime: an idle session must not leave a permanent record. This is
    the same no-unbounded-growth rule that motivated #757 (iowarp/clio-agent#761
    made ``_STATE`` per-session, which reintroduced that risk without eviction).
    """
    key = _active_lm_session()
    with _LOCK:
        st = _bucket(key)
        st["inflight"] = max(0.0, st["inflight"] - 1)
        if st["inflight"] <= 0:
            # Drained -> drop the bucket. A missing bucket reads as not-in-flight
            # (lm_call_in_flight), and a drained bucket's timestamps are never
            # consulted as progress, so eviction is semantically transparent.
            _STATE.pop(key, None)
        else:
            st["last"] = time.monotonic()


def _bucket_in_flight(st: dict[str, float]) -> bool:
    """True when one session's in-flight record still counts as progress.

    Two regimes, picked by whether a token has actually streamed:
    - STREAMING (``last`` > ``started`` -- ``note_lm_activity`` fired): the
      inter-token idle window is authoritative. A genuinely generating reasoning
      model keeps refreshing it per chunk and is never false-killed however long
      it runs; a frozen (0-token) call stops refreshing and is abandoned at the
      idle window. The coarse per-call ceiling does NOT apply here -- it only ever
      existed because there was no finer wedge signal, which streaming now provides.
    - NON-STREAMING / pre-first-token (``last`` == ``started``): no per-token
      signal, so fall back to the per-call ceiling (wedge backstop). This also
      covers prefill latency before the first token, so a still-prefilling call is
      never false-killed at the idle window.
    """
    if st["inflight"] <= 0:
        return False
    now = time.monotonic()
    if st["last"] > st["started"]:
        return (now - st["last"]) < _inter_token_idle_seconds()
    return (now - st["started"]) < _max_lm_call_seconds()


def lm_call_in_flight(session_id: str | None = None) -> bool:
    """True when an LM call is actively in flight and counts as progress.

    When ``session_id`` is given, answers strictly for that session's bucket, so
    the no-progress watchdog attributes an in-flight call only to the turn that
    owns it -- a busy neighbor session can never keep a wedged session alive
    (iowarp/clio-agent#761 defect 2). When ``session_id`` is ``None`` (off-turn
    callers with no session in hand), falls back to global-any: True if any
    session's bucket is in flight.

    The per-session regime (streaming inter-token idle vs. non-streaming per-call
    ceiling) is unchanged; see :func:`_bucket_in_flight`. The turn-level
    no-progress watchdog / turn timeout remain the ultimate backstop above it.
    """
    with _LOCK:
        if session_id is not None:
            st = _STATE.get(session_id)
            return st is not None and _bucket_in_flight(st)
        return any(_bucket_in_flight(st) for st in _STATE.values())


def build_dspy_callback() -> Any | None:
    """Return a DSPy ``BaseCallback`` feeding the tracker, or ``None`` if DSPy's
    callback API is unavailable (older DSPy / import failure -- non-fatal)."""

    try:
        from dspy.utils.callback import BaseCallback  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - callbacks are best-effort liveness, never required
        return None

    class _LMActivityCallback(BaseCallback):  # type: ignore[misc, valid-type]
        def on_lm_start(self, call_id, instance, inputs):  # noqa: ANN001, D102
            note_lm_start()

        def on_lm_end(self, call_id, outputs, exception):  # noqa: ANN001, D102
            note_lm_end()
            # Diagnostic (reasoning-model react-leaf): log what the LM actually
            # returned so we can see whether `content` is empty and the tool
            # decision was lost to `reasoning_content`. HIGH_FREQ trace; the legacy
            # CLIO_LOG_LM_RESPONSE flag still enables it (folded into the trace
            # whitelist as the `lm_response` tag). Best-effort, never raises.
            if trace.HF_ON:
                try:
                    items = outputs if isinstance(outputs, (list, tuple)) else [outputs]
                    parts = []
                    for it in items[:2]:
                        if isinstance(it, str):
                            txt = it
                        elif isinstance(it, dict):
                            txt = str(it.get("text") or it.get("content") or it)
                        else:
                            txt = repr(getattr(it, "text", it))
                        parts.append(f"len={len(txt)} head={txt[:300]!r}")
                    trace.hot(
                        "LM-RESPONSE",
                        "call=%s n=%s :: %s",
                        call_id,
                        len(items),
                        " || ".join(parts),
                    )
                except Exception:  # noqa: BLE001,S110 - diagnostic only
                    pass

    return _LMActivityCallback()
