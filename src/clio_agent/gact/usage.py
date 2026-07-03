"""Usage / cost metering for the GACT server (#714).

Behavior-preserving extraction from :mod:`clio_agent.gact.app`. This module is the
single source of truth for rolling a turn's token usage and cost up out of DSPy's
per-LM ``history``, plus the best-effort per-token price table used when an
upstream provider doesn't report a cost.

The turn engine snapshots ``len(lm.history)`` for every known LM at turn start
(:func:`_snapshot_lm_history_index`), then diffs the slice at turn end
(:func:`_usage_from_history_slice`, :func:`_reasoning_records_from_history_slice`)
so planner + every expert + chat token counts (and reasoning traces) roll up
across the whole turn. The history diff IS the path the turn uses: ``lm.history``
is shared across threads (list.append under the GIL), so it survives the
executor-thread + streaming hops that make ``dspy.settings.usage_tracker``
unreliable from worker threads.

The module imports only stdlib plus :mod:`clio_agent.gact.runtime.globals` (for the
single-owner :func:`_entry_reasoning_text` reasoning-channel extractor). It never
imports :mod:`clio_agent.gact.app`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

# Single source of truth for the reasoning-channel extractor: it already lives in
# runtime.globals (where _active_lm_last_reasoning consumes it). Reuse it here
# instead of carrying a second copy.
from clio_agent.gact.runtime.globals import _entry_reasoning_text

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

__all__ = [
    "_all_known_lms",
    "_snapshot_lm_history_index",
    "_usage_from_history_slice",
    "_entry_reasoning_text",
    "_entry_response_text",
    "_entry_prompt_text",
    "_reasoning_records_from_history_slice",
    "_usage_from_dspy_history",
    "_estimate_cost_usd",
    "_PRICE_TABLE_PER_M",
]


def _all_known_lms(app: "FastAPI") -> list[Any]:
    """Return every LM instance the running agent might call —
    ``dspy.settings.lm`` plus the agent's ``_planner_lm`` and any
    expert-bound LMs. Lets the turn handler diff history across
    all of them so planner + expert + chat token counts roll up."""

    lms: list[Any] = []
    try:
        from clio_agent.gact.runtime.ambient_lm import resolve_active_lm  # noqa: PLC0415

        # The global dspy LM: the bound profile inside a ``dspy.context``, else the
        # process boot default — recorded as a structured ``ambient_lm_default``
        # reason (queryable), never a silent ambient read. The agent's explicit LMs
        # gathered below cover the accounting even when this is the boot default, so
        # removing the global default cannot silently under-count the turn (#818).
        main = resolve_active_lm(site="usage._all_known_lms", app=app)
        if main is not None:
            lms.append(main)
    except Exception as exc:  # pragma: no cover  # noqa: BLE001 - roll up what we can
        logger.warning(
            "global dspy LM not reachable; usage rollup may under-count this turn "
            "reason=usage_lm_discovery_failed error=%s",
            exc,
        )
    agent = getattr(getattr(app, "state", None), "agent", None)
    # Include _main_lm: the agent's primary LM (planner + experts route through it
    # when it is not the global dspy.settings.lm). Missing it under-counts usage
    # AND drops the reasoning trace for the bulk of the turn. Keep the others.
    for attr in ("_main_lm", "_planner_lm", "_router_lm", "router_lm", "_expert_lm", "main_lm"):
        side = getattr(agent, attr, None) if agent is not None else None
        if side is not None and side not in lms:
            lms.append(side)
    return lms


def _snapshot_lm_history_index(app: Optional["FastAPI"] = None) -> dict[int, int]:
    """Return current ``len(lm.history)`` for every known LM,
    keyed by ``id(lm)`` so the diff side can find them again
    even if the agent rebinds attributes mid-turn."""

    if app is None:
        from clio_agent.gact.runtime.ambient_lm import resolve_active_lm  # noqa: PLC0415

        lm = resolve_active_lm(site="usage._snapshot_lm_history_index")
        return {id(lm): len(getattr(lm, "history", None) or [])} if lm else {}
    snapshot: dict[int, int] = {}
    for lm in _all_known_lms(app):
        history = getattr(lm, "history", None) or []
        snapshot[id(lm)] = len(history)
    return snapshot


def _usage_from_history_slice(start: Any, app: Optional["FastAPI"] = None) -> dict[str, Any]:
    """Sum usage from each known LM's ``history[start:]`` — every
    call this turn made across planner + experts + chat. Accepts
    either a ``dict[id(lm) -> int]`` snapshot (preferred) or a
    legacy single int for backwards compat with single-LM callers.
    """

    if app is not None:
        lms = _all_known_lms(app)
    else:
        from clio_agent.gact.runtime.ambient_lm import resolve_active_lm  # noqa: PLC0415

        lm = resolve_active_lm(site="usage._usage_from_history_slice")
        lms = [lm] if lm else []
    if not lms:
        return {}
    if isinstance(start, int):
        # Legacy single-int callers — apply to main LM only.
        snap = {id(lms[0]): start}
    else:
        snap = start
    input_tok = output_tok = cache_read = cache_write = 0
    raw_cost = 0.0
    last_model = ""
    for lm in lms:
        start_idx = snap.get(id(lm), 0)
        history = getattr(lm, "history", None) or []
        for entry in history[start_idx:]:
            if not isinstance(entry, dict):
                continue
            usage = entry.get("usage") or {}
            if not isinstance(usage, dict):
                continue
            input_tok += int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            output_tok += int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
            cache_read += int(usage.get("cache_read_input_tokens") or 0)
            cache_write += int(usage.get("cache_creation_input_tokens") or 0)
            raw_cost += float(usage.get("cost_usd") or usage.get("total_cost") or 0.0)
            last_model = entry.get("model") or last_model
    if raw_cost == 0.0:
        raw_cost = _estimate_cost_usd(last_model, input_tok, output_tok)
    return {
        "input": input_tok,
        "output": output_tok,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "cost_usd": raw_cost,
    }


def _entry_response_text(entry: dict[str, Any]) -> str:
    """Pull the answer text out of one dspy ``lm.history`` entry's outputs."""

    outputs = entry.get("outputs")
    texts: list[str] = []
    if isinstance(outputs, list):
        for out in outputs:
            if isinstance(out, str):
                texts.append(out)
            elif isinstance(out, dict) and out.get("text"):
                texts.append(str(out["text"]))
    return "\n".join(t for t in texts if t).strip()


