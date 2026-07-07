"""Per-turn usage/token/cost rollup for the GACT turn engine (#767 Phase B).

Slice 1 of the ``turn.py`` decomposition: the cost + token rollup that used to
live inline in ``_run_turn_in_background`` moves here as a free function taking
:class:`~clio_agent.gact.turn_state.TurnState` first (the gact seam convention).

The rollup is behavior-preserving — it mutates ``state.turn_tokens`` in place and
reassigns the ``state.turn_cost`` scalar, exactly as the former linear body did
(TRICKY #3 in the Phase B spec). Real DSPy predictions do not always populate
``.tokens`` / ``.cost_usd`` directly, so the usage is pulled from the per-turn
``UsageTracker`` history slice first (works across threads + streaming), then the
DSPy LM history, then a character-based estimate — but only when the LM actually
fired this turn.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from clio_agent.gact.providers.config import _current_lm_model_id
from clio_agent.gact.usage import (
    _estimate_cost_usd,
    _snapshot_lm_history_index,
    _usage_from_dspy_history,
    _usage_from_history_slice,
)

if TYPE_CHECKING:
    from clio_agent.gact.turn_state import TurnState

logger = logging.getLogger(__name__)


def roll_up_usage(state: "TurnState", pred: Any) -> None:
    """Roll the turn's token counts + cost into ``state`` (#767 Phase B).

    Mutates ``state.turn_tokens`` in place and writes ``state.turn_cost``. Pulls
    usage from the prediction's ``.tokens`` when present; otherwise diffs the LM
    history slice for this turn (planner + expert + chat calls), falling back to
    the DSPy history and finally a character-based estimate when the upstream
    proxy reports zero usage but the LM demonstrably fired.

    Args:
        state: The active turn's mutable working set.
        pred: The forward prediction to harvest usage from.
    """

    # CLIO-BBBBBBBBBB24: cost + token rollup. Real DSPy
    # predictions don't always populate .tokens / .cost_usd
    # directly — pull from the per-turn UsageTracker first
    # (works across threads + streaming), then LM history.
    raw_tokens = getattr(pred, "tokens", None)
    if raw_tokens is not None:
        for key in state.turn_tokens:
            if isinstance(raw_tokens, dict):
                v = raw_tokens.get(key, 0)
            else:
                v = getattr(raw_tokens, key, 0)
            state.turn_tokens[key] = int(v or 0)
    else:
        # Diff the LM history slice for this turn first — captures
        # planner + expert + chat calls cleanly. Falls back to
        # ``last entry only`` for older code paths, then to a
        # character-based estimate when the upstream proxy
        # reports zero (some OpenAI-compatible proxies don't
        # populate usage on chunked replies).
        history_end = _snapshot_lm_history_index(state.app)
        history_made_calls = any(
            history_end.get(k, 0) > state.history_start.get(k, 0)
            for k in {*state.history_start.keys(), *history_end.keys()}
        )
        usage = _usage_from_history_slice(state.history_start, state.app)
        if not usage.get("output"):
            usage = _usage_from_dspy_history()
        for key in state.turn_tokens:
            state.turn_tokens[key] = int(usage.get(key, 0) or 0)
        state.turn_cost = float(usage.get("cost_usd", 0.0) or 0.0)
        # Char-based fallback only when the LM actually fired
        # this turn (history grew) but the upstream proxy
        # reported zero usage. Don't synthesize numbers when
        # there was no real call (e.g. unit tests with a fake
        # agent that bypasses dspy.LM entirely).
        if history_made_calls:
            # Record WHICH estimate strategies fired this turn so the degraded
            # rollup is queryable instead of silent (#772). One structured
            # warning per turn, not per strategy.
            estimate_strategies: list[str] = []
            if state.turn_tokens["output"] == 0 and state.answer_text:
                state.turn_tokens["output"] = max(1, len(state.answer_text) // 4)
                estimate_strategies.append("output_chars_div_4")
            if state.turn_tokens["input"] == 0 and state.enriched_text:
                state.turn_tokens["input"] = max(1, len(state.enriched_text) // 4)
                estimate_strategies.append("input_chars_div_4")
            if state.turn_cost == 0.0:
                state.turn_cost = _estimate_cost_usd(
                    _current_lm_model_id(),
                    state.turn_tokens["input"],
                    state.turn_tokens["output"],
                )
                estimate_strategies.append("price_table_estimate")
            if estimate_strategies:
                logger.warning(
                    "usage rollup degraded to an estimate because the LM fired "
                    "but reported zero usage "
                    "reason=usage_estimated strategy=%s session=%s",
                    ",".join(estimate_strategies),
                    state.sid,
                )
    if not state.turn_cost:
        state.turn_cost = float(getattr(pred, "cost_usd", 0.0) or 0.0)
