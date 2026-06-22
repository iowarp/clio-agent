"""Acceptance: per-expert 90%-style auto-compaction (GOAL.md "Definition of done" #3).

Fires off the provider's exact ``prompt_tokens / context_window`` when the ratio
crosses a configurable threshold, calling ``summarize(all)`` so the next prompt is
the compacted view. dspy's reactive ``truncate_trajectory`` stays the never-fired
backstop. ``_last_prompt_tokens`` and ``_summarize_segments_llm`` are patched so the
trigger is deterministic and no real LM call is made.
"""

from __future__ import annotations

import dspy

import clio_agent.gact.app as app

from .conftest import live_plane_context, make_react_agent

SID, SCOPE = "s1", "agentA"


def _populate(arc, scope=SCOPE):
    arc.append_segment(SID, scope, "thought", {"text": "T0"}, step=0)
    arc.append_segment(SID, scope, "tool_call", {"name": "a", "args": {}}, step=0)
    arc.append_segment(SID, scope, "observation", {"text": "O0"}, step=0)


def _patch(monkeypatch, *, prompt_tokens, summary="COMPACT_SUMMARY"):
    monkeypatch.setattr(app, "_last_prompt_tokens", lambda: prompt_tokens)
    monkeypatch.setattr(app, "_summarize_segments_llm", lambda segs: summary)


def test_fires_over_threshold(arc, monkeypatch):
    _patch(monkeypatch, prompt_tokens=900)  # 900/1000 = 0.90 >= 0.85 default
    _populate(arc)
    agent = make_react_agent()
    with live_plane_context(arc, session=SID, scope=SCOPE, window=1000):
        agent._maybe_autocompact()
    # collapsed to a single summary observation
    assert arc.render_segments_keys(SID, SCOPE) == {"observation_0": "COMPACT_SUMMARY"}


def test_does_not_fire_under_threshold(arc, monkeypatch):
    _patch(monkeypatch, prompt_tokens=500)  # 0.50 < 0.85
    _populate(arc)
    agent = make_react_agent()
    before = arc.render_segments_keys(SID, SCOPE)
    with live_plane_context(arc, session=SID, scope=SCOPE, window=1000):
        agent._maybe_autocompact()
    assert arc.render_segments_keys(SID, SCOPE) == before  # untouched


def test_threshold_is_env_configurable(arc, monkeypatch):
    monkeypatch.setenv("CLIO_AUTOCOMPACT_PCT", "0.50")
    _patch(monkeypatch, prompt_tokens=600)  # 0.60 >= 0.50 (would NOT fire at 0.85)
    _populate(arc)
    agent = make_react_agent()
    with live_plane_context(arc, session=SID, scope=SCOPE, window=1000):
        agent._maybe_autocompact()
    assert arc.render_segments_keys(SID, SCOPE) == {"observation_0": "COMPACT_SUMMARY"}


def test_disabled_when_window_unknown(arc, monkeypatch):
    _patch(monkeypatch, prompt_tokens=9999)  # huge, but window=0 => no denominator
    _populate(arc)
    agent = make_react_agent()
    before = arc.render_segments_keys(SID, SCOPE)
    with live_plane_context(arc, session=SID, scope=SCOPE, window=0):
        agent._maybe_autocompact()
    assert arc.render_segments_keys(SID, SCOPE) == before  # auto-compaction off


def test_skips_when_summary_llm_fails(arc, monkeypatch):
    _patch(monkeypatch, prompt_tokens=900, summary="")  # summarizer returns '' => skip
    _populate(arc)
    agent = make_react_agent()
    before = arc.render_segments_keys(SID, SCOPE)
    with live_plane_context(arc, session=SID, scope=SCOPE, window=1000):
        agent._maybe_autocompact()
    assert arc.render_segments_keys(SID, SCOPE) == before  # context preserved on failure


def test_last_prompt_tokens_falls_back_to_token_counter(monkeypatch):
    """Regression guard: when the provider reports no/zero prompt_tokens (the ALCF
    vLLM endpoint returns prompt_tokens:0), _last_prompt_tokens must fall back to a
    client-side token_counter over the last call's real messages — else
    auto-compaction never fires in production. Uses tiktoken offline (no network)."""
    fake_lm = type(
        "FakeLM",
        (),
        {
            "model": "gpt-3.5-turbo",  # tiktoken-native => offline count
            "history": [{"messages": [{"role": "user", "content": "the quick brown fox " * 30}]}],
        },
    )()
    with dspy.context(lm=fake_lm):
        # no usage_tracker installed -> must use the history/token_counter fallback
        n = app._last_prompt_tokens()
    assert n > 0, "fallback failed: a non-empty prompt must count > 0 tokens"


def test_last_prompt_tokens_fallback_when_tracker_reports_zero(monkeypatch):
    """Even with a usage tracker present, a 0 prompt_tokens must fall back."""
    fake_lm = type(
        "FakeLM",
        (),
        {
            "model": "gpt-3.5-turbo",
            "history": [{"messages": [{"role": "user", "content": "hello world " * 25}]}],
        },
    )()
    fake_tracker = type(
        "FakeTracker", (), {"usage_data": {"gpt-3.5-turbo": [{"prompt_tokens": 0}]}}
    )()
    with dspy.context(lm=fake_lm):
        monkeypatch.setattr(dspy.settings, "usage_tracker", fake_tracker, raising=False)
        n = app._last_prompt_tokens()
    assert n > 0


def test_per_expert_independent(arc, monkeypatch):
    """Each expert checks its OWN window; one over-threshold scope compacts, an
    under-threshold sibling does not."""
    _patch(monkeypatch, prompt_tokens=900)
    _populate(arc, scope="agentA/hot")
    _populate(arc, scope="agentA/cold")
    agent = make_react_agent()
    # hot: window 1000 -> 0.90 fires
    with live_plane_context(arc, session=SID, scope="agentA/hot", window=1000):
        agent._maybe_autocompact()
    # cold: window 100000 -> 0.009 does not fire
    with live_plane_context(arc, session=SID, scope="agentA/cold", window=100000):
        agent._maybe_autocompact()
    assert arc.render_segments_keys(SID, "agentA/hot") == {"observation_0": "COMPACT_SUMMARY"}
    assert "O0" in str(arc.render_segments_keys(SID, "agentA/cold"))  # untouched
