"""Usage / cost metering for the GACT server (#714).

This module rolls a turn's token usage, cost, reasoning and prompt size up out of the
calls the turn made, plus the best-effort per-token price table used when an upstream
provider doesn't report a cost.

The calls are the DSPy history entries in the turn's ledger
(:mod:`clio_agent.runtime.turn_lm_ledger`): every clio LM records there and nowhere
else, and the ledger ends with the turn, so the agent process keeps no history of its
own (the durable record of each call is the ``lm.call`` event). The turn notes how
many calls its ledger held at the start (:func:`_turn_call_count`) and reads the calls
after it (:func:`_usage_from_turn_calls`, :func:`_reasoning_records_from_turn_calls`,
:func:`_last_prompt_usage_from_turn_calls`) -- planner, every expert and chat alike.

The reasoning/answer split of a history entry is the single-owner rule in
:mod:`clio_agent.gact.reasoning_extract` (the same one the live stream applies, so a
reloaded reasoning log equals the live transcript). It never imports
:mod:`clio_agent.gact.app`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, Optional

# Single source of truth for the reasoning-channel rule, shared with the live stream.
from clio_agent.gact.reasoning_extract import entry_reasoning_text as _entry_reasoning_text
from clio_agent.gact.reasoning_extract import entry_response_text as _entry_response_text
from clio_agent.runtime import trace, turn_lm_ledger

if TYPE_CHECKING:
    from clio_agent.gact.turn_state import TurnState

logger = logging.getLogger(__name__)

__all__ = [
    "_turn_call_count",
    "_usage_from_turn_calls",
    "_last_prompt_usage_from_turn_calls",
    "_estimated_prompt_usage",
    "_entry_reasoning_text",
    "_entry_response_text",
    "_entry_prompt_text",
    "_reasoning_records_from_turn_calls",
    "_estimate_cost_usd",
    "_price_table_match",
    "_capture_reasoning_enabled",
    "capture_reasoning_log",
    "reported_cost_total",
]


def _usage_cache_tokens(usage: dict[str, Any]) -> tuple[int, int, bool]:
    """Return provider-reported prompt-cache tokens and whether they were measured."""

    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    details = details if isinstance(details, dict) else {}
    read_keys = ("cache_read_input_tokens", "cached_tokens")
    write_keys = ("cache_creation_input_tokens", "cache_write_input_tokens")
    cache_read = int(
        usage.get("cache_read_input_tokens")
        or details.get("cached_tokens")
        or details.get("cache_read_input_tokens")
        or 0
    )
    cache_write = int(
        usage.get("cache_creation_input_tokens")
        or usage.get("cache_write_input_tokens")
        or details.get("cache_creation_input_tokens")
        or details.get("cache_write_input_tokens")
        or 0
    )
    measured = any(key in usage for key in (*read_keys, *write_keys)) or any(
        key in details for key in (*read_keys, *write_keys)
    )
    return cache_read, cache_write, measured


def _turn_call_count() -> int:
    """How many calls the current turn's ledger holds (0 outside a turn)."""

    return len(turn_lm_ledger.ledger_calls())


def _turn_calls(start: int) -> list[dict[str, Any]]:
    """The current turn's calls after the first ``start``."""

    return turn_lm_ledger.ledger_calls()[start:]


