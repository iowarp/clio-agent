"""P4.3 (#1081): local-timezone/DST-correct cron, clamps, one-shot, jitter, retry,
overlap, and the cron_create/list/delete tool triad + /cron command.

The timezone/DST cases INJECT a reference timestamp + tz (never the wall clock) so the
next-fire computation is deterministic on any box. The tool triad tests drive the store
directly (server-generated result-only ids, read-back, cancel-both), and the command test
asserts the built-in /cron row exists.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from clio_agent.gact.scheduler import (
    CronError,
    Schedule,
    ScheduleStore,
    cron_matches,
    jitter_seconds,
    next_fire,
    validate_cron,
)

CHICAGO = ZoneInfo("America/Chicago")
EASTERN = ZoneInfo("America/New_York")
UTC = timezone.utc


# --------------------------------------------------------------------------- #
# Timezone correctness (injected reference — no wall-clock read).             #
# --------------------------------------------------------------------------- #
def test_next_fire_local_9am_differs_from_utc_interpretation() -> None:
    """"0 9 * * *" in America/Chicago computes 9am CHICAGO local, not 09:00 UTC."""

    # A summer reference: Chicago is CDT (UTC-5), so 9am local == 14:00 UTC.
    ref = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    fire = next_fire("0 9 * * *", ref, CHICAGO)
    assert fire is not None
    # It is the 9am Chicago instant...
    assert fire.astimezone(CHICAGO).hour == 9
    assert fire.astimezone(CHICAGO).minute == 0
    # ...and that UTC instant is 14:00, provably DIFFERENT from a UTC interpretation (09:00).
    assert fire == datetime(2026, 7, 1, 14, 0, tzinfo=UTC)
    utc_interpretation = next_fire("0 9 * * *", ref, UTC)
    assert utc_interpretation == datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    assert fire != utc_interpretation


def test_next_fire_winter_uses_standard_offset() -> None:
    """The SAME cron shifts UTC across DST: 9am Chicago is 15:00 UTC in winter (CST)."""

    ref = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    fire = next_fire("0 9 * * *", ref, CHICAGO)
    assert fire == datetime(2026, 1, 1, 15, 0, tzinfo=UTC)  # CST = UTC-6


def test_dst_spring_forward_gap_does_not_skip() -> None:
    """A wall time in the spring-forward gap still fires (mapped just past the jump)."""

    # US spring forward 2026: 2026-03-08, clocks jump 02:00 -> 03:00 (Eastern).
    # "30 2 * * *" names 02:30, which does not exist that day. It must NOT be skipped:
    # fold=0 maps it through the pre-jump offset, landing at the real 03:30 EDT instant.
    ref = datetime(2026, 3, 8, 0, 0, tzinfo=EASTERN).astimezone(UTC)
    fire = next_fire("30 2 * * *", ref, EASTERN)
    assert fire is not None
    local = fire.astimezone(EASTERN)
    assert (local.year, local.month, local.day) == (2026, 3, 8)
    # The instant is the one the clock jumped to — 03:30 EDT (07:30 UTC).
    assert fire == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)


def test_dst_fall_back_overlap_fires_once_not_twice() -> None:
    """A wall time in the fall-back overlap fires exactly ONCE (no double-fire)."""

    # US fall back 2026: 2026-11-01, clocks fall 02:00 -> 01:00 (Eastern); 01:30 occurs
    # twice (EDT then EST). "30 1 * * *" must fire once, then skip to the NEXT day.
    ref = datetime(2026, 11, 1, 0, 0, tzinfo=EASTERN).astimezone(UTC)
    first = next_fire("30 1 * * *", ref, EASTERN)
    assert first is not None
    # First occurrence is the EDT one (fold=0): 01:30 EDT == 05:30 UTC.
    assert first == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    # Recomputing strictly after that instant must NOT return the second (EST) 01:30
    # (06:30 UTC) — it jumps to the next day. That is the no-double-fire guarantee.
    second = next_fire("30 1 * * *", first + timedelta(minutes=1), EASTERN)
    assert second is not None
    assert second != datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
    assert second.astimezone(EASTERN).day == 2


# --------------------------------------------------------------------------- #
# Parser: ranges.                                                             #
# --------------------------------------------------------------------------- #
def test_cron_ranges_parse() -> None:
    """Ranges (N-M) and stepped ranges (N-M/S) parse and match."""

    # Weekdays 09:00 — Mon..Fri (cron dow 1-5).
    assert cron_matches("0 9 * * 1-5", datetime(2026, 7, 1, 9, 0))  # Wed
    assert not cron_matches("0 9 * * 1-5", datetime(2026, 7, 4, 9, 0))  # Sat
    # Hour range with a step: every 2 hours from 8..18.
    assert cron_matches("0 8-18/2 * * *", datetime(2026, 7, 1, 10, 0))
    assert not cron_matches("0 8-18/2 * * *", datetime(2026, 7, 1, 11, 0))
    # Minute range.
    assert cron_matches("10-20 * * * *", datetime(2026, 7, 1, 5, 15))
    assert not cron_matches("10-20 * * * *", datetime(2026, 7, 1, 5, 25))


def test_validate_cron_rejects_garbage() -> None:
    """Out-of-bounds / unparseable cron fields raise a typed invalid_cron."""

    validate_cron("0 9 * * 1-5")  # ok
    for bad in ("99 * * * *", "abc * * * *", "0 9 * *", "0 9 * * 1-", "*/0 * * * *"):
        with pytest.raises(CronError) as ei:
            validate_cron(bad)
        assert ei.value.reason == "invalid_cron"


# --------------------------------------------------------------------------- #
# Jitter: deterministic per id.                                              #
# --------------------------------------------------------------------------- #
def test_jitter_is_deterministic_per_id() -> None:
    """Same id -> same offset; disabled window -> 0; distinct ids spread."""

    assert jitter_seconds("sched_abc", 0) == 0
    a = jitter_seconds("sched_abc", 60)
    assert a == jitter_seconds("sched_abc", 60)  # stable across calls (restart-safe)
    assert 0 <= a < 60
    b = jitter_seconds("sched_def", 60)
    assert 0 <= b < 60
    # Highly likely distinct (sha256-derived) — assert the mechanism spreads at least once.
    assert jitter_seconds("id-1", 3600) != jitter_seconds("id-2", 3600)


def test_next_fire_applies_jitter() -> None:
    """A positive jitter offsets the fire instant off the minute boundary."""

    ref = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    base = next_fire("0 9 * * *", ref, CHICAGO, jitter_s=0)
    jittered = next_fire("0 9 * * *", ref, CHICAGO, jitter_s=17)
    assert (jittered - base).total_seconds() == 17


# --------------------------------------------------------------------------- #
# Store: create/list/delete (server-generated result-only id, read-back).     #
# --------------------------------------------------------------------------- #
def _store(tmp_path: Path) -> ScheduleStore:
    return ScheduleStore(path=tmp_path / "schedules.json")


def test_create_returns_stable_id_list_reads_back_delete_removes(tmp_path: Path) -> None:
    """cron_create -> stable server id; cron_list reads it back; cron_delete removes it."""

    store = _store(tmp_path)
    sch = store.create(session_id="sess_1", question="ping", cron="0 9 * * *")
    assert sch.id.startswith("sched_")
    assert sch.next_fire_at  # armed
    # read-back
    rows = store.list(session_id="sess_1")
    assert [r.id for r in rows] == [sch.id]
    assert rows[0].question == "ping"
    # delete removes it
    assert store.delete(sch.id) is True
    assert store.list(session_id="sess_1") == []
    assert store.delete(sch.id) is False  # idempotent


def test_id_is_server_generated_not_echoed(tmp_path: Path) -> None:
    """Two creates get distinct server ids (never a client-supplied value)."""

    store = _store(tmp_path)
    a = store.create(session_id="s", question="q", cron="0 9 * * *")
    b = store.create(session_id="s", question="q", cron="0 9 * * *")
    assert a.id != b.id


# --------------------------------------------------------------------------- #
# One-shot: run_at / delay_s fires once then is gone.                         #
# --------------------------------------------------------------------------- #
def test_run_at_one_shot_fires_once_then_deleted(tmp_path: Path) -> None:
    """A run_at schedule fires once and auto-deletes (recurring=False analog)."""

    store = _store(tmp_path)
    ref = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    sch = store.create(
        session_id="s", question="q", run_at="2026-07-01T12:05:00+00:00", now=ref
    )
    assert sch.recurring is False
    assert store.get(sch.id) is not None
    # Not yet due at ref.
    assert list(store.due_now(ref)) == []
    # Due at/after the run_at instant.
    fire_time = datetime(2026, 7, 1, 12, 5, tzinfo=UTC)
    due = list(store.due_now(fire_time))
    assert [d.id for d in due] == [sch.id]
    # Firing it deletes it (gone).
    store.mark_fired(sch.id, now=fire_time)
    assert store.get(sch.id) is None
    assert list(store.due_now(fire_time + timedelta(minutes=1))) == []


def test_delay_s_one_shot(tmp_path: Path) -> None:
    """delay_s arms a one-shot delay_s seconds out."""

    store = _store(tmp_path)
    ref = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    sch = store.create(session_id="s", question="q", delay_s=90, now=ref)
    assert sch.recurring is False
    from clio_agent.gact.scheduler import _parse_iso_utc

    assert _parse_iso_utc(sch.next_fire_at) == ref + timedelta(seconds=90)


# --------------------------------------------------------------------------- #
# Clamps: min-interval floor, max_fires, until (typed reasons).               #
# --------------------------------------------------------------------------- #
def test_min_interval_floor_rejects_with_typed_reason(tmp_path: Path, monkeypatch) -> None:
    """A sub-floor cron is rejected with a typed min_interval_below_floor."""

    monkeypatch.setenv("CLIO_SCHEDULER_MIN_INTERVAL_S", "300")
    store = _store(tmp_path)
    with pytest.raises(CronError) as ei:
        store.create(session_id="s", question="q", cron="* * * * *")  # 60s < 300s floor
    assert ei.value.reason == "min_interval_below_floor"
    # A coarser cron at/above the floor is accepted.
    ok = store.create(session_id="s", question="q", cron="*/5 * * * *")  # 300s == floor
    assert ok.next_fire_at


def test_max_fires_stops_recurrence_with_typed_reason(tmp_path: Path) -> None:
    """max_fires disables the schedule (typed reason) once reached."""

    store = _store(tmp_path)
    ref = datetime(2026, 7, 1, 8, 59, tzinfo=UTC)
    sch = store.create(session_id="s", question="q", cron="* * * * *", max_fires=2, now=ref)
    store.mark_fired(sch.id, now=ref)
    assert store.get(sch.id).enabled is True  # 1 of 2
    store.mark_fired(sch.id, now=ref + timedelta(minutes=1))
    retired = store.get(sch.id)
    assert retired.enabled is False
    assert retired.disabled_reason == "max_fires_reached"
    # No longer fires.
    assert list(store.due_now(ref + timedelta(minutes=5))) == []


def test_until_stops_recurrence_with_typed_reason(tmp_path: Path) -> None:
    """A schedule past its `until` is retired (typed until_reached) instead of firing."""

    store = _store(tmp_path)
    ref = datetime(2026, 7, 1, 8, 0, tzinfo=UTC)
    sch = store.create(
        session_id="s",
        question="q",
        cron="* * * * *",
        until="2026-07-01T09:00:00+00:00",
        now=ref,
    )
    # Before until: due.
    assert [d.id for d in store.due_now(datetime(2026, 7, 1, 8, 30, tzinfo=UTC))] == [sch.id]
    # At/after until: retired, not fired.
    assert list(store.due_now(datetime(2026, 7, 1, 9, 0, tzinfo=UTC))) == []
    retired = store.get(sch.id)
    assert retired.enabled is False
    assert retired.disabled_reason == "until_reached"


def test_max_lifetime_ceiling_applied_by_default(tmp_path: Path, monkeypatch) -> None:
    """A recurring schedule with no explicit until gets an auto lifetime ceiling."""

    monkeypatch.setenv("CLIO_SCHEDULER_MAX_LIFETIME_S", "3600")
    store = _store(tmp_path)
    ref = datetime(2026, 7, 1, 8, 0, tzinfo=UTC)
    sch = store.create(session_id="s", question="q", cron="* * * * *", now=ref)
    from clio_agent.gact.scheduler import _parse_iso_utc

    assert _parse_iso_utc(sch.until) == ref + timedelta(seconds=3600)


# --------------------------------------------------------------------------- #
# Failure handling: retry/backoff (deferred-not-dropped, typed).              #
# --------------------------------------------------------------------------- #
def test_record_fire_failure_retries_then_disables(tmp_path: Path) -> None:
    """A failed fire schedules a backoff retry (typed, not dropped); exhausting retries
    disables with a typed reason."""

    store = _store(tmp_path)
    ref = datetime(2026, 7, 1, 8, 0, tzinfo=UTC)
    sch = store.create(session_id="s", question="q", cron="* * * * *", now=ref)
    from clio_agent.gact.scheduler import _parse_iso_utc, max_retries

    store.record_fire_failure(sch.id, "boom", now=ref)
    after = store.get(sch.id)
    assert after.retry_count == 1
    assert after.last_error == "boom"
    assert after.enabled is True
    # Next fire pushed into the future (backoff), not dropped.
    assert _parse_iso_utc(after.next_fire_at) > ref
    # Exhaust the retry budget -> disabled with a typed reason.
    for i in range(max_retries() + 2):
        store.record_fire_failure(sch.id, "boom", now=ref + timedelta(minutes=i))
    dead = store.get(sch.id)
    assert dead.enabled is False
    assert dead.disabled_reason == "max_retries_exceeded"


def test_mark_fired_clears_retry_state(tmp_path: Path) -> None:
    """A successful fire resets the retry counter/error."""

    store = _store(tmp_path)
    ref = datetime(2026, 7, 1, 8, 0, tzinfo=UTC)
    sch = store.create(session_id="s", question="q", cron="* * * * *", now=ref)
    store.record_fire_failure(sch.id, "boom", now=ref)
    assert store.get(sch.id).retry_count == 1
    store.mark_fired(sch.id, now=ref + timedelta(minutes=1))
    ok = store.get(sch.id)
    assert ok.retry_count == 0
    assert ok.last_error == ""


# --------------------------------------------------------------------------- #
# Overlap policy (explicit typed choice).                                     #
# --------------------------------------------------------------------------- #
def test_overlap_policy_defaults_queue_and_validates(tmp_path: Path) -> None:
    """overlap_policy defaults to queue (deferred-not-dropped) and rejects garbage."""

    store = _store(tmp_path)
    sch = store.create(session_id="s", question="q", cron="0 9 * * *")
    assert sch.overlap_policy == "queue"
    skip = store.create(session_id="s", question="q", cron="0 9 * * *", overlap_policy="skip")
    assert skip.overlap_policy == "skip"
    with pytest.raises(CronError) as ei:
        store.create(session_id="s", question="q", cron="0 9 * * *", overlap_policy="nonsense")
    assert ei.value.reason == "invalid_overlap_policy"


# --------------------------------------------------------------------------- #
# Trigger validation.                                                         #
# --------------------------------------------------------------------------- #
def test_missing_and_ambiguous_triggers(tmp_path: Path) -> None:
    """Exactly one trigger is required."""

    store = _store(tmp_path)
    with pytest.raises(CronError) as e1:
        store.create(session_id="s", question="q")
    assert e1.value.reason == "missing_trigger"
    with pytest.raises(CronError) as e2:
        store.create(session_id="s", question="q", cron="0 9 * * *", delay_s=30)
    assert e2.value.reason == "ambiguous_trigger"


def test_invalid_timezone_typed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(CronError) as ei:
        store.create(session_id="s", question="q", cron="0 9 * * *", timezone_name="Mars/Phobos")
    assert ei.value.reason == "invalid_timezone"


# --------------------------------------------------------------------------- #
# Persistence: survives a reload; unknown/forward keys tolerated.             #
# --------------------------------------------------------------------------- #
def test_schedule_survives_store_reload(tmp_path: Path) -> None:
    """schedules.json round-trips a full P4.3 schedule (daemon-restart durability)."""

    path = tmp_path / "schedules.json"
    store = ScheduleStore(path=path)
    sch = store.create(session_id="s", question="q", cron="0 9 * * *", timezone_name="America/Chicago")
    reloaded = ScheduleStore(path=path)
    got = reloaded.get(sch.id)
    assert got is not None
    assert got.timezone == "America/Chicago"
    assert got.next_fire_at == sch.next_fire_at


def test_forward_version_row_drops_unknown_keys(tmp_path: Path) -> None:
    """A row with an unknown (forward-version) field loads by dropping the extra key."""

    import json

    path = tmp_path / "schedules.json"
    row = {
        "id": "sched_future00000",
        "session_id": "s",
        "cron": "0 9 * * *",
        "question": "q",
        "some_future_field": 123,  # unknown -> dropped, not a crash
    }
    path.write_text(json.dumps({"schedules": [row]}), encoding="utf-8")
    store = ScheduleStore(path=path)
    assert store.get("sched_future00000") is not None


# --------------------------------------------------------------------------- #
# Seams (stored, not wired) — present so P4.x can build on them.              #
# --------------------------------------------------------------------------- #
def test_seam_fields_stored(tmp_path: Path) -> None:
    """goal / cross_run_memory / spawn_task ride the row as forward-compat seams."""

    store = _store(tmp_path)
    sch = store.create(
        session_id="s",
        question="q",
        cron="*/5 * * * *",
        goal="until the file exists",
        cross_run_memory="fresh",
        spawn_task=True,
    )
    got = store.get(sch.id)
    assert got.goal == "until the file exists"
    assert got.cross_run_memory == "fresh"
    assert got.spawn_task is True


def test_schedule_dataclass_defaults() -> None:
    """A bare Schedule (legacy row shape) still constructs with P4.3 defaults."""

    sch = Schedule(id="sched_x", session_id="s", cron="* * * * *", question="q")
    assert sch.timezone == "UTC"
    assert sch.recurring is True
    assert sch.overlap_policy == "queue"


# --------------------------------------------------------------------------- #
# Runtime tick: overlap skip vs queue, and retry-not-dropped on fire failure.  #
# --------------------------------------------------------------------------- #
from types import SimpleNamespace  # noqa: E402

from clio_agent.gact import scheduler_runtime as _rt  # noqa: E402


def _fake_app(store: ScheduleStore, *, busy: bool) -> SimpleNamespace:
    state = SimpleNamespace(
        sessions=SimpleNamespace(get=lambda _sid: SimpleNamespace(status="running")),
        turn_runner=SimpleNamespace(busy=lambda _sid: busy),
        deferred_schedules=set(),
        schedules=store,
    )
    return SimpleNamespace(state=state)


def test_overlap_skip_drops_occurrence_and_advances(tmp_path: Path) -> None:
    """overlap_policy='skip' on a busy session advances past the occurrence (no defer)."""

    store = _store(tmp_path)
    ref = datetime(2026, 7, 1, 8, 59, tzinfo=UTC)
    sch = store.create(
        session_id="s", question="q", cron="* * * * *", overlap_policy="skip", now=ref
    )
    before = store.get(sch.id).fire_count
    app = _fake_app(store, busy=True)
    _rt._fire_schedule(app, store.get(sch.id))
    # Skipped: NOT deferred, and the fire counter advanced (occurrence consumed).
    assert sch.id not in app.state.deferred_schedules
    assert store.get(sch.id).fire_count == before + 1


def test_overlap_queue_defers_on_busy(tmp_path: Path) -> None:
    """overlap_policy='queue' (default) defers a busy occurrence for retry (not dropped)."""

    store = _store(tmp_path)
    sch = store.create(session_id="s", question="q", cron="* * * * *", overlap_policy="queue")
    before = store.get(sch.id).fire_count
    app = _fake_app(store, busy=True)
    _rt._fire_schedule(app, store.get(sch.id))
    assert sch.id in app.state.deferred_schedules  # queued for retry
    assert store.get(sch.id).fire_count == before  # not consumed


def test_tick_fire_failure_records_retry_not_dropped(tmp_path: Path, monkeypatch) -> None:
    """A staging failure in the tick records a backoff retry (deferred-not-dropped)."""

    store = _store(tmp_path)
    sch = store.create(session_id="s", question="q", cron="* * * * *")
    app = _fake_app(store, busy=False)

    def _boom(_app, _sch):
        raise RuntimeError("staging blew up")

    monkeypatch.setattr(_rt, "_fire_schedule", _boom)
    _rt._fire_one(app, store.get(sch.id))
    after = store.get(sch.id)
    assert after.retry_count == 1  # retried, not silently dropped
    assert "staging blew up" in after.last_error


# --------------------------------------------------------------------------- #
# Mark-after-stage ordering (#1031 P4.3 adversarial review): a staging        #
# exception raised from the REAL `_fire_schedule` (not a wholesale-replaced   #
# stub) must not have already counted the occurrence via `mark_fired`.       #
# --------------------------------------------------------------------------- #
def test_fire_failure_retries_one_shot_not_dropped(tmp_path: Path, monkeypatch) -> None:
    """A one-shot schedule whose staging raises is retried, not dropped.

    Regression: `_fire_schedule` used to call `store.mark_fired(sch.id)` BEFORE
    staging. `mark_fired` POPS a one-shot row outright, so when staging then
    raised, `_fire_one`'s `record_fire_failure(sch.id, ...)` looked the row up
    by id, found it already gone, and silently no-opped (#687-689 early
    return) -- the occurrence was dropped with no retry. This drives the REAL
    `_fire_schedule` (only the staging call is stubbed) so the bug is not
    masked the way `test_tick_fire_failure_records_retry_not_dropped` above
    masks it by replacing `_fire_schedule` wholesale.
    """

    import clio_agent.gact.app as gact_app

    store = _store(tmp_path)
    sch = store.create(session_id="s", question="q", delay_s=1)
    app = _fake_app(store, busy=False)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("staging blew up")

    monkeypatch.setattr(gact_app, "_turn_start_background_user_turn", _boom)
    _rt._fire_one(app, store.get(sch.id))

    after = store.get(sch.id)
    assert after is not None, "one-shot schedule was dropped instead of retried"
    assert after.retry_count == 1
    assert "staging blew up" in after.last_error
    assert after.next_fire_at, "a retry must be scheduled, not left blank"


def test_fire_failure_does_not_overcount_recurring(tmp_path: Path, monkeypatch) -> None:
    """A recurring schedule whose staging raises must not advance past the failure.

    Regression: the old mark-before-stage order incremented `fire_count` and
    recomputed `next_fire_at` for an occurrence that never actually ran, so a
    `max_fires` ceiling would retire the schedule after fewer real executions
    than requested, and the next real attempt would be delayed by a full cron
    period it never earned. This drives the REAL `_fire_schedule`.
    """

    import clio_agent.gact.app as gact_app

    store = _store(tmp_path)
    ref = datetime(2026, 7, 1, 8, 59, tzinfo=UTC)
    sch = store.create(session_id="s", question="q", cron="* * * * *", now=ref)
    before = store.get(sch.id)
    before_fire_count = before.fire_count
    before_last_fired_at = before.last_fired_at
    app = _fake_app(store, busy=False)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("staging blew up")

    monkeypatch.setattr(gact_app, "_turn_start_background_user_turn", _boom)
    _rt._fire_one(app, store.get(sch.id))

    after = store.get(sch.id)
    # The occurrence never ran, so it must not be counted: fire_count/last_fired_at
    # stay put. `next_fire_at` DOES change -- that's `record_fire_failure` legitimately
    # scheduling a short backoff retry, not `mark_fired`'s "advance to the next real
    # cron occurrence" (which would be a full minute out, not a sub-second backoff).
    assert after.fire_count == before_fire_count, "failed occurrence must not be counted"
    assert after.last_fired_at == before_last_fired_at, "must not record a fire that never ran"
    assert after.retry_count == 1
    assert "staging blew up" in after.last_error
