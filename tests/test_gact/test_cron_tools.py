"""P4.3 (#1081): the model-callable cron triad + /cron command + cancel-both.

Drives cron_create/cron_list/cron_delete through the same active-app/session context the
react loop establishes (:mod:`clio_agent.gact.context`), asserting the server-generated
result-only id, the read-back, and cancel-both (deleting also clears any daemon deferred
entry — no orphan tick). Also asserts the built-in /cron command row exists.

Each test body runs inside a fresh ``contextvars.copy_context()`` so the (reset-less)
``set_app``/``set_session_id`` bindings never leak between tests or into other suites.
"""

from __future__ import annotations

import contextvars
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

from clio_agent.gact import context as _ctx
from clio_agent.gact.cron_tools import (
    build_cron_create_tool,
    build_cron_delete_tool,
    build_cron_list_tool,
)
from clio_agent.gact.scheduler import CronError, ScheduleStore


def _app(tmp_path: Path) -> SimpleNamespace:
    """A minimal app carrying only what the cron tools touch."""

    state = SimpleNamespace(
        schedules=ScheduleStore(path=tmp_path / "schedules.json"),
        deferred_schedules=set(),
    )
    return SimpleNamespace(state=state)


def _in_ctx(fn: Callable[[], None]) -> None:
    """Run ``fn`` in an isolated copy of the current context (no binding leak)."""

    contextvars.copy_context().run(fn)


def _bind(app: object, sid: str) -> None:
    _ctx.set_app(app)
    _ctx.set_session_id(sid)


def test_cron_create_returns_result_only_id_list_reads_back(tmp_path: Path) -> None:
    """create returns a server-generated id in the RESULT; list reads it back."""

    def body() -> None:
        app = _app(tmp_path)
        _bind(app, "sess_1")
        result = build_cron_create_tool().func(cron="0 9 * * *", prompt="daily standup")
        assert result["schedule_id"].startswith("sched_")
        assert result["recurring"] is True
        assert result["next_fire_at"]
        assert result["timezone"]

        listed = build_cron_list_tool().func()
        assert len(listed) == 1
        assert listed[0]["id"] == result["schedule_id"]
        assert listed[0]["prompt"] == "daily standup"
        assert listed[0]["cron"] == "0 9 * * *"

    _in_ctx(body)


def test_cron_delete_cancel_both_no_orphan(tmp_path: Path) -> None:
    """delete removes the store row AND clears a queued deferred entry (cancel-both)."""

    def body() -> None:
        app = _app(tmp_path)
        _bind(app, "sess_1")
        result = build_cron_create_tool().func(cron="0 9 * * *", prompt="q")
        sid = result["schedule_id"]
        # Simulate the daemon having deferred this schedule (busy session).
        app.state.deferred_schedules.add(sid)

        deleted = build_cron_delete_tool().func(schedule_id=sid)
        assert deleted is True
        # Store row gone AND the deferred entry cleared — no orphan tick can resurrect it.
        assert app.state.schedules.get(sid) is None
        assert sid not in app.state.deferred_schedules
        # Idempotent.
        assert build_cron_delete_tool().func(schedule_id=sid) is False

    _in_ctx(body)


def test_cron_create_one_shot_recurring_false(tmp_path: Path) -> None:
    """recurring=False arms a one-shot (auto-deletes after firing)."""

    def body() -> None:
        app = _app(tmp_path)
        _bind(app, "s")
        result = build_cron_create_tool().func(cron="0 9 * * *", prompt="q", recurring=False)
        assert result["recurring"] is False

    _in_ctx(body)


def test_cron_create_bad_cron_is_typed_error(tmp_path: Path) -> None:
    """A malformed cron surfaces a typed CronError the model can repair against."""

    def body() -> None:
        app = _app(tmp_path)
        _bind(app, "s")
        with pytest.raises(CronError) as ei:
            build_cron_create_tool().func(cron="99 * * * *", prompt="q")
        assert ei.value.reason == "invalid_cron"

    _in_ctx(body)


def test_cron_tools_require_active_session() -> None:
    """Outside a live turn (no app/session bound) the tools raise no_active_session."""

    def body() -> None:
        _ctx.set_app(None)
        _ctx.set_session_id("")
        with pytest.raises(CronError) as ei:
            build_cron_list_tool().func()
        assert ei.value.reason == "no_active_session"

    _in_ctx(body)


def test_cron_command_row_exists() -> None:
    """The built-in /cron command row is present and available."""

    from clio_agent.gact.runtime.commands import BACKEND_COMMANDS

    rows = {row["id"]: row for row in BACKEND_COMMANDS}
    assert "/cron" in rows
    cron_row = rows["/cron"]
    assert cron_row["status"] == "available"
    assert cron_row["enabled"] is True
    assert "/schedule" in cron_row.get("aliases", [])
