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

import os
import threading
import time
from typing import Any

_LOCK = threading.Lock()
_STATE: dict[str, float] = {"inflight": 0.0, "started": 0.0, "last": 0.0}

# Hard ceiling on how long a single in-flight LM call is trusted as "progress".
# Past this, the call is assumed wedged and stops refreshing the watchdog so the
# turn can be aborted. Generous by default: a local 9B reasoning model generating
# toward a large max_tokens at ~30 tok/s can legitimately run ~15-20 min.
_DEFAULT_MAX_LM_CALL_S = 1800.0


def _max_lm_call_seconds() -> float:
    raw = os.environ.get("CLIO_MAX_LM_CALL_S", "").strip()
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return _DEFAULT_MAX_LM_CALL_S


def note_lm_start() -> None:
    with _LOCK:
        _STATE["inflight"] += 1
        now = time.monotonic()
        _STATE["started"] = now
        _STATE["last"] = now


def note_lm_end() -> None:
    with _LOCK:
        _STATE["inflight"] = max(0.0, _STATE["inflight"] - 1)
        _STATE["last"] = time.monotonic()


def lm_call_in_flight() -> bool:
    """True when an LM call is actively in flight and within the per-call ceiling.

    The ceiling is wedge protection: once a single call exceeds it, this returns
    False so the no-progress watchdog regains authority and can abort a provider
    that has genuinely hung.
    """
    with _LOCK:
        if _STATE["inflight"] <= 0:
            return False
        return (time.monotonic() - _STATE["started"]) < _max_lm_call_seconds()


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

    return _LMActivityCallback()