def _usage_from_turn_calls(start: int) -> dict[str, Any]:
    """Sum usage over the calls the turn made after its first ``start`` calls."""

    input_tok = output_tok = cache_read = cache_write = 0
    raw_cost = 0.0
    cost_reported = False
    last_model = ""
    for entry in _turn_calls(start):
        usage = entry.get("usage") or {}
        if not isinstance(usage, dict):
            continue
        input_tok += int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        output_tok += int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        entry_cache_read, entry_cache_write, _ = _usage_cache_tokens(usage)
        cache_read += entry_cache_read
        cache_write += entry_cache_write
        # dspy.clients.base_lm records a REAL provider/subscription cost as
        # the history entry's own top-level "cost" (from the LM response's
        # ``_hidden_params["response_cost"]``) -- a SIBLING of "usage", not
        # nested inside it. That's the primary, authoritative source; the
        # nested usage["cost_usd"]/["total_cost"] fallback covers upstream
        # OpenAI-compatible proxies that stick their own cost field onto
        # the usage block instead (iowarp/clio-agent#8).
        entry_cost_raw = entry.get("cost")
        if entry_cost_raw is None:
            entry_cost_raw = usage.get("cost_usd") or usage.get("total_cost")
        if entry_cost_raw is None:
            # An engine LM (Claude Code) reports its provider cost on the typed
            # response; DSPy has no pricing provider for custom engines.
            provider_data = getattr(entry.get("response"), "provider_data", None) or {}
            entry_cost_raw = provider_data.get("cost_usd")
        entry_cost = float(entry_cost_raw or 0.0)
        if entry_cost_raw is not None:
            cost_reported = True
        raw_cost += entry_cost
        last_model = entry.get("model") or last_model
    cost_known = cost_reported
    if raw_cost == 0.0 and not cost_reported:
        cost_known = _price_table_match(last_model) is not None
        raw_cost = _estimate_cost_usd(last_model, input_tok, output_tok)
    return {
        "input": input_tok,
        "output": output_tok,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "cost_usd": raw_cost,
        # True when ``cost_usd`` came from a provider report or a price-table
        # match (even a legitimately-free $0 match) -- False means no source
        # for this turn's cost exists, so it is unknown, not zero (#775 no
        # silent fallback: callers must not present this as a real number).
        "cost_known": cost_known,
    }


def _last_prompt_usage_from_turn_calls(start: int) -> dict[str, Any]:
    """Return the newest prompt reading among the calls the turn made after ``start``.

    The reading is provider-measured when the call's entry carries prompt usage.
    When a provider omits that number, the actual rendered messages are counted with
    the same model-aware fallback used by automatic compaction. Cache utilization is
    exposed only when the provider emitted cache detail fields.
    """

    candidates = [
        (str(entry.get("timestamp") or ""), ordinal, entry)
        for ordinal, entry in enumerate(_turn_calls(start))
    ]
    if not candidates:
        return {}
    _, _, entry = max(candidates, key=lambda row: (row[0], row[1]))
    usage = entry.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    model = str(entry.get("model") or "")
    used_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    cache_read, cache_write, cache_measured = _usage_cache_tokens(usage)
    source = "provider" if used_tokens > 0 else ""
    if used_tokens <= 0:
        messages = entry.get("messages")
        if messages:
            from clio_agent.gact.runtime.context_tokens import (  # noqa: PLC0415
                _heuristic_message_tokens,
                _model_uses_tiktoken,
            )

            if model and _model_uses_tiktoken(model):
                try:
                    import litellm  # noqa: PLC0415

                    used_tokens = int(litellm.token_counter(model=model, messages=messages))
                except Exception:  # noqa: BLE001 - explicit heuristic fallback below
                    used_tokens = _heuristic_message_tokens(messages)
            else:
                used_tokens = _heuristic_message_tokens(messages)
            source = "estimated" if used_tokens > 0 else ""
    if used_tokens <= 0:
        return {}
    from clio_agent.gact.prompt_breakdown import request_breakdown  # noqa: PLC0415

    breakdown = request_breakdown(entry)
    return {
        "used_tokens": used_tokens,
        "source": source,
        "model": model,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "cache_tokens_measured": cache_measured,
        **({"prompt_breakdown": breakdown.model_dump()} if breakdown is not None else {}),
    }


def _estimated_prompt_usage(prompt: str, model: str) -> dict[str, Any]:
    """Measure a materialized prompt when the provider emitted no usage record.

    Subscription CLI providers can complete a real turn without adding a DSPy LM
    history entry.  The exact prompt passed into the agent is still available at
    the turn boundary, so retain a clearly labelled estimate instead of reporting
    context usage as unavailable.  Cache fields remain unmeasured.
    """

    if not prompt:
        return {}
    from clio_agent.gact.runtime.context_tokens import (  # noqa: PLC0415
        _heuristic_text_tokens,
        _model_uses_tiktoken,
    )

    used_tokens = 0
    if model and _model_uses_tiktoken(model):
        try:
            import litellm  # noqa: PLC0415

            used_tokens = int(litellm.token_counter(model=model, text=prompt))
        except Exception:  # noqa: BLE001 - explicit heuristic fallback below
            used_tokens = 0
    if used_tokens <= 0:
        used_tokens = _heuristic_text_tokens(prompt)
    return {
        "used_tokens": used_tokens,
        "source": "estimated",
        "model": model,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cache_tokens_measured": False,
    }


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


