"""iowarp/clio-agent#766: scheduler firings must stage like user turns.

A fired schedule must go through the same staging as POST /messages --
the session flips to ``running`` (publishing ``session.status_changed``)
and TurnRunner holds its real reservation so cancellation can reach it.
Tick errors must be logged with a structured
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
from functools import partial
from pathlib import Path
from typing import Any

import pytest
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


def _make(
    tmp_path: Path, agent: Any, request: pytest.FixtureRequest
) -> tuple[Any, TestClient, str]:
    from clio_agent.gact import app as app_module

    async def parked_scheduler(_app: Any) -> None:
        await asyncio.Event().wait()

    # These tests drive the real tick explicitly. A second lifespan-owned tick
    # can fire the same due schedule while the manual tick is staging its turn.
    monkeypatch = request.getfixturevalue("monkeypatch")
    monkeypatch.setattr(app_module, "_scheduler_tick", parked_scheduler)
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    client = TestClient(app)
    client.__enter__()
    request.addfinalizer(partial(client.__exit__, None, None, None))
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    return app, client, sid


def test_fired_schedule_is_registered_and_cancellable(
    tmp_path: Path, host_agent_executor: Any, request: pytest.FixtureRequest
) -> None:
    """A due schedule stages a turn exactly like a user POST would.

    Needs ``host_agent_executor``: 287dcf1e made a bare session execute the
    code-shipped builtin react ``main`` on the SAME tool-executor-gated react
    runtime seam as blueprint agents, so the fired schedule only reaches the
    ``build_app(agent=...)`` host fake (``_SlowAgent`` here) via that seam.
    Without it the turn errors with ``custom_agent_tool_executor_unavailable``
    synchronously instead of staying in flight for ``sleep_s``, so it never
    gets caught by the ``in_flight_turns`` poll.
    """

    app, client, sid = _make(tmp_path, _SlowAgent(sleep_s=1.0), request)
    app.state.schedules.add(session_id=sid, cron="* * * * *", question="scheduled q")

    async def drive() -> None:
        tick = asyncio.create_task(_scheduler_tick(app))
        try:
            # The first tick iteration runs immediately and fires the
            # always-due schedule.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 3.0
            while loop.time() < deadline:
                if (
                    app.state.turn_runner.busy(sid)
                    and app.state.sessions.get(sid).status == "running"
                    and app.state.task_history.acknowledged_notifications(sid)
                    and any(
                        event.type == "session.status_changed"
                        and event.payload.get("status") == "running"
                        for event in app.state.bus._history.get(sid, [])
                    )
                ):
                    break
                await asyncio.sleep(0.02)

            handle = app.state.turn_runner.handle(sid)
            assert handle is not None and handle.turn_id, "scheduled turn has no active reservation"

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

            # Confirm the model input while its worker is still cancellable.
            # This host agent emits no live parts; its transcript is sealed at
            # retirement, after the input commitment and running-status event.
            acknowledged = app.state.task_history.acknowledged_notifications(sid)
            [mail] = [
                app.state.task_history.mail_item(sid, identity)
                for identity in acknowledged
                if app.state.task_history.mail_item(sid, identity).kind == "schedule"
            ]
            payload = await app.state.task_history.io.run(
                app.state.task_history.read_content, mail.content
            )
            assert payload["text"] == "scheduled q"
            meta = payload["metadata"]
            assert meta.get("scheduled") is True
            assert meta.get("schedule_id", "").startswith("sched_")

            # Cancellable: a cancel reaches the registered task, and the
            # done-callback drops the registration.
            original = app.state.turn_runner.handle(sid)
            assert original is not None and original.turn_id
            await asyncio.wait_for(app.state.turn_runner.stop(sid), timeout=5.0)
            assert app.state.turn_runner.handle(sid) != original

            # Retirement must also retain exactly one attributed injection.
            response = await asyncio.to_thread(client.get, f"/v1/sessions/{sid}/messages")
            msgs = response.json()["messages"]
            wakes = [
                part
                for message in msgs
                for part in message["parts"]
                if part["type"] == "injection" and part.get("source") == "schedule"
            ]
            assert len(wakes) == 1
            assert "scheduled q" in wakes[0]["text"]
            assert not [m for m in msgs if m["role"] == "user"]
        finally:
            tick.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick

    asyncio.run_coroutine_threadsafe(drive(), app.state.mcp_app_loop).result(timeout=5.0)


def test_tick_scan_error_is_logged_not_swallowed(
    tmp_path: Path, caplog, request: pytest.FixtureRequest
) -> None:
    """An injected due-scan failure produces a structured warning."""

    app, _client, _sid = _make(tmp_path, _SlowAgent(sleep_s=0.1), request)

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

    asyncio.run_coroutine_threadsafe(drive(), app.state.mcp_app_loop).result(timeout=5.0)
    matching = [r for r in caplog.records if "schedule_due_scan_failed" in r.getMessage()]
    assert matching, "injected tick error was swallowed (no structured warning logged)"


def test_fire_error_is_logged_and_tick_survives(
    tmp_path: Path, caplog, monkeypatch, request: pytest.FixtureRequest
) -> None:
    """A single schedule that fails to fire is logged and does not kill the loop."""

    import clio_agent.gact.scheduler_runtime as scheduler_runtime

    app, _client, sid = _make(tmp_path, _SlowAgent(sleep_s=0.1), request)
    app.state.schedules.add(session_id=sid, cron="* * * * *", question="q")

    def _boom(*args, **kwargs):
        raise RuntimeError("boom: injected staging failure")

    monkeypatch.setattr(scheduler_runtime, "accept_session_input", _boom)

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

    asyncio.run_coroutine_threadsafe(drive(), app.state.mcp_app_loop).result(timeout=5.0)
    matching = [r for r in caplog.records if "schedule_fire_failed" in r.getMessage()]
    assert matching, "schedule firing error was swallowed (no structured warning logged)"
    assert any(sid in r.getMessage() for r in matching)


def test_corrupt_schedule_store_starts_empty_with_reason(tmp_path: Path, caplog) -> None:
    """Garbage JSON on disk logs schedule_store_corrupt and starts empty."""

    from clio_agent.gact.scheduler import ScheduleStore

    path = tmp_path / "schedules.json"
    path.write_text("{not valid json", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="clio_agent.gact.scheduler"):
        store = ScheduleStore(path=path)

    assert store.list() == []
    matching = [r for r in caplog.records if "schedule_store_corrupt" in r.getMessage()]
    assert matching, "corrupt schedule store was swallowed (no structured warning)"
    assert any(str(path) in r.getMessage() for r in matching)


def test_invalid_schedule_row_is_dropped_with_reason(tmp_path: Path, caplog) -> None:
    """A single malformed row is dropped and logged; valid rows survive."""

    import json

    from clio_agent.gact.scheduler import ScheduleStore

    path = tmp_path / "schedules.json"
    good = {
        "id": "sched_good0000000",
        "session_id": "sess_x",
        "cron": "* * * * *",
        "question": "q",
    }
    bad = {"session_id": "sess_y", "cron": "* * * * *"}  # missing "id"
    path.write_text(json.dumps({"schedules": [good, bad]}), encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="clio_agent.gact.scheduler"):
        store = ScheduleStore(path=path)

    ids = [s.id for s in store.list()]
    assert ids == ["sched_good0000000"], f"expected only the valid row, got {ids}"
    matching = [r for r in caplog.records if "schedule_row_invalid" in r.getMessage()]
    assert matching, "invalid schedule row was swallowed (no structured warning)"


def test_schedules_list_surfaces_cron_timezone(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """The schedules API states its cron timezone on the wire (P4.3 #1081: local, not UTC).

    Cron is now evaluated in each schedule's own local timezone (DST-correct); the
    envelope reports the server's default local zone and every row carries its ``timezone``.
    """

    from clio_agent.gact.scheduler import default_timezone_name

    _app, client, sid = _make(tmp_path, _SlowAgent(sleep_s=0.1), request)
    client.post(f"/v1/sessions/{sid}/schedules", json={"cron": "0 9 * * *", "question": "q"})
    body = client.get(f"/v1/sessions/{sid}/schedules").json()
    assert body["cron_timezone"] == default_timezone_name()
    assert body["schedules"][0]["timezone"] == default_timezone_name()


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
