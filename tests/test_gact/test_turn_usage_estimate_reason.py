"""Slice 3 (#772): the usage rollup records *why* an estimate ran.

``roll_up_usage`` degrades to a character-based token estimate and/or a
price-table cost estimate when the LM demonstrably fired this turn but the
upstream proxy reported zero usage. That degradation used to be silent; these
tests pin the structured ``reason=usage_estimated`` warning (with the concrete
``strategy=``) on the estimate path, and pin that the no-calls case stays
silent AND never synthesizes tokens.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import turn_usage


def _fake_state(**overrides: Any) -> SimpleNamespace:
    """Minimal ``TurnState`` stand-in carrying just what ``roll_up_usage`` reads."""

    base: dict[str, Any] = {
        "app": SimpleNamespace(),
        "sid": "sess_test",
        "history_start": {0: 0},
        "turn_tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
        "answer_text": "",
        "enriched_text": "",
        "turn_cost": 0.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def zero_usage_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the estimate branch: history grew but every usage source is zero."""

    monkeypatch.setattr(turn_usage, "_snapshot_lm_history_index", lambda _app: {0: 3})
    monkeypatch.setattr(
        turn_usage, "_usage_from_history_slice", lambda _start, _app: {}
    )
    monkeypatch.setattr(turn_usage, "_usage_from_dspy_history", lambda: {})
    monkeypatch.setattr(turn_usage, "_current_lm_model_id", lambda: "test/model")
    monkeypatch.setattr(
        turn_usage, "_estimate_cost_usd", lambda _m, _i, _o: 0.0042
    )


def test_estimate_path_records_usage_estimated_reason(
    zero_usage_proxy: None, caplog: pytest.LogCaptureFixture
) -> None:
    """chars/4 + price-table estimates emit one structured warning per turn."""

    state = _fake_state(
        answer_text="x" * 40,
        enriched_text="y" * 80,
    )
    with caplog.at_level("WARNING", logger="clio_agent.gact.turn_usage"):
        turn_usage.roll_up_usage(state, SimpleNamespace())

    records = [
        r for r in caplog.records if "reason=usage_estimated" in r.getMessage()
    ]
    assert len(records) == 1, "exactly one structured warning per turn"
    msg = records[0].getMessage()
    assert "strategy=" in msg
    assert "output_chars_div_4" in msg
    assert "input_chars_div_4" in msg
    assert "price_table_estimate" in msg
    assert "session=sess_test" in msg
    # The estimates still landed on the state.
    assert state.turn_tokens["output"] == 10
    assert state.turn_tokens["input"] == 20
    assert state.turn_cost == pytest.approx(0.0042)


def test_no_calls_case_is_silent_and_synthesizes_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No real LM call this turn -> no warning, no synthesized tokens/cost."""

    # History did NOT grow (history_made_calls is False), so the estimate
    # branch must not run even though answer/enriched text is present.
    monkeypatch.setattr(turn_usage, "_snapshot_lm_history_index", lambda _app: {0: 0})
    monkeypatch.setattr(
        turn_usage, "_usage_from_history_slice", lambda _start, _app: {}
    )
    monkeypatch.setattr(turn_usage, "_usage_from_dspy_history", lambda: {})

    def _boom(*_a: Any, **_k: Any) -> float:  # pragma: no cover - must not run
        raise AssertionError("price-table estimate must not run with no calls")

    monkeypatch.setattr(turn_usage, "_estimate_cost_usd", _boom)

    state = _fake_state(answer_text="x" * 40, enriched_text="y" * 80)
    with caplog.at_level("WARNING", logger="clio_agent.gact.turn_usage"):
        turn_usage.roll_up_usage(state, SimpleNamespace())

    assert not [
        r for r in caplog.records if "reason=usage_estimated" in r.getMessage()
    ]
    # pins turn_usage.py:76-81 — no synthesized tokens when the LM never fired.
    assert state.turn_tokens["output"] == 0
    assert state.turn_tokens["input"] == 0
    assert state.turn_cost == 0.0
