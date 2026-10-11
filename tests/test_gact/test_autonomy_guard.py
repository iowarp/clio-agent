"""Unattended work cannot evade limits, pacing, cancellation or restart settlement."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import goal as goal_mod
from clio_agent.gact.autonomous_loop import _get_loop, run_loop_command, start_loop
from clio_agent.gact.autonomy_guard import (
    goal_redrive_is_current,
    reconcile_autonomous_controls,
)
from clio_agent.gact.goal import (
    GoalJudgement,
    _get_goal,
    _settle_goal_decision,
    arm_goal,
    clear_goal,
    dispatch_goal_at_finalize,
    parse_goal_command,
    run_goal_command,
)
from clio_agent.gact.loop_inbox import inbox_for
from clio_agent.gact.scheduler_runtime import _fire_schedule
from tests.test_gact.test_goal import _app, _session


@pytest.mark.parametrize("limit", ["time", "tokens", "iterations"])
def test_waiting_loop_limit_prevents_staging(tmp_path: Path, monkeypatch: Any, limit: str) -> None:
    """A limit crossed during the wait must prevent another tool/model turn."""
    app = _app(tmp_path)
    sid = _session(app)
    arm_goal(app, sid, condition="Finish the sweep")
    start_loop(app, sid, prompt="One checkpoint", interval_s=60, max_wallclock_s=1, max_tokens=20)
    loop = _get_loop(app, sid)
    schedule = app.state.schedules.get(loop["pending_schedule_id"])
    if limit == "time":
        loop["created_at"] = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()
    elif limit == "iterations":
        loop["iteration"] = loop["max_iters"]
    else:
        app.state.sessions.update(sid, add_tokens_input=21)
    app.state.sessions.update(sid, metadata_patch={"loop": loop})
    staged: list[str] = []
    from clio_agent.gact import app as app_mod

    monkeypatch.setattr(
        app_mod, "_turn_start_background_user_turn", lambda *a, **k: staged.append(sid)
    )
    _fire_schedule(app, schedule)
    assert staged == []
    assert app.state.schedules.list(session_id=sid) == []
    assert _get_loop(app, sid)["stop_reason"] == (
        "loop_max_iters" if limit == "iterations" else "loop_budget"
    )
    assert _get_goal(app, sid)["clear_reason"] == "goal_loop_stopped"


def test_unmet_goal_obeys_loop_pacing(tmp_path: Path, monkeypatch: Any) -> None:
    app = _app(tmp_path)
    sid = _session(app)
    arm_goal(app, sid, condition="Finish the sweep")
    start_loop(app, sid, prompt="One checkpoint", interval_s=60)

    async def unmet(*args: Any) -> GoalJudgement:
        return GoalJudgement(met=False, reason="There are checkpoints remaining")

    monkeypatch.setattr(goal_mod, "run_llm_judge", unmet)
    decision = asyncio.run(dispatch_goal_at_finalize(app, session_id=sid))
    assert decision is not None and decision.outcome == "redrive"
    assert _get_goal(app, sid)["active"] is True
    assert inbox_for(app, sid).drain() == []
    assert len(app.state.schedules.list(session_id=sid)) == 1


@pytest.mark.parametrize("paced", [False, True])
def test_goal_expiring_while_queued_prevents_another_turn(tmp_path: Path, paced: bool) -> None:
    app = _app(tmp_path)
    sid = _session(app)
    arm_goal(app, sid, condition="Done", max_wallclock_s=1)
    goal = _get_goal(app, sid)
    if paced:
        start_loop(app, sid, prompt="One checkpoint", interval_s=60)
        goal = _get_goal(app, sid)
    goal["created_at"] = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()
    app.state.sessions.update(sid, metadata_patch={"goal": goal})
    if paced:
        schedule = app.state.schedules.get(_get_loop(app, sid)["pending_schedule_id"])
        from clio_agent.gact.autonomy_guard import admit_loop_wakeup

        assert not admit_loop_wakeup(app, schedule)
        assert not app.state.schedules.list(session_id=sid)
        assert _get_loop(app, sid)["stop_reason"] == "loop_goal_capped"
    else:
        assert not goal_redrive_is_current(
            app, sid, {"goal_redrive": True, "goal_id": goal["goal_id"]}
        )
    assert _get_goal(app, sid)["clear_reason"] == "goal_budget"


def test_unpaced_goal_redrives_then_clear_invalidates_wakeup(tmp_path: Path) -> None:
    app = _app(tmp_path)
    sid = _session(app)
    arm_goal(app, sid, condition="Finish the sweep")
    _settle_goal_decision(app, sid, _get_goal(app, sid), GoalJudgement(met=False), "t", "tr")
    wakeup = inbox_for(app, sid).drain()[0]
    assert goal_redrive_is_current(app, sid, wakeup.metadata)
    clear_goal(app, sid)
    assert not goal_redrive_is_current(app, sid, wakeup.metadata)
    arm_goal(app, sid, condition="A different goal")
    assert not goal_redrive_is_current(app, sid, wakeup.metadata)


def test_clear_before_settlement_cannot_resurrect_goal(tmp_path: Path) -> None:
    """Recheck under the mutation lock, including the post-await executor window."""
    app = _app(tmp_path)
    sid = _session(app)
    arm_goal(app, sid, condition="Finish the sweep")
    stale = _get_goal(app, sid)
    clear_goal(app, sid)
    assert _settle_goal_decision(app, sid, stale, GoalJudgement(met=False), "t", "tr") is None
    assert not _get_goal(app, sid)["active"]
    assert inbox_for(app, sid).drain() == []


def test_stale_budget_check_cannot_clear_a_replacement_goal(tmp_path: Path) -> None:
    from clio_agent.gact.autonomy_guard import expire_goal_budget

    app = _app(tmp_path)
    sid = _session(app)
    arm_goal(app, sid, condition="Old", max_wallclock_s=1)
    stale = _get_goal(app, sid)
    stale["created_at"] = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()
    arm_goal(app, sid, condition="New")
    assert not expire_goal_budget(app, sid, stale)
    assert _get_goal(app, sid)["active"] and _get_goal(app, sid)["condition"] == "New"


def test_exhausted_goal_does_not_call_judge(tmp_path: Path, monkeypatch: Any) -> None:
    app = _app(tmp_path)
    sid = _session(app)
    arm_goal(app, sid, condition="Finish the sweep", max_tokens=1)
    app.state.sessions.update(sid, add_tokens_input=2)

    async def unexpected(*args: Any) -> GoalJudgement:
        pytest.fail("An exhausted goal must not spend another judge call")

    monkeypatch.setattr(goal_mod, "run_llm_judge", unexpected)
    result = asyncio.run(dispatch_goal_at_finalize(app, session_id=sid))
    assert result is not None and result.reason == "goal_budget"
    assert inbox_for(app, sid).drain() == []


def test_last_allowed_goal_continuation_can_succeed(tmp_path: Path, monkeypatch: Any) -> None:
    app = _app(tmp_path)
    sid = _session(app)
    arm_goal(app, sid, condition="Done", max_goal_iters=1)
    goal = _get_goal(app, sid)
    goal["iters_elapsed"] = 1
    app.state.sessions.update(sid, metadata_patch={"goal": goal})

    async def met(*args: Any) -> GoalJudgement:
        return GoalJudgement(met=True, reason="The last attempt succeeded")

    monkeypatch.setattr(goal_mod, "run_llm_judge", met)
    result = asyncio.run(dispatch_goal_at_finalize(app, session_id=sid))
    assert result is not None and result.reason == "goal_met"


def test_failed_judge_stops_instead_of_spending_more_turns(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app = _app(tmp_path)
    sid = _session(app)
    arm_goal(app, sid, condition="Finish the sweep")

    async def unavailable(*args: Any) -> GoalJudgement:
        return GoalJudgement(met=False, available=False)

    monkeypatch.setattr(goal_mod, "run_llm_judge", unavailable)
    result = asyncio.run(dispatch_goal_at_finalize(app, session_id=sid))
    assert result is not None and result.reason == "goal_judge_unavailable"
    assert not result.met
    assert inbox_for(app, sid).drain() == []


@pytest.mark.parametrize("status", ["idle", "running"])
def test_restart_preserves_only_durable_waiting_work(tmp_path: Path, status: str) -> None:
    app = _app(tmp_path)
    from clio_agent.gact.sessions import SessionStore

    app.state.sessions = SessionStore(path=tmp_path / "sessions.json")
    sid = _session(app)
    arm_goal(app, sid, condition="Finish the sweep")
    start_loop(app, sid, prompt="One checkpoint", interval_s=60)
    app.state.sessions.update(sid, status=status)
    # Use fresh stores: the test must not accidentally depend on the old inbox or process.
    from clio_agent.gact.scheduler import ScheduleStore

    reloaded = SimpleNamespace(
        state=SimpleNamespace(
            sessions=SessionStore(path=tmp_path / "sessions.json"),
            schedules=ScheduleStore(path=tmp_path / "schedules.json"),
            deferred_schedules=set(),
        )
    )
    assert reloaded.state.sessions.get(sid) is not None
    reconcile_autonomous_controls(reloaded)
    if status == "idle":
        assert _get_loop(reloaded, sid)["active"]
        assert _get_goal(reloaded, sid)["active"]
        assert len(reloaded.state.schedules.list(session_id=sid)) == 1
    else:
        assert _get_loop(reloaded, sid)["stop_reason"] == "loop_server_interrupted"
        assert _get_goal(reloaded, sid)["clear_reason"] == "goal_server_interrupted"
        assert reloaded.state.schedules.list(session_id=sid) == []


def test_restart_does_not_leave_unpaced_goal_active(tmp_path: Path) -> None:
    app = _app(tmp_path)
    sid = _session(app)
    arm_goal(app, sid, condition="Finish the sweep")
    reconcile_autonomous_controls(app)
    assert _get_goal(app, sid)["clear_reason"] == "goal_server_interrupted"


def test_normal_backend_boot_persists_schedule_without_explicit_path(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The production entry point omits sessions_path; its schedules must still persist."""
    from clio_agent.gact.app import build_app

    monkeypatch.setenv("CLIO_SESSIONS_PATH", str(tmp_path / "sessions.json"))
    app = build_app()
    sid = app.state.sessions.create(workspace_id="ws_default", title="Waiting loop").id
    start_loop(app, sid, prompt="One checkpoint", interval_s=60)
    pending = _get_loop(app, sid)["pending_schedule_id"]
    assert (tmp_path / "schedules.json").is_file()
    restarted = build_app()
    reconcile_autonomous_controls(restarted)
    assert restarted.state.schedules.get(pending) is not None
    assert _get_loop(restarted, sid)["active"] is True


