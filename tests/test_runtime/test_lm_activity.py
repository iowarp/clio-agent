"""Tests for token-streaming liveness (Trace v2 T4).

Two units:
- ``lm_call_in_flight`` is the no-progress watchdog's "this call still counts as
  progress" gate. With token-liveness streaming, ``note_lm_activity`` refreshes it
  per chunk so a slow-but-generating reasoning model is never false-killed, while a
  genuinely frozen (0-token) call stops refreshing and is abandoned at the idle
  window -- not the 1800s ceiling.
- ``IOLoggingLM._clio_streamed_call`` drives a call streamed (drain-and-discard each
  chunk -> ``note_lm_activity``) while ``aforward`` assembles the authoritative
  result. Real LM errors propagate; the result shape is unchanged.
"""

from __future__ import annotations

import pytest

from clio_agent import config as cfg
from clio_agent.runtime import lm_activity


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    # Isolate the module-global tracker between tests and pin the clock.
    lm_activity._STATE.update({"inflight": 0.0, "started": 0.0, "last": 0.0})
    monkeypatch.delenv("CLIO_MAX_LM_CALL_S", raising=False)
    monkeypatch.delenv("CLIO_LM_INTER_TOKEN_IDLE_S", raising=False)
    yield
    lm_activity._STATE.update({"inflight": 0.0, "started": 0.0, "last": 0.0})


def _clock(monkeypatch):
    """Return a setter for a monotonic clock the module reads."""
    box = {"now": 1000.0}
    monkeypatch.setattr(lm_activity.time, "monotonic", lambda: box["now"])
    return box


def test_not_in_flight_when_idle():
    assert lm_activity.lm_call_in_flight() is False


def test_in_flight_within_ceiling_no_tokens(monkeypatch):
    clk = _clock(monkeypatch)
    lm_activity.note_lm_start()
    # No streamed tokens (last == started): trust up to the per-call ceiling.
    clk["now"] += 300.0  # past the 120s idle window, but well under 1800s ceiling
    assert lm_activity.lm_call_in_flight() is True


def test_non_streaming_call_not_killed_at_idle_window(monkeypatch):
    # Regression guard: a non-streaming (or pre-first-token) call must NOT be
    # treated as stalled at the idle window -- only the ceiling applies until a
    # token is actually seen.
    clk = _clock(monkeypatch)
    lm_activity.note_lm_start()
    clk["now"] += 121.0
    assert lm_activity.lm_call_in_flight() is True


def test_ceiling_kills_overlong_call(monkeypatch):
    clk = _clock(monkeypatch)
    lm_activity.note_lm_start()
    clk["now"] += 1801.0
    assert lm_activity.lm_call_in_flight() is False


def test_streaming_token_refreshes_then_idle_kills(monkeypatch):
    clk = _clock(monkeypatch)
    lm_activity.note_lm_start()
    clk["now"] += 10.0
    lm_activity.note_lm_activity()  # first token: idle gate now engaged
    clk["now"] += 119.0  # within the 120s inter-token window
    assert lm_activity.lm_call_in_flight() is True
    clk["now"] += 2.0  # now 121s since last token -> idle exceeded
    assert lm_activity.lm_call_in_flight() is False


def test_streaming_steady_tokens_stay_alive_past_idle(monkeypatch):
    clk = _clock(monkeypatch)
    lm_activity.note_lm_start()
    # A steady token stream past the idle window keeps the call alive.
    for _ in range(20):
        clk["now"] += 100.0
        lm_activity.note_lm_activity()
        assert lm_activity.lm_call_in_flight() is True


def test_idle_window_env_override(monkeypatch):
    monkeypatch.setenv("CLIO_LM_INTER_TOKEN_IDLE_S", "30")
    clk = _clock(monkeypatch)
    lm_activity.note_lm_start()
    clk["now"] += 5.0
    lm_activity.note_lm_activity()
    clk["now"] += 31.0
    assert lm_activity.lm_call_in_flight() is False


def test_note_lm_end_clears_inflight(monkeypatch):
    _clock(monkeypatch)
    lm_activity.note_lm_start()
    assert lm_activity.lm_call_in_flight() is True
    lm_activity.note_lm_end()
    assert lm_activity.lm_call_in_flight() is False


# --- token-liveness gate ---------------------------------------------------


def test_token_liveness_default_on(monkeypatch):
    monkeypatch.delenv("CLIO_LM_TOKEN_LIVENESS", raising=False)
    assert cfg._token_liveness_enabled() is True


def test_token_liveness_kill_switch(monkeypatch):
    monkeypatch.setenv("CLIO_LM_TOKEN_LIVENESS", "0")
    assert cfg._token_liveness_enabled() is False


# --- streamed-call driver --------------------------------------------------


def test_streamed_call_drains_chunks_and_returns_result(monkeypatch):
    import dspy

    lm = cfg._io_logging_lm_cls()(model="openai/dummy")

    activity: list[int] = []
    monkeypatch.setattr(lm_activity, "note_lm_activity", lambda: activity.append(1))

    async def fake_aforward(self, prompt=None, messages=None, **kwargs):  # noqa: ANN001
        send = dspy.settings.send_stream
        for i in range(4):
            await send.send(f"chunk{i}")
        return "ASSEMBLED-RESULT"

    monkeypatch.setattr(type(lm), "aforward", fake_aforward, raising=False)

    out = lm._clio_streamed_call(messages=[{"role": "user", "content": "hi"}])
    assert out == "ASSEMBLED-RESULT"
    assert len(activity) == 4  # one note_lm_activity per streamed chunk


def test_streamed_call_propagates_lm_error(monkeypatch):
    lm = cfg._io_logging_lm_cls()(model="openai/dummy")

    async def boom_aforward(self, prompt=None, messages=None, **kwargs):  # noqa: ANN001
        raise ValueError("provider exploded")

    monkeypatch.setattr(type(lm), "aforward", boom_aforward, raising=False)

    # A real LM error must propagate (the repair loop owns it), NOT be swallowed
    # into a silent fallback that would double-call the provider.
    with pytest.raises(ValueError, match="provider exploded"):
        lm._clio_streamed_call(messages=[{"role": "user", "content": "hi"}])


def test_streamed_call_falls_back_when_plumbing_missing(monkeypatch):
    lm = cfg._io_logging_lm_cls()(model="openai/dummy")

    # Simulate anyio unavailable -> _StreamingPlumbingError so __call__ can fall
    # back to the blocking path.
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "anyio":
            raise ImportError("no anyio")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(cfg._StreamingPlumbingError):
        lm._clio_streamed_call(messages=[{"role": "user", "content": "hi"}])
