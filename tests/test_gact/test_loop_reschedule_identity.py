"""Repeated tool calls in one turn replace one wakeup without spending two iterations."""

from __future__ import annotations

import contextvars
from pathlib import Path

import pytest

from clio_agent.gact import context
from clio_agent.gact.autonomous_loop import loop_wakeup_impl, start_loop
from tests.test_gact.test_loop import _app, _bind, _loop_state, _session


def test_same_turn_replaces_one_wakeup_without_double_counting(tmp_path: Path) -> None:
    """A second tool call changes the pending instruction, rather than burning a turn."""

    def check() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        start_loop(app, sid, prompt="initial", max_iters=2)
        context.set_turn_id("first-turn")
        assert loop_wakeup_impl(60, "first instruction")["stopped"] is False
        first = _loop_state(app, sid)["pending_schedule_id"]
        assert loop_wakeup_impl(120, "revised instruction")["stopped"] is False
        state = _loop_state(app, sid)
        assert state["iteration"] == 1
        assert state["pending_schedule_id"] != first
        schedules = app.state.schedules.list(session_id=sid)
        assert len(schedules) == 1 and schedules[0].question == "revised instruction"
        context.set_turn_id("second-turn")
        assert loop_wakeup_impl(60, "another turn")["stopped"] is True
        assert _loop_state(app, sid)["stop_reason"] == "loop_max_iters"
        assert app.state.schedules.list(session_id=sid) == []

    contextvars.copy_context().run(check)


def test_self_initiated_loop_counts_repeated_call_once(tmp_path: Path) -> None:
    """Starting from a tool and revising that wake in the same turn is one decision."""

    def check() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        context.set_turn_id("initiating-turn")
        loop_wakeup_impl(60, "first")
        loop_wakeup_impl(120, "second")
        assert _loop_state(app, sid)["iteration"] == 0
        assert len(app.state.schedules.list(session_id=sid)) == 1

    contextvars.copy_context().run(check)


@pytest.mark.parametrize("budget", ["tokens", "cost"])
def test_repeated_same_turn_still_checks_spending(tmp_path: Path, budget: str) -> None:
    """Idempotent iteration counting cannot bypass the independent token/cost bound."""

    def check() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        start_loop(app, sid, prompt="initial", max_tokens=1, max_usd=0.01)
        context.set_turn_id("same-turn")
        assert loop_wakeup_impl(60, "first")["stopped"] is False
        if budget == "tokens":
            app.state.sessions.update(sid, add_tokens_input=2)
        else:
            app.state.sessions.update(sid, add_cost_usd=0.02)
        assert loop_wakeup_impl(60, "second")["stopped"] is True
        assert _loop_state(app, sid)["stop_reason"] == "loop_budget"

    contextvars.copy_context().run(check)
