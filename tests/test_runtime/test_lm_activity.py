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
from clio_agent.lm import io_logging
from clio_agent.runtime import lm_activity


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    # Isolate the per-session tracker between tests and pin the clock. These unit
    # tests drive note_lm_* with no GACT session bound, so all activity lands in
    # the unattributed "" bucket and lm_call_in_flight() (no arg) reads it via the
    # global-any fallback.
    lm_activity._STATE.clear()
    monkeypatch.delenv("CLIO_MAX_LM_CALL_S", raising=False)
    monkeypatch.delenv("CLIO_LM_INTER_TOKEN_IDLE_S", raising=False)
    yield
    lm_activity._STATE.clear()


def _clock(monkeypatch):
    """Return a setter for a monotonic clock the module reads."""
    box = {"now": 1000.0}
    monkeypatch.setattr(lm_activity.time, "monotonic", lambda: box["now"])
    return box


def test_not_in_flight_when_idle():
    assert lm_activity.lm_call_in_flight() is False


def test_drained_session_bucket_is_evicted():
    # #761/#757 no-unbounded-growth: per-session buckets must not accumulate. A
    # session whose LM calls have all ended leaves NO residual bucket in _STATE.
    lm_activity.note_lm_start()
    assert "" in lm_activity._STATE  # unattributed bucket created on start
    lm_activity.note_lm_end()
    assert "" not in lm_activity._STATE  # drained -> evicted (was retained before the fix)
    assert lm_activity.lm_call_in_flight() is False


def test_bucket_survives_until_last_overlapping_call_ends():
    # Eviction must key on the drain, not any end: with two overlapping calls in
    # one bucket, the bucket persists until the LAST one ends.
    lm_activity.note_lm_start()
    lm_activity.note_lm_start()
    lm_activity.note_lm_end()
    assert "" in lm_activity._STATE  # one still in flight -> bucket retained
    assert lm_activity.lm_call_in_flight() is True
    lm_activity.note_lm_end()
    assert "" not in lm_activity._STATE  # both ended -> evicted
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


def test_note_lm_activity_for_is_a_noop_for_the_unattributed_bucket(monkeypatch):
    """``session_id=""`` (unattributed/off-turn) is falsy -- ``note_lm_activity_for``
    must no-op for it exactly like an empty ``session_id`` anywhere else in
    this module, never silently landing on the wrong (global) bucket."""
    lm_activity.note_lm_start()  # lands in the "" unattributed bucket
    lm_activity.note_lm_activity_for("")
    assert lm_activity._STATE[""]["queued_last"] == 0.0


def test_queued_signal_keeps_prefill_ceiling_past_the_streaming_idle_window(monkeypatch):
    """F5 (#1305 review round): a queued connect-slot wait (note_lm_activity_for)
    must NOT silently flip the call into STREAMING regime -- it stays under the
    generous prefill ceiling, refreshed off ``queued_last``, not the tight
    120s inter-token-idle window.

    SABOTAGE: have ``note_lm_activity_for`` write ``last`` instead of a
    distinct ``queued_last`` -> ``last > started`` becomes true -> the call
    is misclassified STREAMING and this goes red at the 121s mark.
    """
    clk = _clock(monkeypatch)
    lm_activity._STATE["sess-q1"] = {
        "inflight": 1.0,
        "started": clk["now"],
        "last": clk["now"],
        "queued_last": 0.0,
    }
    lm_activity.note_lm_activity_for("sess-q1")
    clk["now"] += 121.0  # past the 120s STREAMING idle window
    # Still in flight: queued regime uses the prefill ceiling, not the
    # streaming idle window -- this would be False if misclassified.
    assert lm_activity.lm_call_in_flight("sess-q1") is True


def test_queued_signal_extends_past_the_static_started_ceiling_when_refreshed(monkeypatch):
    """F5: a queue that genuinely outlasts the per-call ceiling measured off
    ``started`` still counts as progress for as long as it keeps refreshing
    ``queued_last`` -- the whole point of a REFRESHED queued signal over the
    static NON-STREAMING fallback.
    """
    clk = _clock(monkeypatch)
    lm_activity._STATE["sess-q2"] = {
        "inflight": 1.0,
        "started": clk["now"],
        "last": clk["now"],
        "queued_last": 0.0,
    }
    for _ in range(20):
        clk["now"] += 200.0  # 20 * 200 = 4000s total, past the 1800s ceiling
        lm_activity.note_lm_activity_for("sess-q2")
        assert lm_activity.lm_call_in_flight("sess-q2") is True


def test_queued_signal_without_refresh_still_falls_back_to_started_ceiling(monkeypatch):
    """F5: a bucket that was NEVER queued (``queued_last`` stays 0) is
    unaffected by the new regime -- the original NON-STREAMING/prefill
    fallback measured off ``started`` is unchanged."""
    clk = _clock(monkeypatch)
    lm_activity.note_lm_start()
    clk["now"] += 1801.0
    assert lm_activity.lm_call_in_flight() is False