def _entry_prompt_text(entry: dict[str, Any]) -> str:
    """Best-effort: the rendered user/question prompt for one history entry."""

    prompt = entry.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        return prompt.strip()
    messages = entry.get("messages")
    if isinstance(messages, list):
        # The last user message is the closest thing to "the question" asked.
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                return str(msg.get("content") or "").strip()
    return ""


def _reasoning_records_from_history_slice(
    start: Any, app: Optional["FastAPI"] = None
) -> list[dict[str, Any]]:
    """Collect ``(question, reasoning, response)`` per LM call in the turn's
    history slice -- across planner + every expert + chat -- so the reasoning
    tokens are LOGGED, not discarded. Only entries that actually carried
    reasoning are included (non-reasoning models yield an empty list)."""

    if app is not None:
        lms = _all_known_lms(app)
    else:
        from clio_agent.gact.runtime.ambient_lm import resolve_active_lm  # noqa: PLC0415

        lm = resolve_active_lm(site="usage._reasoning_records_from_history_slice")
        lms = [lm] if lm else []
    lms = [lm for lm in lms if lm is not None]
    if not lms:
        return []
    snap = {id(lms[0]): start} if isinstance(start, int) else (start or {})
    records: list[dict[str, Any]] = []
    for lm in lms:
        start_idx = snap.get(id(lm), 0)
        history = getattr(lm, "history", None) or []
        for entry in history[start_idx:]:
            if not isinstance(entry, dict):
                continue
            reasoning = _entry_reasoning_text(entry)
            if not reasoning:
                continue
            records.append(
                {
                    "model": entry.get("model") or "",
                    "question": _entry_prompt_text(entry),
                    "reasoning": reasoning,
                    "response": _entry_response_text(entry),
                    "reasoning_chars": len(reasoning),
                    "timestamp": entry.get("timestamp") or "",
                }
            )
    return records


