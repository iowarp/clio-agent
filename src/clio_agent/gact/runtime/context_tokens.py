"""Token / context-window leaf machinery for the GACT server (#714 decomposition).

This module owns the pure, leaf-level helpers that measure prompt-token usage and
resolve a model's context window -- the inputs the expert forward's
auto-compaction reasons over. It is deliberately a *leaf*: it imports only
``dspy`` / ``litellm`` (lazily, inside functions), stdlib, the config resolver,
and the bundled ``model_limits.json`` -- and has **zero** ``app.state`` coupling.
Folding it out before the heavily-coupled expert runtime keeps that later move
free of any ``app.py`` import.

Responsibilities:

* :func:`_last_prompt_tokens` -- the LAST LM call's prompt-token count (current
  window fullness), provider-exact when available, client-side otherwise.
* :func:`_resolve_expert_context_window` -- the auto-compaction denominator.
* :func:`_estimate_text_tokens` -- best-effort token count for arbitrary text.
* :func:`_autocompact_threshold` -- the configurable trigger fraction.
* :func:`_bucket_context_categories` + :data:`_CONTEXT_CATEGORY` -- map ARC's
  per-kind token attribution into ``/context``-style buckets.
* :func:`_arc_obs_value` -- coerce a tool observation into a serializable payload.
"""

from __future__ import annotations

from typing import Any

from clio_agent import conf
from clio_agent.arc.segments import _encode_safe


def _arc_obs_value(value: Any) -> Any:
    """Coerce a tool observation into a msgpack-serializable segment payload.

    Delegates to the ONE shared ingest coercion
    :func:`clio_agent.arc.segments._encode_safe` (#737 S2, caveat a). Before the
    unification this had its own shallow rule — JSON-natives passed through and
    ANYTHING exotic collapsed to ``str(value)`` — which DIVERGED from the log
    encoder ``_encode_safe`` (which recursively coerces dicts/lists/tuples/sets/
    pydantic/dataclass and only ``str()``s as a last resort). Two encoders over the
    same observation meant a working-set segment and its ``_events`` log twin could
    not be byte-identical, so the working-set could not be re-derived as a FOLD of
    the log. Routing both through ``_encode_safe`` retires the split: the observation
    a working-set atom stores is now byte-identical to the coercion every other
    log write uses, and the ~4-chars/token heuristic over it does not drift.

    Args:
        value: A raw tool observation of any type.

    Returns:
        The value coerced to a plain msgpack/JSON-native form (never raises).
    """
    return _encode_safe(value)


def _autocompact_threshold() -> float:
    """The configurable 90%-style auto-compaction trigger fraction (0..1].

    Resolved via the config store (file → env ``CLIO_AUTOCOMPACT_PCT`` → default
    0.85); the design recommends compacting below 0.90 so the summary is built
    from fuller context. A non-numeric or out-of-range value falls back to the
    default (lenient by design: this is a soft tuning knob, not a hard contract,
    and a typo must never wedge the auto-compaction path).
    """
    raw = conf.resolve(
        "autocompact.pct",
        env="CLIO_AUTOCOMPACT_PCT",
        default=0.85,
    )
    try:
        v = conf.as_float(raw)
    except (ValueError, TypeError):
        return 0.85
    return v if 0.0 < v <= 1.0 else 0.85


def _last_prompt_tokens() -> int:
    """The LAST LM call's prompt-token count (0 if it cannot be determined).

    Each send is the full prompt, so this single value IS current window fullness
    (not a running sum). Primary source is the provider-exact ``prompt_tokens`` from
    ``dspy.track_usage()``. Some providers (e.g. the ALCF/Argonne vLLM endpoint)
    report ``prompt_tokens: 0`` — in that case we fall back to a client-side
    ``litellm.token_counter`` over the LAST call's actual messages
    (``lm.history[-1]['messages']``). Approximate for non-tiktoken local models, but
    non-zero and monotonic with context growth, which is what the threshold needs.
    """
    import dspy  # noqa: PLC0415

    from clio_agent.gact.runtime.ambient_lm import resolve_active_lm  # noqa: PLC0415

    # Resolve the LM through the ambient guard: inside an expert/main
    # ``dspy.context`` this is the bound profile LM (the normal auto-compaction
    # path); outside one it falls through to the boot default AND records a
    # structured ``ambient_lm_default`` reason so the miss is queryable (#818).
    lm = resolve_active_lm(site="context_tokens._last_prompt_tokens")
    model = str(getattr(lm, "model", "") or "")

    # 1. Provider-exact prompt_tokens from the usage tracker.
    tracker = getattr(dspy.settings, "usage_tracker", None)
    data = getattr(tracker, "usage_data", None) if tracker is not None else None
    if data:
        try:
            entries = data.get(model)
            if not entries:  # fall back to the most-recently-populated model
                for v in reversed(list(data.values())):
                    if v:
                        entries = v
                        break
            pt = int(entries[-1].get("prompt_tokens", 0) or 0) if entries else 0
            if pt > 0:
                return pt
        except Exception:  # noqa: BLE001,S110 - cached prompt-token lookup best-effort; falls through
            pass

    # 2. Provider didn't report prompt_tokens: count the last call's real messages.
    try:
        history = getattr(lm, "history", None)
        messages = history[-1].get("messages") if history else None
        if messages:
            import litellm  # noqa: PLC0415

            return int(litellm.token_counter(model=model, messages=messages))
    except Exception:  # noqa: BLE001,S110 - litellm token_counter optional; returns 0
        pass
    return 0