def test_real_streaming_always_wins_over_a_stale_queued_signal(monkeypatch):
    """F5: once a real token has streamed (``last`` > ``started``), the
    STREAMING regime's tighter idle window is authoritative regardless of
    whether ``queued_last`` is also fresh -- a queued signal must never
    resurrect a call the streaming regime has already judged dead.
    """
    clk = _clock(monkeypatch)
    lm_activity._STATE["sess-q3"] = {
        "inflight": 1.0,
        "started": clk["now"],
        "last": clk["now"],
        "queued_last": 0.0,
    }
    lm_activity.note_lm_activity_for("sess-q3")  # queued BEFORE the connect landed
    clk["now"] += 5.0
    lm_activity._STATE["sess-q3"]["last"] = clk["now"]  # first real token -> STREAMING engaged
    clk["now"] += 121.0  # past the 120s inter-token window; queued_last is stale
    assert lm_activity.lm_call_in_flight("sess-q3") is False


def test_note_lm_start_resets_a_stale_queued_last_from_a_dirty_bucket():
    """Residual 4 (#1305 round 3): with >1 concurrent call in the SAME
    session, a NEW note_lm_start() must not let an earlier (already-ended)
    call's ``queued_last`` survive -- a stale queued regime from a call that
    is no longer even running must never influence a brand-new call's own
    liveness classification.

    SABOTAGE: drop the ``st["queued_last"] = 0.0`` line from
    ``note_lm_start`` -> the dirty value survives -> this goes red.
    """
    # A dirty bucket: as if a PRIOR overlapping call in this session queued
    # (and has since ended) without note_lm_start ever resetting it.
    lm_activity._STATE[""] = {
        "inflight": 1.0,
        "started": 500.0,
        "last": 500.0,
        "queued_last": 900.0,
    }
    lm_activity.note_lm_start()
    assert lm_activity._STATE[""]["queued_last"] == 0.0


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


def test_provider_lm_kwargs_exposes_sampling_surface():
    c = cfg.LMProviderConfig(
        provider="lm_studio",
        model="qwopus3.5-9b-v3",
        top_p=0.95,
        top_k=20,
        min_p=0.0,
        presence_penalty=0.5,
    )
    extras = cfg._provider_lm_kwargs(c)
    # OpenAI-standard -> direct kwargs.
    assert extras["top_p"] == 0.95
    assert extras["presence_penalty"] == 0.5
    # Non-OpenAI -> extra_body (llama.cpp/LM Studio/vLLM).
    assert extras["extra_body"]["top_k"] == 20
    assert extras["extra_body"]["min_p"] == 0.0


def test_provider_lm_kwargs_omits_unset_sampling():
    c = cfg.LMProviderConfig(provider="lm_studio", model="m")
    extras = cfg._provider_lm_kwargs(c)
    assert "top_p" not in extras
    assert "presence_penalty" not in extras
    assert "top_k" not in (extras.get("extra_body") or {})
    assert "min_p" not in (extras.get("extra_body") or {})


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

    # httpx/litellm timeouts are transient (provider slowness/stall) -> retry.
    class ReadTimeout(Exception):
        pass

    assert cfg._is_transient_provider_error(ReadTimeout("Read timed out"))
    assert cfg._is_transient_provider_error(TimeoutError("request timeout"))
    # NOT transient -> the repair loop owns these, never retried as transient.
    assert not cfg._is_transient_provider_error(_FakeAdapterParseError("bad fields"))
    assert not cfg._is_transient_provider_error(ValueError("provider exploded"))


def test_call_retries_transient_then_succeeds(monkeypatch):
    lm = cfg._io_logging_lm_cls()(model="openai/dummy")
    monkeypatch.setattr(io_logging, "_lm_transient_retries", lambda: 2)
    monkeypatch.setattr(io_logging, "_lm_transient_backoff_s", lambda: 0.0)

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
    monkeypatch.setattr(io_logging, "_lm_transient_retries", lambda: 3)
    monkeypatch.setattr(io_logging, "_lm_transient_backoff_s", lambda: 0.0)

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
    monkeypatch.setattr(io_logging, "_lm_transient_retries", lambda: 2)
    monkeypatch.setattr(io_logging, "_lm_transient_backoff_s", lambda: 0.0)

    calls = {"n": 0}

    def always_crash(self, prompt=None, messages=None, **kwargs):  # noqa: ANN001
        calls["n"] += 1
        raise _FakeMidStreamFallbackError("the model has crashed")

    monkeypatch.setattr(type(lm), "_clio_invoke_once", always_crash, raising=False)
    with pytest.raises(_FakeMidStreamFallbackError):
        lm(messages=[{"role": "user", "content": "hi"}])
    assert calls["n"] == 3  # initial + 2 retries