def test_composer_limits_and_user_stop_are_deterministic(tmp_path: Path) -> None:
    app = _app(tmp_path)
    sid = _session(app)
    condition, bounds, _ = parse_goal_command(
        {"input": "--max-iters=72 --max-hours=72 --max-tokens=1000000 Save final report"}
    )
    assert condition == "Save final report"
    assert bounds == {"max_goal_iters": 72, "max_wallclock_s": 259200, "max_tokens": 1000000}
    run_goal_command(app, sid, {"input": "--max-iters=72 Save final report"})
    run_loop_command(app, sid, {"input": "1h --max-iters=72 --max-hours=72 One batch"})
    loop = _get_loop(app, sid)
    assert loop["interval_s"] == 3600 and loop["max_iters"] == 72
    assert loop["max_wallclock_s"] == 259200 and loop["prompt"] == "One batch"
    run_loop_command(app, sid, {"input": "stop"})
    assert _get_loop(app, sid)["stop_reason"] == "loop_user_cancelled"
    assert not _get_goal(app, sid)["active"]
    assert app.state.schedules.list(session_id=sid) == []


@pytest.mark.parametrize(
    "option", ["--max-iters=-1", "--max-iters=2.5", "--max-hours=nan", "--max-hours=inf", "--bad=1"]
)
def test_invalid_options_leave_prior_work_unchanged(tmp_path: Path, option: str) -> None:
    app = _app(tmp_path)
    sid = _session(app)
    arm_goal(app, sid, condition="Original")
    start_loop(app, sid, prompt="Original")
    goal, loop = _get_goal(app, sid), _get_loop(app, sid)
    assert "rejected" in run_goal_command(app, sid, {"input": option + " New goal"})
    assert "rejected" in run_loop_command(app, sid, {"input": "1m " + option + " New loop"})
    assert _get_goal(app, sid) == goal and _get_loop(app, sid) == loop


