from __future__ import annotations

from types import SimpleNamespace

from clio_agent.gact import usage


def test_last_prompt_usage_normalizes_litellm_cache_details(monkeypatch) -> None:
    lm = SimpleNamespace(
        model="openai/gpt-5",
        history=[
            {
                "model": "openai/gpt-5",
                "timestamp": "2026-09-11T10:00:00Z",
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 40,
                    "prompt_tokens_details": {"cached_tokens": 750},
                },
            }
        ],
    )
    monkeypatch.setattr(usage, "_all_known_lms", lambda _app: [lm])

    snapshot = usage._last_prompt_usage_from_history_slice({id(lm): 0}, SimpleNamespace())

    assert snapshot == {
        "used_tokens": 1000,
        "source": "provider",
        "model": "openai/gpt-5",
        "cache_read_tokens": 750,
        "cache_write_tokens": 0,
        "cache_tokens_measured": True,
    }


def test_usage_from_history_slice_reads_dspys_own_top_level_cost_field(monkeypatch) -> None:
    """Root-cause regression (#775): dspy.clients.base_lm._process_lm_response
    records a REAL provider cost as the history entry's own top-level "cost"
    (from the LM response's ``_hidden_params["response_cost"]``) -- a SIBLING
    of "usage", never nested inside it. The old code only ever looked inside
    ``entry["usage"]`` for a "cost_usd"/"total_cost" key, so ANY provider whose
    cost reached DSPy through the standard, documented mechanism (litellm's own
    cost calculation, or a custom provider's ``_hidden_params["response_cost"]``
    -- see providers/claude_code_bridge.py) had its real cost silently dropped
    on every single turn, session-wide, regardless of provider."""
    lm = SimpleNamespace(
        model="claude_code/sonnet-5",
        history=[
            {
                "model": "claude_code/sonnet-5",
                "usage": {"prompt_tokens": 130, "completion_tokens": 45, "total_tokens": 175},
                # The real shape: cost lives HERE, not inside "usage".
                "cost": 0.00412,
            }
        ],
    )
    monkeypatch.setattr(usage, "_all_known_lms", lambda _app: [lm])

    result = usage._usage_from_history_slice({id(lm): 0}, SimpleNamespace())

    assert result["input"] == 130
    assert result["output"] == 45
    assert result["cost_usd"] == 0.00412
    assert result["cost_known"] is True


def test_usage_from_history_slice_reports_unknown_not_zero_with_no_cost_source(
    monkeypatch,
) -> None:
    """A provider that reports usage but truly no cost (no dspy "cost", no
    nested cost_usd/total_cost, and a model the price table has never priced)
    must come back cost_known=False -- honest unknown, not a fabricated $0."""
    lm = SimpleNamespace(
        model="claude_code/sonnet-5",
        history=[
            {
                "model": "claude_code/sonnet-5",
                "usage": {"prompt_tokens": 300, "completion_tokens": 90, "total_tokens": 390},
                "cost": None,
            }
        ],
    )
    monkeypatch.setattr(usage, "_all_known_lms", lambda _app: [lm])

    result = usage._usage_from_history_slice({id(lm): 0}, SimpleNamespace())

    assert result["input"] == 300
    assert result["output"] == 90
    assert result["cost_usd"] == 0.0
    assert result["cost_known"] is False


def test_usage_from_history_slice_treats_a_real_reported_zero_cost_as_known(
    monkeypatch,
) -> None:
    """A provider that explicitly reports $0.00 (e.g. a free tier) is a KNOWN
    cost, not "unknown" -- distinct from the no-cost-source case above even
    though both compute the same numeric raw_cost."""
    lm = SimpleNamespace(
        model="openrouter/some-model:free",
        history=[
            {
                "model": "openrouter/some-model:free",
                "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
                "cost": 0.0,
            }
        ],
    )
    monkeypatch.setattr(usage, "_all_known_lms", lambda _app: [lm])

    result = usage._usage_from_history_slice({id(lm): 0}, SimpleNamespace())

    assert result["cost_usd"] == 0.0
    assert result["cost_known"] is True


def test_estimated_prompt_usage_labels_unmeasured_cache(monkeypatch) -> None:
    monkeypatch.setattr(usage, "_all_known_lms", lambda _app: [])

    snapshot = usage._estimated_prompt_usage("abcdefgh", "anthropic/claude-sonnet")

    assert snapshot == {
        "used_tokens": 2,
        "source": "estimated",
        "model": "anthropic/claude-sonnet",
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cache_tokens_measured": False,
    }