# --- D15: retry-boundary discard (duplicated narration on the wire) --------


def test_call_retry_discards_live_streamed_part_before_reissue(monkeypatch):
    """D15 root cause: a transient failure can land AFTER the failed attempt
    already streamed its answer/next_thought text live (the streamed path
    flushes per-chunk and on close, both BEFORE the exception propagates —
    ``io_logging.py``'s ``_clio_streamed_call``). Re-issuing without discarding
    that already-streamed content lands the retry's fresh stream on top of it in
    the SAME still-open transcript part -- the exact duplicate paragraph observed
    live (sess_539d24da07bf part_2b645566433b). The retry loop must call
    ``note_lm_retry_reset`` exactly once per actual retry, BEFORE re-issuing --
    never on the call that finally succeeds."""

    lm = cfg._io_logging_lm_cls()(model="openai/dummy")
    monkeypatch.setattr(io_logging, "_lm_transient_retries", lambda: 2)
    monkeypatch.setattr(io_logging, "_lm_transient_backoff_s", lambda: 0.0)

    reset_calls = {"n": 0}
    monkeypatch.setattr(
        lm_activity,
        "note_lm_retry_reset",
        lambda: reset_calls.__setitem__("n", reset_calls["n"] + 1),
    )

    calls = {"n": 0}

    def flaky_twice(self, prompt=None, messages=None, **kwargs):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _FakeMidStreamFallbackError("the model has crashed")
        return ["RECOVERED"]

    monkeypatch.setattr(type(lm), "_clio_invoke_once", flaky_twice, raising=False)
    out = lm(messages=[{"role": "user", "content": "hi"}])
    assert out == ["RECOVERED"]
    assert calls["n"] == 3  # crashed twice, retried twice, succeeded on the 3rd
    assert reset_calls["n"] == 2  # once per retry -- NOT on the final success


def test_call_non_transient_error_never_discards(monkeypatch):
    """A non-transient (typed-output/parse) error is never retried, so it must
    never trigger a discard either -- the repair loop owns it unchanged."""

    lm = cfg._io_logging_lm_cls()(model="openai/dummy")
    monkeypatch.setattr(io_logging, "_lm_transient_retries", lambda: 3)
    monkeypatch.setattr(io_logging, "_lm_transient_backoff_s", lambda: 0.0)

    reset_calls = {"n": 0}
    monkeypatch.setattr(
        lm_activity,
        "note_lm_retry_reset",
        lambda: reset_calls.__setitem__("n", reset_calls["n"] + 1),
    )
    monkeypatch.setattr(
        type(lm),
        "_clio_invoke_once",
        lambda self, prompt=None, messages=None, **kw: (_ for _ in ()).throw(
            _FakeAdapterParseError("missing fields")
        ),
        raising=False,
    )
    with pytest.raises(_FakeAdapterParseError):
        lm(messages=[{"role": "user", "content": "hi"}])
    assert reset_calls["n"] == 0


def test_call_success_without_retry_never_discards(monkeypatch):
    """The overwhelmingly common (non-retried) call path must never discard --
    that would defeat live progressive streaming for every ordinary call."""

    lm = cfg._io_logging_lm_cls()(model="openai/dummy")
    reset_calls = {"n": 0}
    monkeypatch.setattr(
        lm_activity,
        "note_lm_retry_reset",
        lambda: reset_calls.__setitem__("n", reset_calls["n"] + 1),
    )
    monkeypatch.setattr(
        type(lm),
        "_clio_invoke_once",
        lambda self, prompt=None, messages=None, **kw: ["OK"],
        raising=False,
    )
    out = lm(messages=[{"role": "user", "content": "hi"}])
    assert out == ["OK"]
    assert reset_calls["n"] == 0


def test_note_lm_retry_reset_calls_bound_discard_hook():
    """The lm_activity-level wiring: ``note_lm_retry_reset`` calls whatever
    ``discard_open`` hook ``set_live_chunk_emitter`` bound, synchronously, in
    THIS thread -- no cross-thread scheduling (unlike ``note_lm_answer_delta``),
    matching ``record_dedup``'s calling convention."""

    calls: list[int] = []
    token = lm_activity._LIVE_CHUNK_EMITTER.set((None, None, None, lambda: calls.append(1)))
    try:
        lm_activity.note_lm_retry_reset()
    finally:
        lm_activity._LIVE_CHUNK_EMITTER.reset(token)
    assert calls == [1]


