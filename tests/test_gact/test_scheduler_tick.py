"""iowarp/clio-agent#766: scheduler firings must stage like user turns.

A fired schedule must go through the same staging as POST /messages --
the session flips to ``running`` (publishing ``session.status_changed``)
and the turn task is registered in ``app.state.in_flight_turns`` so
cancellation can reach it. Tick errors must be logged with a structured
reason, never silently swallowed, and the sleep between ticks must be
aligned to the next minute boundary instead of a flat 60s drift.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from clio_agent.gact.app import _scheduler_tick, build_app


@dataclass
class _Pred:
    answer: str = "ok"
    selected_expert: str = "data_expert"
    routing_rationale: str = ""


class _SlowAgent:
    """Blocks in the executor long enough for the test to observe/cancel."""

    def __init__(self, sleep_s: float = 1.0) -> None:
        self.sleep_s = sleep_s

    def forward(self, question: str, session_id: str):
        time.sleep(self.sleep_s)
        return _Pred()


def _make(tmp_path: Path, agent) -> tuple:
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    return app, client, sid


def test_fired_schedule_is_registered_and_cancellable(tmp_path: Path) -> None:
    """A due schedule stages a turn exactly like a user POST would."""

    app, client, sid = _make(tmp_path, _SlowAgent(sleep_s=1.0))
    app.state.schedules.add(session_id=sid, cron="* * * * *", question="scheduled q")

    async def drive() -> None:
        tick = asyncio.create_task(_scheduler_tick(app))
        try:
            # The first tick iteration runs immediately and fires the
            # always-due schedule.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 3.0
            while loop.time() < deadline:
                if app.state.in_flight_turns.get(sid) is not None:
                    break
                await asyncio.sleep(0.02)

            task = app.state.in_flight_turns.get(sid)
            assert task is not None, "scheduled turn was not registered in in_flight_turns"

            sess = app.state.sessions.get(sid)
            assert sess is not None
            assert sess.status == "running", (
                f"session status should be running while the scheduled turn "
                f"is in flight, got {sess.status!r}"
            )
            status_events = [
                e
                for e in app.state.bus._history.get(sid, [])
                if e.type == "session.status_changed" and e.payload.get("status") == "running"
            ]
            assert status_events, "no session.status_changed event for the scheduled turn"

            # The staged user message carries the schedule metadata.
            msgs = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
            user_msgs = [m for m in msgs if m["role"] == "user"]
            assert user_msgs
            meta = user_msgs[0].get("metadata", {})
            assert meta.get("scheduled") is True
            assert meta.get("schedule_id", "").startswith("sched_")

            # Cancellable: a cancel reaches the registered task, and the
            # done-callback drops the registration.
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5.0)
            assert app.state.in_flight_turns.get(sid) is None
        finally:
            tick.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick

    asyncio.run(drive())


def test_tick_scan_error_is_logged_not_swallowed(tmp_path: Path, caplog) -> None:
    """An injected due-scan failure produces a structured warning."""

    app, _client, _sid = _make(tmp_path, _SlowAgent(sleep_s=0.1))

    class _BoomStore:
        def due_now(self, when):
            raise RuntimeError("boom: injected tick failure")

    app.state.schedules = _BoomStore()

    async def drive() -> None:
        with caplog.at_level(logging.WARNING, logger="clio_agent.gact.app"):
            tick = asyncio.create_task(_scheduler_tick(app))
            try:
                loop = asyncio.get_running_loop()
                deadline = loop.time() + 3.0
                while loop.time() < deadline:
                    if any("schedule_due_scan_failed" in r.getMessage() for r in caplog.records):
                        break
                    await asyncio.sleep(0.02)
                assert not tick.done(), "scheduler tick loop died on a scan error"
            finally:
                tick.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await tick

    asyncio.run(drive())
    matching = [r for r in caplog.records if "schedule_due_scan_failed" in r.getMessage()]
    assert matching, "injected tick error was swallowed (no structured warning logged)"


def test_fire_error_is_logged_and_tick_survives(tmp_path: Path, caplog, monkeypatch) -> None:
    """A single schedule that fails to fire is logged and does not kill the loop."""

    import clio_agent.gact.app as gact_app

    app, _client, sid = _make(tmp_path, _SlowAgent(sleep_s=0.1))
    app.state.schedules.add(session_id=sid, cron="* * * * *", question="q")

    def _boom(*args, **kwargs):
        raise RuntimeError("boom: injected staging failure")

    monkeypatch.setattr(gact_app, "_turn_start_background_user_turn", _boom)

    async def drive() -> None:
        with caplog.at_level(logging.WARNING, logger="clio_agent.gact.app"):
            tick = asyncio.create_task(_scheduler_tick(app))
            try:
                loop = asyncio.get_running_loop()
                deadline = loop.time() + 3.0
                while loop.time() < deadline:
                    if any("schedule_fire_failed" in r.getMessage() for r in caplog.records):
                        break
                    await asyncio.sleep(0.02)
                assert not tick.done(), "scheduler tick loop died on a firing error"
            finally:
                tick.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await tick

    asyncio.run(drive())
    matching = [r for r in caplog.records if "schedule_fire_failed" in r.getMessage()]
    assert matching, "schedule firing error was swallowed (no structured warning logged)"
    assert any(sid in r.getMessage() for r in matching)


def test_schedules_list_surfaces_utc_cron_assumption(tmp_path: Path) -> None:
    """The schedules API states its UTC-only cron evaluation on the wire."""

    _app, client, sid = _make(tmp_path, _SlowAgent(sleep_s=0.1))
    body = client.get(f"/v1/sessions/{sid}/schedules").json()
    assert body["cron_timezone"] == "utc"


def test_sleep_aligns_to_next_minute_boundary() -> None:
    """The inter-tick sleep targets the next minute boundary, not now+60s."""

    from clio_agent.gact.app import _seconds_until_next_minute

    # Mid-minute: sleep the remaining ~30s, not a flat 60s.
    mid = datetime(2026, 7, 1, 12, 0, 30, tzinfo=timezone.utc)
    remaining = _seconds_until_next_minute(mid)
    assert 29.0 <= remaining <= 31.0

    # Just before the boundary: never a zero/negative sleep (no hot loop).
    late = datetime(2026, 7, 1, 12, 0, 59, 990000, tzinfo=timezone.utc)
    assert _seconds_until_next_minute(late) >= 0.5

    # On the boundary: roughly a full minute until the next one.
    exact = datetime(2026, 7, 1, 12, 1, 0, tzinfo=timezone.utc)
    assert 59.0 <= _seconds_until_next_minute(exact) <= 61.0