def _usage_from_dspy_history() -> dict[str, Any]:
    """Reach into DSPy's currently-configured LM and pull the most
    recent call's usage block. Returns ``{}`` whenever DSPy isn't
    importable, no LM is configured, or the history is empty —
    callers default to zeros.

    Best-effort. DSPy's history shape changes between minor versions;
    we accept any dict-shaped record under ``lm.history[-1]`` whose
    ``usage`` (or ``response.usage``) carries the OpenAI-style keys
    we already use on the wire.
    """

    from clio_agent.gact.runtime.ambient_lm import resolve_active_lm  # noqa: PLC0415

    lm = resolve_active_lm(site="usage._usage_from_dspy_history")
    if lm is None:
        return {}
    history = getattr(lm, "history", None)
    if not history:
        return {}
    last = history[-1]
    usage = last.get("usage") if isinstance(last, dict) else getattr(last, "usage", None)
    if usage is None and isinstance(last, dict):
        resp = last.get("response", {}) or {}
        usage = resp.get("usage", {}) if isinstance(resp, dict) else None
    if not isinstance(usage, dict):
        return {}
    input_tok = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    output_tok = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    raw_cost = float(usage.get("cost_usd") or usage.get("total_cost") or 0.0)
    # iowarp/clio-agent#8: some OpenAI-compatible proxies don't pass
    # cost_usd through, so the upstream usage dict reports zero. Fall
    # back to a per-token price table keyed by the LM's model id when
    # raw_cost == 0.
    if raw_cost == 0.0:
        model = ""
        if isinstance(last, dict):
            model = last.get("model") or last.get("response", {}).get("model", "") or ""
        else:
            model = getattr(last, "model", "") or ""
        raw_cost = _estimate_cost_usd(model, input_tok, output_tok)
    return {
        "input": input_tok,
        "output": output_tok,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "cost_usd": raw_cost,
    }


# iowarp/clio-agent#8: per-million-token prices (USD) for models we
# expect to see through our presets. Best-effort — the LM provider
# is the source of truth when it actually reports cost; this kicks
# in only when the upstream usage dict has zero. Keys match the
# substrings we look for in the reported model id (case-insensitive).
_PRICE_TABLE_PER_M: dict[str, tuple[float, float]] = {
    # (input $/M tokens, output $/M tokens) as of model-card pricing.
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-4": (15.0, 75.0),
    "claude-opus-4-6": (15.0, 75.0),
    "claude-3-5-haiku": (0.8, 4.0),
    "claude-3-5-sonnet": (3.0, 15.0),
    "claude-3-opus": (15.0, 75.0),
    # OpenRouter free tier — by definition $0.
    ":free": (0.0, 0.0),
    # OpenAI defaults if someone wires direct.
    "gpt-4o-mini": (0.15, 0.6),
    "gpt-4o": (2.5, 10.0),
}


def _estimate_cost_usd(model_id: str, input_tokens: int, output_tokens: int) -> float:
    """Best-effort cost estimate when the LM doesn't report one.

    Substring-matches the model id against ``_PRICE_TABLE_PER_M``;
    returns 0.0 when nothing matches (no false-precision number).
    """

    if not model_id:
        return 0.0
    needle = model_id.lower()
    match: Optional[tuple[float, float]] = None
    for key, prices in _PRICE_TABLE_PER_M.items():
        if key in needle:
            match = prices
            break
    if match is None:
        return 0.0
    input_price, output_price = match
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000