def test_judge_timeout_cancels_pending_call(tmp_path: Path, monkeypatch: Any) -> None:
    import dspy

    app = _app(tmp_path)
    sid = _session(app)
    cancelled: list[bool] = []

    async def pending(**kwargs: Any) -> Any:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    monkeypatch.setattr(goal_mod, "DEFAULT_JUDGE_TIMEOUT_S", 0.01)
    monkeypatch.setattr(goal_mod, "_judge_route", lambda app: (None, None))
    monkeypatch.setattr(dspy, "Predict", lambda *args: SimpleNamespace(acall=pending))
    verdict = asyncio.run(goal_mod.run_llm_judge(app, sid, {"condition": "Done"}))
    assert not verdict.available and not verdict.met
    assert cancelled == [True]


def test_goal_cap_stops_paired_loop_at_real_finalize_seam(tmp_path: Path, monkeypatch: Any) -> None:
    from clio_agent.gact import turn_finalize_goal

    app = _app(tmp_path)
    sid = _session(app)
    arm_goal(app, sid, condition="Done", max_tokens=1)
    start_loop(app, sid, prompt="One checkpoint")
    app.state.sessions.update(sid, add_tokens_input=2)
    monkeypatch.setattr(turn_finalize_goal, "finalize_turn", lambda *args, **kwargs: None)
    state = SimpleNamespace(app=app, sid=sid, turn_id="t", trace_id="tr")
    asyncio.run(
        turn_finalize_goal.finalize_turn_async(
            state,
            None,
            drain_observed_tool_calls=lambda: [],
            update_retry_attempt=lambda *args: None,
        )
    )
    assert _get_loop(app, sid)["stop_reason"] == "loop_goal_capped"
    assert not app.state.schedules.list(session_id=sid)


