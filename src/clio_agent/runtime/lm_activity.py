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

The tracker is process-global on purpose: the watchdog is a coarse liveness net,
not a per-session SLA, and "is ANY LM call generating right now" is a sound
liveness proxy. A per-call ceiling bounds a genuinely wedged call so the watchdog
still fires when a provider truly hangs (no tokens, dead socket).
"""

from __future__ import annotations

import threading
import time
from typing import Any

from clio_agent import conf
from clio_agent.runtime import trace

_LOCK = threading.Lock()
_STATE: dict[str, float] = {"inflight": 0.0, "started": 0.0, "last": 0.0}

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
        from clio_agent.gact.app import (  # noqa: PLC0415
            _ACTIVE_GACT_APP,
            _ACTIVE_GACT_SESSION_ID,
            _ACTIVE_GACT_TRACE_ID,
            _ACTIVE_GACT_TURN_ID,
            _emit_semantic_event,
        )
    except Exception:  # noqa: BLE001 - app may be unavailable (CLI/optimizer paths)
        return
    app = _ACTIVE_GACT_APP.get()
    sid = _ACTIVE_GACT_SESSION_ID.get()
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
            turn_id=_ACTIVE_GACT_TURN_ID.get(),
            trace_id=_ACTIVE_GACT_TRACE_ID.get(),
            status="running",
            summary=f"LM call started ({model or 'lm'}).",
            provider={"model_id": model},
            payload={"call_id": str(call_id), "model": model, "messages": messages},
            detail_level="off",
        )
    except Exception:  # noqa: BLE001 - capture must never break a call
        pass


def note_lm_start() -> None:
    with _LOCK:
        _STATE["inflight"] += 1
        now = time.monotonic()
        _STATE["started"] = now
        _STATE["last"] = now


def note_lm_activity() -> None:
    """Refresh the last-activity timestamp -- called per streamed token/chunk.

    When token-liveness streaming is on, this turns ``lm_call_in_flight`` into an
    inter-token-idle gate: a slow-but-generating reasoning model keeps refreshing
    the watchdog on every chunk, while a genuinely frozen call (0 tokens) stops
    refreshing and is aborted fast.
    """
    with _LOCK:
        _STATE["last"] = time.monotonic()


def note_lm_end() -> None:
    with _LOCK:
        _STATE["inflight"] = max(0.0, _STATE["inflight"] - 1)
        _STATE["last"] = time.monotonic()


def lm_call_in_flight() -> bool:
    """True when an LM call is actively in flight and counts as progress.

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

    The turn-level no-progress watchdog / turn timeout remain the ultimate backstop
    above either regime.
    """
    with _LOCK:
        if _STATE["inflight"] <= 0:
            return False
        now = time.monotonic()
        if _STATE["last"] > _STATE["started"]:
            return (now - _STATE["last"]) < _inter_token_idle_seconds()
        return (now - _STATE["started"]) < _max_lm_call_seconds()


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
                except Exception:  # noqa: BLE001 - diagnostic only
                    pass

    return _LMActivityCallback()
