"""Tests for token-streaming liveness (Trace v2 T4).

Two units:
- ``lm_call_in_flight`` is the no-progress watchdog's "this call still counts as
  progress" gate. With token-liveness streaming, ``note_lm_activity`` refreshes it
  per chunk so a slow-but-generating reasoning model is never false-killed, while a
  genuinely frozen (0-token) call stops refreshing and is abandoned at the idle
  window -- not the 1800s ceiling.
- ``IOLoggingLM._clio_streamed_call`` drives a call streamed (drain-and-discard each
  chunk -> ``note_lm_activity``) while ``aforward`` assembles the authoritative
  result. Real LM errors propagate as themselves; the call is one provider call
  with one ``lm.call`` log, never re-issued blocking.
"""

from __future__ import annotations

import pytest

from clio_agent import config as cfg
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


def test_provider_lm_kwargs_exposes_sampling_surface():
    # llama.cpp (provider_id="llama_cpp"), not lm_studio: model-capabilities
    # brief 5.2's supplement table only adds top_k/min_p to llama.cpp's and
    # vLLM's accepted-parameter set -- LM Studio's real LiteLLM-mapped set
    # never includes them, so they are correctly OMITTED for lm_studio now
    # (fail closed, request_builder.py), not sent unconditionally as before.
    c = cfg.LMProviderConfig(
        provider="openai",
        provider_id="llama_cpp",
        model="qwopus3.5-9b-v3",
        api_base="http://127.0.0.1:8088/v1",
        top_p=0.95,
        top_k=20,
        min_p=0.0,
        presence_penalty=0.5,
    )
    extras = cfg.build_request_kwargs(c)
    # OpenAI-standard -> direct kwargs.
    assert extras["top_p"] == 0.95
    assert extras["presence_penalty"] == 0.5
    # Non-OpenAI -> extra_body (llama.cpp/vLLM).
    assert extras["extra_body"]["top_k"] == 20
    assert extras["extra_body"]["min_p"] == 0.0


def test_provider_lm_kwargs_omits_unset_sampling():
    c = cfg.LMProviderConfig(
        provider="openai", provider_id="llama_cpp", model="m", api_base="http://127.0.0.1:8088/v1"
    )
    extras = cfg.build_request_kwargs(c)
    assert "top_p" not in extras
    assert "presence_penalty" not in extras
    assert "top_k" not in (extras.get("extra_body") or {})
    assert "min_p" not in (extras.get("extra_body") or {})
