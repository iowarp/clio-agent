"""Tests for the MCP commitment-wait activity tracker (iowarp/clio-agent#1230).

``commitment_wait_in_flight`` is the no-progress watchdog's "this session has
an honest, declared wait_for_terminal commitment open right now" gate --
mirrors ``runtime.lm_activity.lm_call_in_flight``'s per-session shape and the
#761 defect-2 lesson (a busy neighbor session must never keep a genuinely
wedged session's watchdog alive).
"""

from __future__ import annotations

from clio_agent.runtime import commitment_activity


def setup_function() -> None:
    commitment_activity._INFLIGHT.clear()


def teardown_function() -> None:
    commitment_activity._INFLIGHT.clear()


def test_not_in_flight_when_idle() -> None:
    assert commitment_activity.commitment_wait_in_flight() is False


def test_start_marks_in_flight_for_unattributed_bucket() -> None:
    commitment_activity.note_commitment_start()
    assert commitment_activity.commitment_wait_in_flight() is True
    assert commitment_activity._INFLIGHT[""] == 1


def test_drained_bucket_is_evicted() -> None:
    # No-unbounded-growth (mirrors lm_activity #761/#757): a session whose
    # commitment waits have all ended leaves NO residual bucket.
    commitment_activity.note_commitment_start()
    assert "" in commitment_activity._INFLIGHT
    commitment_activity.note_commitment_end()
    assert "" not in commitment_activity._INFLIGHT
    assert commitment_activity.commitment_wait_in_flight() is False


def test_bucket_survives_until_last_overlapping_wait_ends() -> None:
    commitment_activity.note_commitment_start()
    commitment_activity.note_commitment_start()
    commitment_activity.note_commitment_end()
    assert commitment_activity.commitment_wait_in_flight() is True
    commitment_activity.note_commitment_end()
    assert commitment_activity.commitment_wait_in_flight() is False


def test_session_scoped_lookup_ignores_other_sessions(monkeypatch) -> None:
    # #761 defect 2: a busy NEIGHBOR session's in-flight wait must never read
    # as progress for a DIFFERENT session's watchdog.
    calls = iter(["session-a", "session-a"])
    monkeypatch.setattr(commitment_activity, "_active_session", lambda: next(calls))
    commitment_activity.note_commitment_start()
    assert commitment_activity.commitment_wait_in_flight("session-a") is True
    assert commitment_activity.commitment_wait_in_flight("session-b") is False
    # session_id=None falls back to global-any (off-turn callers).
    assert commitment_activity.commitment_wait_in_flight() is True


def test_track_is_a_noop_when_not_unbounded() -> None:
    with commitment_activity.track(False):
        assert commitment_activity.commitment_wait_in_flight() is False
    assert commitment_activity.commitment_wait_in_flight() is False


def test_track_marks_and_clears_around_the_body() -> None:
    with commitment_activity.track(True):
        assert commitment_activity.commitment_wait_in_flight() is True
    assert commitment_activity.commitment_wait_in_flight() is False


def test_track_clears_even_when_the_body_raises() -> None:
    class _Boom(Exception):
        pass

    try:
        with commitment_activity.track(True):
            assert commitment_activity.commitment_wait_in_flight() is True
            raise _Boom
    except _Boom:
        pass
    assert commitment_activity.commitment_wait_in_flight() is False
