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

    # Patch acall (the @with_callbacks-wrapped streaming entry the driver calls):
    # stream N chunks via send_stream, return the assembled outputs.
    async def fake_acall(self, prompt=None, messages=None, **kwargs):  # noqa: ANN001
        send = dspy.settings.send_stream
        for i in range(4):
            await send.send(f"chunk{i}")
        return ["ASSEMBLED-RESULT"]

    monkeypatch.setattr(type(lm), "acall", fake_acall, raising=False)

    out = lm._clio_streamed_call(messages=[{"role": "user", "content": "hi"}])
    assert out == ["ASSEMBLED-RESULT"]
    assert len(activity) == 4  # one note_lm_activity per streamed chunk


def test_streamed_call_propagates_lm_error(monkeypatch):
    lm = cfg._io_logging_lm_cls()(model="openai/dummy")

    async def boom_acall(self, prompt=None, messages=None, **kwargs):  # noqa: ANN001
        raise ValueError("provider exploded")

    monkeypatch.setattr(type(lm), "acall", boom_acall, raising=False)

    # A real LM error must propagate (the repair loop owns it), NOT be swallowed
    # into a silent fallback that would double-call the provider.
    with pytest.raises(ValueError, match="provider exploded"):
        lm._clio_streamed_call(messages=[{"role": "user", "content": "hi"}])


class _FakeMidStreamFallbackError(Exception):
    pass


class _FakeAdapterParseError(Exception):
    pass


def test_is_transient_provider_error_classifies():
    # Transient infra failures -> retry.
    assert cfg._is_transient_provider_error(_FakeMidStreamFallbackError("boom"))
    assert cfg._is_transient_provider_error(
        RuntimeError("OpenAIException - The model has crashed without additional info")
    )
    assert cfg._is_transient_provider_error(ConnectionError("Connection error"))
    # NOT transient -> the repair loop owns these, never retried as transient.
    assert not cfg._is_transient_provider_error(_FakeAdapterParseError("bad fields"))
    assert not cfg._is_transient_provider_error(ValueError("provider exploded"))


def test_call_retries_transient_then_succeeds(monkeypatch):
    lm = cfg._io_logging_lm_cls()(model="openai/dummy")
    monkeypatch.setattr(cfg, "_lm_transient_retries", lambda: 2)
    monkeypatch.setattr(cfg, "_lm_transient_backoff_s", lambda: 0.0)

    calls = {"n": 0}

    def flaky_once(self, prompt=None, messages=None, **kwargs):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            raise _FakeMidStreamFallbackError("the model has crashed")
        return ["RECOVERED"]

    monkeypatch.setattr(type(lm), "_clio_invoke_once", flaky_once, raising=False)
    out = lm(messages=[{"role": "user", "content": "hi"}])
    assert out == ["RECOVERED"]
    assert calls["n"] == 2  # crashed once, retried once, succeeded


def test_call_does_not_retry_non_transient(monkeypatch):
    lm = cfg._io_logging_lm_cls()(model="openai/dummy")
    monkeypatch.setattr(cfg, "_lm_transient_retries", lambda: 3)
    monkeypatch.setattr(cfg, "_lm_transient_backoff_s", lambda: 0.0)

    calls = {"n": 0}

    def always_parse_error(self, prompt=None, messages=None, **kwargs):  # noqa: ANN001
        calls["n"] += 1
        raise _FakeAdapterParseError("missing fields")

    monkeypatch.setattr(type(lm), "_clio_invoke_once", always_parse_error, raising=False)
    with pytest.raises(_FakeAdapterParseError):
        lm(messages=[{"role": "user", "content": "hi"}])
    assert calls["n"] == 1  # parse errors are NOT retried as transient


def test_call_exhausts_transient_retries_then_raises(monkeypatch):
    lm = cfg._io_logging_lm_cls()(model="openai/dummy")
    monkeypatch.setattr(cfg, "_lm_transient_retries", lambda: 2)
    monkeypatch.setattr(cfg, "_lm_transient_backoff_s", lambda: 0.0)

    calls = {"n": 0}

    def always_crash(self, prompt=None, messages=None, **kwargs):  # noqa: ANN001
        calls["n"] += 1
        raise _FakeMidStreamFallbackError("the model has crashed")

    monkeypatch.setattr(type(lm), "_clio_invoke_once", always_crash, raising=False)
    with pytest.raises(_FakeMidStreamFallbackError):
        lm(messages=[{"role": "user", "content": "hi"}])
    assert calls["n"] == 3  # initial + 2 retries


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