def _reasoning_records_from_turn_calls(start: int) -> list[dict[str, Any]]:
    """Collect ``(question, reasoning, response)`` per call the turn made after
    ``start`` -- across planner + every expert + chat -- so the reasoning tokens are
    LOGGED, not discarded. Only calls that actually carried reasoning are included
    (non-reasoning models yield an empty list)."""

    records: list[dict[str, Any]] = []
    for entry in _turn_calls(start):
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


def _capture_reasoning_enabled() -> bool:
    """Whether finalize persists per-call reasoning onto assistant metadata.

    ``runtime.capture_reasoning`` / ``CLIO_CAPTURE_REASONING`` (default on);
    set to 0 to avoid the metadata growth.
    """
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    return conf.resolve(
        "runtime.capture_reasoning", env="CLIO_CAPTURE_REASONING", default=True, cast=conf.as_bool
    )


def capture_reasoning_log(state: "TurnState") -> None:
    """Persist per-call reasoning traces onto the assistant message metadata.

    Args:
        state: The in-flight turn state; its ``assistant_metadata`` is mutated
            in place (``reasoning_log`` key) when any LM call this turn carried
            reasoning tokens. A best-effort capture — never fails the turn.
    """

    # Reasoning capture: log the chain-of-thought tokens for EVERY LM call this
    # turn (planner + each expert + chat), read from the turn's calls. Most
    # stacks discard reasoning_content; we persist (question, reasoning, response)
    # on the assistant message metadata because the reasoning has scientific
    # value for analysing how the model reached its answer. Gated by
    # CLIO_CAPTURE_REASONING (default on); set to 0 to avoid the metadata growth.
    if _capture_reasoning_enabled():
        try:
            _reasoning_log = _reasoning_records_from_turn_calls(state.calls_start)
        except Exception:  # noqa: BLE001 - reasoning capture is best-effort, never fail a turn
            _reasoning_log = []
        if _reasoning_log:
            state.assistant_metadata["reasoning_log"] = _reasoning_log
            trace.event(
                "REASONING",
                "captured %d call(s): %s",
                len(_reasoning_log),
                "; ".join(
                    f"{(r['model'] or '?').split('/')[-1]}={r['reasoning_chars']}c"
                    for r in _reasoning_log
                ),
            )


def _price_table_match(model_id: str) -> Optional[tuple[float, float]]:
    """List price per million tokens (input, output) for ``model_id``, or ``None``.

    Read from the community model cost map (LiteLLM's, the same source the
    provider catalog's pricing facts cite), never a table compiled into CLIO.
    ``None`` means the model is genuinely unpriced (a local or self-hosted
    model) -- distinct from a listed price of $0 (an OpenRouter ``:free``
    model). Callers use this to tell "no cost data exists" apart from "free".
    """

    if not model_id:
        return None
    from clio_agent.providers.handshake.sources.litellm_catalog import (  # noqa: PLC0415
        lookup_litellm_info,
    )

    found = lookup_litellm_info(model_id, allow_fetch=False)
    if found is None:
        return None
    info = found[1]
    inp, out = info.get("input_cost_per_token"), info.get("output_cost_per_token")
    if isinstance(inp, bool) or isinstance(out, bool):
        return None
    if not (isinstance(inp, int | float) and isinstance(out, int | float)):
        return None
    return inp * 1_000_000, out * 1_000_000


def _estimate_cost_usd(model_id: str, input_tokens: int, output_tokens: int) -> float:
    """Best-effort cost estimate when the LM doesn't report one.

    Prices the tokens at the model's list price (:func:`_price_table_match`);
    returns 0.0 when the model has none (no false-precision number).
    """

    match = _price_table_match(model_id)
    if match is None:
        return 0.0
    input_price, output_price = match
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000


def reported_cost_total(messages: Iterable[Mapping[str, Any]]) -> Optional[float]:
    """Sum the costs the provider actually reported across message rows.

    Returns ``None`` when no message carries a reported cost, so a session rollup
    built from these rows stays "not reported" instead of a fabricated ``0.0``.
    """

    reported = [float(m["cost_usd"]) for m in messages if m.get("cost_usd") is not None]
    return sum(reported) if reported else None
