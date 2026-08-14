"""Tests for the S5 per-session BringupTimerRegistry (iowarp/clio-agent#1215).

Covers the storage half seam call sites depend on: a session's FIRST
``timer_for_session`` call gets a real, live timer; every call after
``finish_bringup`` gets the no-op null timer (bring-up is a first-turn-only
concept, so turn 2+ must never silently start a new measurement window);
the LRU cap settles (never silently drops) an evicted still-open timer; and
``finish_bringup`` is a safe, idempotent no-op for a session that never
started bring-up.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.runtime.bringup_timing import (
    BringupTimer,
    BringupTimerRegistry,
    _NullBringupTimer,
    finish_bringup,
    timer_for_session,
)


def _fake_app() -> Any:
    return SimpleNamespace(state=SimpleNamespace())


def _capture_audits(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    audits: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "clio_agent.gact.runtime.bringup_timing.stream_audit",
        lambda stage, **fields: audits.append((stage, fields)),
    )
    return audits


def test_first_call_creates_a_real_timer_and_is_stable_across_calls() -> None:
    app = _fake_app()
    t1 = timer_for_session(app, "sess_a")
    t2 = timer_for_session(app, "sess_a")
    assert isinstance(t1, BringupTimer)
    # Sabotage: create a fresh timer on every call instead of caching -> t1 is t2 fails.
    assert t1 is t2
    # A registry was lazily installed on app.state (RULE 4: no new store --
    # reused across calls, not recreated).
    assert isinstance(app.state.bringup_timers, BringupTimerRegistry)


def test_different_sessions_get_independent_timers() -> None:
    app = _fake_app()
    a = timer_for_session(app, "sess_a")
    b = timer_for_session(app, "sess_b")
    assert a is not b
    assert isinstance(a, BringupTimer)
    assert isinstance(b, BringupTimer)


def test_after_finish_later_calls_return_the_null_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bring-up is first-turn-only: once settled, a session's later turns must
    never silently start measuring a whole new bring-up window."""

    app = _fake_app()
    timer_for_session(app, "sess_c").start_phase("session.create")
    timer_for_session(app, "sess_c").end_phase("session.create")

    summary = finish_bringup(app, "sess_c")
    assert summary is not None
    assert summary.session_id == "sess_c"

    later = timer_for_session(app, "sess_c")
    # Sabotage: keep creating a fresh BringupTimer after finish() -> this
    # isinstance check goes red, and a second bring-up window would silently
    # start being measured for an already-warm session.
    assert isinstance(later, _NullBringupTimer)
    # The null timer is a genuine no-op -- unconditionally callable, never raises.
    later.start_phase("enrichment")
    later.end_phase("enrichment")
    with later.phase("workspace.lease"):
        pass


def test_finish_bringup_never_started_is_a_safe_noop() -> None:
    app = _fake_app()
    assert finish_bringup(app, "sess_never_started") is None
    # A later timer_for_session call for that session still works normally
    # (never-started is distinct from already-settled).
    timer = timer_for_session(app, "sess_never_started")
    assert isinstance(timer, BringupTimer)


def test_finish_bringup_emits_exactly_one_summary_row(monkeypatch: pytest.MonkeyPatch) -> None:
    audits = _capture_audits(monkeypatch)
    app = _fake_app()
    timer_for_session(app, "sess_d").start_phase("session.create")
    timer_for_session(app, "sess_d").end_phase("session.create")
    finish_bringup(app, "sess_d")
    finish_bringup(app, "sess_d")  # idempotent -- must not re-emit

    summaries = [f for stage, f in audits if stage == "bringup.summary"]
    assert len(summaries) == 1
    assert summaries[0]["session_id"] == "sess_d"


def test_lru_cap_settles_rather_than_silently_drops_an_evicted_timer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audits = _capture_audits(monkeypatch)
    registry = BringupTimerRegistry(max_entries=2)
    registry.get_or_create("s1").start_phase("session.create")
    registry.get_or_create("s2").start_phase("session.create")
    # Third distinct session pushes the registry over its cap -- s1 (LRU) is evicted.
    registry.get_or_create("s3").start_phase("session.create")

    # Sabotage: drop the evicted timer without calling finish() -> no
    # bringup.summary row for s1 exists anywhere -> this assertion goes red
    # (a silently lost measurement, exactly what RULE "no silent fallback" forbids).
    summaries = [f for stage, f in audits if stage == "bringup.summary"]
    assert any(f["session_id"] == "s1" for f in summaries)

    # s1 is now settled -- a later get_or_create for it returns the null timer,
    # not a freshly reincarnated real one.
    assert isinstance(registry.get_or_create("s1"), _NullBringupTimer)


def test_concurrent_get_or_create_across_threads_is_safe() -> None:
    """Modest concurrency smoke: many threads racing get_or_create for a MIX
    of shared and distinct session ids never crashes and never hands out two
    different live timer objects for the same still-open session id."""

    app = _fake_app()
    session_ids = [f"sess_{i % 5}" for i in range(200)]  # 5 distinct ids, reused
    results: list[Any] = [None] * len(session_ids)
    errors: list[BaseException] = []

    def worker(index: int, sid: str) -> None:
        try:
            results[index] = timer_for_session(app, sid)
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i, sid)) for i, sid in enumerate(session_ids)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    assert not errors, errors
    by_session: dict[str, set[int]] = {}
    for sid, timer in zip(session_ids, results, strict=True):
        by_session.setdefault(sid, set()).add(id(timer))
    # Every distinct session id resolved to exactly ONE timer object across
    # every racing thread -- no duplicate BringupTimer was ever created for it.
    assert all(len(ids) == 1 for ids in by_session.values()), by_session