# ARC segment kind -> Claude-Code-``/context``-style category for the per-agent breakdown.
_CONTEXT_CATEGORY: dict[str, str] = {
    "system": "system",
    "user": "messages",
    "tool_def": "tools",
    "thought": "reasoning",
    "tool_call": "tool_calls",
    "observation": "observations",
    "summary": "summary",
    "lm_io": "io",
    "extract_io": "io",
    "answer": "io",
}


def _bucket_context_categories(
    tokens_by_kind: dict[str, int], used_tokens: int, live_tokens: int
) -> dict[str, int]:
    """Bucket ARC's per-kind token attribution into ``/context``-style categories, plus a
    ``framing`` entry = ``used_tokens - live_tokens`` (the system-prompt + tool-schema
    overhead the model sees but ARC does not store/edit), when the model-grounded
    ``used_tokens`` is known and exceeds the ARC-attributed ``live_tokens``."""
    cats: dict[str, int] = {}
    for kind, toks in tokens_by_kind.items():
        cat = _CONTEXT_CATEGORY.get(kind, "other")
        cats[cat] = cats.get(cat, 0) + int(toks)
    if used_tokens > 0 and (used_tokens - live_tokens) > 0:
        cats["framing"] = used_tokens - live_tokens
    return {k: v for k, v in cats.items() if v}


def _estimate_text_tokens(text: str) -> int:
    """Best-effort token count for a piece of text via the active model's tokenizer
    (``litellm.token_counter``), falling back to a ~4-chars/token heuristic."""
    if not text:
        return 0
    try:
        import litellm  # noqa: PLC0415

        from clio_agent.gact.runtime.ambient_lm import resolve_active_lm  # noqa: PLC0415

        # Bound profile LM inside a ``dspy.context``; boot default (recorded as an
        # ``ambient_lm_default`` reason) outside one — never a silent ambient read.
        lm = resolve_active_lm(site="context_tokens._estimate_text_tokens")
        model = str(getattr(lm, "model", "") or "")
        if model:
            return int(litellm.token_counter(model=model, text=text))
    except Exception:  # noqa: BLE001,S110 - tokenizer optional; falls back to ~4-chars/token
        pass
    return max(1, len(text) // 4)


def _resolve_expert_context_window(cfg: Any) -> int:
    """Resolve the expert model's context window (the auto-compaction denominator).

    Ladder: (1) handshake-discovered ``chosen_context``/``context_window`` on the
    config; (2) ``litellm.get_model_info`` max input tokens; (3) the ``context``
    field in ``model_limits.json``. Returns 0 when unknown (auto-compaction stays
    off; dspy's reactive truncation remains the backstop).
    """
    for attr in ("chosen_context", "context_window"):
        v = getattr(cfg, attr, None)
        if v:
            return int(v)
    model = str(getattr(cfg, "model", "") or "")
    if not model:
        return 0
    try:
        import litellm  # noqa: PLC0415

        info = litellm.get_model_info(model) or {}
        v = info.get("max_input_tokens") or info.get("max_tokens")
        if v:
            return int(v)
    except Exception:  # noqa: BLE001,S110 - litellm model-info optional; tries the next source
        pass
    try:
        import json  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        # ``__file__`` is ``clio_agent/gact/runtime/context_tokens.py``; the bundled
        # limits live under ``clio_agent/providers/...`` -> parents[2] == clio_agent/.
        limits_path = (
            Path(__file__).resolve().parents[2]
            / "providers"
            / "handshake"
            / "sources"
            / "data"
            / "model_limits.json"
        )
        entry = json.loads(limits_path.read_text()).get(model) or {}
        v = entry.get("context")
        if v:
            return int(v)
    except Exception:  # noqa: BLE001,S110 - bundled model_limits lookup best-effort; returns 0 (auto-compaction off)
        pass
    return 0