def test_judge_retains_precise_artifact_facts_and_recent_chronology(tmp_path: Path) -> None:
    """A generic UI title must not hide the filenames that prove the user's goal."""
    app = _app(tmp_path)
    sid = _session(app)
    app.state.messages[sid] = [
        SimpleNamespace(
            created_at="2026-10-11T02:00:00",
            role="assistant",
            parts=[
                SimpleNamespace(
                    type="tool_result",
                    tool_name="create_artifact",
                    is_error=False,
                    presentation={"summary": "Created version 1"},
                    structured_content={"message": "created 1 artifact: paced-2.md"},
                )
            ],
        ),
        SimpleNamespace(
            created_at="2026-10-11T01:00:00",
            role="user",
            parts=[SimpleNamespace(type="text", text="An older instruction")],
        ),
    ]
    recent = goal_mod._build_transcript(app, sid, max_messages=1)
    assert "paced-2.md" in recent and "older instruction" not in recent
    full = goal_mod._build_transcript(app, sid)
    assert full.index("older instruction") < full.index("paced-2.md")


@pytest.mark.parametrize("status", ["waiting_user", "waiting_permission"])
def test_wakeup_waits_for_human_input(tmp_path: Path, monkeypatch: Any, status: str) -> None:
    app = _app(tmp_path)
    sid = _session(app)
    start_loop(app, sid, prompt="One checkpoint", interval_s=60)
    app.state.sessions.update(sid, status=status)
    app.state.turn_runner = SimpleNamespace(busy=lambda sid: False)
    loop = _get_loop(app, sid)
    schedule = app.state.schedules.get(loop["pending_schedule_id"])
    from clio_agent.gact import app as app_mod

    staged: list[bool] = []
    monkeypatch.setattr(
        app_mod, "_turn_start_background_user_turn", lambda *args, **kwargs: staged.append(True)
    )
    _fire_schedule(app, schedule)
    assert not staged
    assert schedule.id in app.state.deferred_schedules
    assert app.state.schedules.get(schedule.id) is not None