def test_note_lm_retry_reset_is_noop_without_discard_hook():
    """A pre-D15 3-arg ``set_live_chunk_emitter`` bind (``discard_open`` unset)
    must be a safe no-op, never an error."""

    token = lm_activity._LIVE_CHUNK_EMITTER.set((None, None, None, None))
    try:
        lm_activity.note_lm_retry_reset()  # must not raise
    finally:
        lm_activity._LIVE_CHUNK_EMITTER.reset(token)


def test_note_lm_retry_reset_is_noop_off_turn():
    """No emitter bound at all (CLI/optimizer/off-turn) -- must not raise."""

    assert lm_activity._LIVE_CHUNK_EMITTER.get() is None
    lm_activity.note_lm_retry_reset()


def test_process_completion_falls_back_to_reasoning_content(monkeypatch):
    # Reasoning models (qwopus) intermittently put the full formatted output in
    # reasoning_content with content empty; dspy parses output["text"] (content) ->
    # empty -> all fields missing. The override must substitute reasoning_content
    # for an empty text, and leave normal outputs untouched.
    import dspy.clients.base_lm as base_lm

    def fake_super(self, response, merged_kwargs):  # noqa: ANN001
        return [
            {"text": "", "reasoning_content": "REASONING_JSON"},  # empty content
            "plain-string-output",  # text-only -> untouched
            {"text": "real-content", "reasoning_content": "ignored"},  # has content
            {"text": "   ", "reasoning_content": "WS_FALLBACK"},  # whitespace content
        ]

    monkeypatch.setattr(base_lm.BaseLM, "_process_completion", fake_super)
    lm = cfg._io_logging_lm_cls()(model="openai/dummy")
    out = lm._process_completion(object(), {})
    assert out[0]["text"] == "REASONING_JSON"  # empty content -> reasoning_content
    assert out[1] == "plain-string-output"  # string untouched
    assert out[2]["text"] == "real-content"  # non-empty content untouched
    assert out[3]["text"] == "WS_FALLBACK"  # whitespace-only content -> fallback


def test_process_completion_skips_truncated_runaway_reasoning(monkeypatch):
    # Regression guard: a runaway/truncated CoT (finish_reason='length', 100k+ chars)
    # must NOT be substituted -- doing so bloats downstream prompts to context
    # overflow (observed: 132k reasoning -> 280k delegation output -> 71k-token
    # prompt -> n_keep > n_ctx).
    from types import SimpleNamespace

    import dspy.clients.base_lm as base_lm

    def fake_super(self, response, merged_kwargs):  # noqa: ANN001
        return [{"text": "", "reasoning_content": "X" * 60000}]

    monkeypatch.setattr(base_lm.BaseLM, "_process_completion", fake_super)
    lm = cfg._io_logging_lm_cls()(model="openai/dummy")
    resp = SimpleNamespace(choices=[SimpleNamespace(finish_reason="length")])
    out = lm._process_completion(resp, {})
    assert out[0]["text"] == ""  # truncated runaway reasoning NOT substituted


def test_process_completion_skips_oversized_complete_reasoning(monkeypatch):
    from types import SimpleNamespace

    import dspy.clients.base_lm as base_lm

    def fake_super(self, response, merged_kwargs):  # noqa: ANN001
        return [{"text": "", "reasoning_content": "Y" * 60000}]

    monkeypatch.setattr(base_lm.BaseLM, "_process_completion", fake_super)
    lm = cfg._io_logging_lm_cls()(model="openai/dummy")
    resp = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop")])
    out = lm._process_completion(resp, {})
    assert out[0]["text"] == ""  # >48k chars: over the size cap, not substituted


def test_process_completion_substitutes_complete_sane_reasoning(monkeypatch):
    from types import SimpleNamespace

    import dspy.clients.base_lm as base_lm

    def fake_super(self, response, merged_kwargs):  # noqa: ANN001
        return [{"text": "", "reasoning_content": '{"next_expert":"synthesis"}'}]

    monkeypatch.setattr(base_lm.BaseLM, "_process_completion", fake_super)
    lm = cfg._io_logging_lm_cls()(model="openai/dummy")
    resp = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop")])
    out = lm._process_completion(resp, {})
    assert out[0]["text"] == '{"next_expert":"synthesis"}'  # complete + sane -> used


def test_process_completion_no_fallback_without_reasoning(monkeypatch):
    import dspy.clients.base_lm as base_lm

    def fake_super(self, response, merged_kwargs):  # noqa: ANN001
        return [{"text": "", "reasoning_content": ""}, {"text": ""}]

    monkeypatch.setattr(base_lm.BaseLM, "_process_completion", fake_super)
    lm = cfg._io_logging_lm_cls()(model="openai/dummy")
    out = lm._process_completion(object(), {})
    # Nothing to fall back to -> text stays empty (real failure surfaces normally).
    assert out[0]["text"] == ""
    assert out[1]["text"] == ""


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