def test_exhausted_schedule_retries_settle_loop_and_goal(tmp_path: Path, monkeypatch: Any) -> None:
    from clio_agent.gact import app as app_mod
    from clio_agent.gact.scheduler_runtime import _fire_one

    app = _app(tmp_path)
    sid = _session(app)
    arm_goal(app, sid, condition="Done")
    start_loop(app, sid, prompt="One checkpoint")
    app.state.turn_runner = SimpleNamespace(busy=lambda sid: False)
    schedule = app.state.schedules.get(_get_loop(app, sid)["pending_schedule_id"])

    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Staging unavailable")

    monkeypatch.setattr(app_mod, "_turn_start_background_user_turn", fail)
    monkeypatch.setenv("CLIO_SCHEDULER_MAX_RETRIES", "1")
    _fire_one(app, schedule)
    _fire_one(app, schedule)
    assert _get_loop(app, sid)["stop_reason"] == "loop_schedule_failed"
    assert _get_goal(app, sid)["clear_reason"] == "goal_loop_stopped"
    assert not app.state.schedules.list(session_id=sid)


def test_startup_storage_failure_retries_before_firing(tmp_path: Path, monkeypatch: Any) -> None:
    """A transient startup read failure must neither fire unsafe work nor kill the scheduler."""
    from clio_agent.gact import scheduler_runtime

    app = _app(tmp_path)
    attempts: list[str] = []

    def reconcile(app: Any) -> None:
        attempts.append("reconcile")
        if len(attempts) == 1:
            raise OSError("Temporary storage outage")

    def tick(app: Any) -> None:
        attempts.append("tick")
        raise asyncio.CancelledError

    async def sleep(delay: float) -> None:
        assert delay >= 0.5

    monkeypatch.setattr(scheduler_runtime, "reconcile_autonomous_controls", reconcile)
    monkeypatch.setattr(scheduler_runtime, "_scheduler_tick_once", tick)
    monkeypatch.setattr(scheduler_runtime.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scheduler_runtime._scheduler_tick(app))
    assert attempts == ["reconcile", "reconcile", "tick"]


@pytest.mark.parametrize("starting", [True, False])
def test_overdue_wakeup_waits_for_runtime_readiness(
    tmp_path: Path, monkeypatch: Any, starting: bool
) -> None:
    from clio_agent.gact import app as app_mod

    app = _app(tmp_path)
    sid = _session(app)
    start_loop(app, sid, prompt="One checkpoint", interval_s=60)
    schedule = app.state.schedules.get(_get_loop(app, sid)["pending_schedule_id"])
    app.state.agent = None if starting else object()
    app.state.lm_config_status = {"state": "ready" if starting else "configuring"}
    app.state.turn_runner = SimpleNamespace(busy=lambda sid: False)
    staged: list[str] = []
    monkeypatch.setattr(
        app_mod, "_turn_start_background_user_turn", lambda *a, **k: staged.append(sid)
    )
    _fire_schedule(app, schedule)
    assert not staged and schedule.id in app.state.deferred_schedules
    assert app.state.schedules.get(schedule.id).enabled
    app.state.agent = object()
    app.state.lm_config_status = {"state": "ready"}
    _fire_schedule(app, schedule)
    assert staged == [sid]
    assert app.state.schedules.get(schedule.id) is None


def test_scheduler_restores_a_sessions_own_model(tmp_path: Path, monkeypatch: Any) -> None:
    from clio_agent.gact import session_host_agent
    from clio_agent.gact.autonomy_guard import prepare_scheduled_host

    app = _app(tmp_path)
    sid = _session(app)
    app.state.sessions.update(sid, model={"provider_id": "codex", "model_id": "gpt-6-luna"})
    start_loop(app, sid, prompt="One checkpoint")
    app.state.agent = None
    app.state.agent_construction_task = SimpleNamespace(done=lambda: False)
    built: list[str] = []

    async def build(app: Any, session_id: str, request: Any) -> None:
        ref = session_host_agent.selected_ref(app, session_id, request)
        assert ref.provider_id == "codex" and ref.model_id == "gpt-6-luna"
        built.append(session_id)
        app.state.agent = object()

    monkeypatch.setattr(session_host_agent, "ensure_host_agent", build)
    asyncio.run(prepare_scheduled_host(app))
    assert not built
    app.state.agent_construction_task = None
    app.state.lm_config_status = {"state": "configuring"}
    asyncio.run(prepare_scheduled_host(app))
    assert not built
    app.state.lm_config_status = {"state": "ready"}
    asyncio.run(prepare_scheduled_host(app))
    asyncio.run(prepare_scheduled_host(app))
    assert built == [sid]
