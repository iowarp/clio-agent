"""Unattended work cannot evade limits, pacing, cancellation or restart settlement."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import MailItem
from clio_agent.gact import goal as goal_mod
from clio_agent.gact.autonomous_loop import _get_loop, run_loop_command, start_loop
from clio_agent.gact.autonomy_guard import reconcile_autonomous_controls
from clio_agent.gact.conversations import Conversations
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
from clio_agent.gact.scheduler_runtime import _fire_schedule
from clio_agent.gact.task_history import TaskHistory
from tests.test_arc.conftest import core_arc as core_arc
from tests.test_arc.test_conversation_owner_contracts import create_environment
from tests.test_gact.test_goal import _app, _session
from tests.test_gact.test_goal import _pending_inputs as pending_mail

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.turn_state import TurnState


@pytest_asyncio.fixture
async def autonomous_session(
    arc: ARCMemory, tmp_path: Path
) -> AsyncIterator[tuple[Any, Conversations, str]]:
    """Use actual mailbox/admission owners while vetoing model execution in policy tests."""
    from clio_agent.gact.scheduler import ScheduleStore

    app, owner, root = await create_environment(arc, tmp_path)
    app.state.schedules = ScheduleStore(path=tmp_path / "schedules.json")
    app.state.deferred_schedules = set()
    owner.execution_gate = lambda _sid: "Policy assertions do not execute a model"
    try:
        yield app, owner, root.owner.session_id
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


@pytest.mark.parametrize("limit", ["time", "tokens", "iterations"])
def test_waiting_loop_limit_prevents_staging(tmp_path: Path, limit: str, arc: ARCMemory) -> None:
    """A limit crossed during the wait must prevent another tool/model turn."""
    app = _app(tmp_path, arc)
    sid = _session(app)
    arm_goal(app, sid, condition="Finish the sweep")
    start_loop(
        app,
        sid,
        prompt="One checkpoint",
        interval_s=60,
        max_iters=2,
        max_wallclock_s=1,
        max_tokens=20,
    )
    loop = _get_loop(app, sid)
    schedule = app.state.schedules.get(loop["pending_schedule_id"])
    if limit == "time":
        loop["created_at"] = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()
    elif limit == "iterations":
        loop["iteration"] = loop["max_iters"]
    else:
        app.state.sessions.update(sid, add_tokens_input=21)
    app.state.sessions.update(sid, metadata_patch={"loop": loop})
    _fire_schedule(cast("FastAPI", app), schedule)
    assert pending_mail(app, sid) == []
    assert app.state.schedules.list(session_id=sid) == []
    assert _get_loop(app, sid)["stop_reason"] == (
        "loop_max_iters" if limit == "iterations" else "loop_budget"
    )
    assert _get_goal(app, sid)["active"]


def test_unmet_goal_continues_independently_of_loop_pacing(
    tmp_path: Path, monkeypatch: Any, arc: ARCMemory
) -> None:
    app = _app(tmp_path, arc)
    sid = _session(app)
    arm_goal(app, sid, condition="Finish the sweep")
    start_loop(app, sid, prompt="One checkpoint", interval_s=60)

    async def unmet(*args: Any) -> GoalJudgement:
        return GoalJudgement(met=False, reason="There are checkpoints remaining")

    monkeypatch.setattr(goal_mod, "run_llm_judge", unmet)
    decision = asyncio.run(dispatch_goal_at_finalize(app, session_id=sid))
    assert decision is not None and decision.outcome == "redrive"
    assert _get_goal(app, sid)["active"] is True
    assert len(pending_mail(app, sid)) == 1
    assert len(app.state.schedules.list(session_id=sid)) == 1


@pytest.mark.parametrize("paced", [False, True])
@pytest.mark.parametrize("limit", ["time", "tokens"])
@pytest.mark.asyncio
async def test_goal_expiring_while_queued_prevents_another_turn(
    autonomous_session: tuple[Any, Conversations, str], paced: bool, limit: str
) -> None:
    from clio_agent.gact.autonomy_guard import admit_loop_wakeup
    from clio_agent.gact.goal_control import maintain_goal_controls

    app, owner, sid = autonomous_session

    def exercise() -> None:
        arm_goal(
            app,
            sid,
            condition="Done",
            max_wallclock_s=1 if limit == "time" else 0,
            max_tokens=1 if limit == "tokens" else 0,
        )
        goal = _get_goal(app, sid)
        _settle_goal_decision(app, sid, goal, GoalJudgement(met=False), "t", "tr")
        [wakeup] = owner.history.pending_mail(sid)
        assert owner.eligible(sid, wakeup)
        if paced:
            start_loop(app, sid, prompt="One checkpoint", interval_s=60)
        goal = _get_goal(app, sid)
        if limit == "time":
            goal["created_at"] = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()
            app.state.sessions.update(sid, metadata_patch={"goal": goal})
        else:
            app.state.sessions.update(sid, add_tokens_input=2)
        if paced:
            schedule = app.state.schedules.get(_get_loop(app, sid)["pending_schedule_id"])
            assert admit_loop_wakeup(app, schedule)
            assert _get_loop(app, sid)["active"]
        # Admission must reject it before the periodic maintenance has run.
        assert _get_goal(app, sid)["active"]
        assert not owner.eligible(sid, wakeup)
        assert owner.mail.stage(owner.caller(sid)).items == ()
        assert owner.history.pending_mail(sid) == (wakeup,)
        assert not owner.history.acknowledged_notifications(sid)
        feedback = owner.history.record_mail(
            MailItem(
                id="human-feedback",
                recipient_session=sid,
                sender_human="human",
                kind="human",
                content=owner.history.write_content(sid, {"text": "Please inspect the result"}),
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        assert owner.eligible(sid, feedback)
        assert owner.mail.stage(owner.caller(sid)).items == (feedback,)

    await owner.history.io.run(exercise)
    await maintain_goal_controls(app)
    assert _get_goal(app, sid)["clear_reason"] == "goal_budget"


@pytest.mark.asyncio
async def test_unpaced_goal_redrives_then_clear_invalidates_wakeup(
    autonomous_session: tuple[Any, Conversations, str],
) -> None:
    app, owner, sid = autonomous_session

    def exercise() -> None:
        arm_goal(app, sid, condition="Finish the sweep")
        _settle_goal_decision(app, sid, _get_goal(app, sid), GoalJudgement(met=False), "t", "tr")
        [wakeup] = owner.history.pending_mail(sid)
        assert owner.eligible(sid, wakeup)
        clear_goal(app, sid)
        assert not owner.eligible(sid, wakeup)
        arm_goal(app, sid, condition="A different goal")
        assert not owner.eligible(sid, wakeup)
        assert owner.history.pending_mail(sid) == (wakeup,)
        assert not owner.history.acknowledged_notifications(sid)

    await owner.history.io.run(exercise)


@pytest.mark.asyncio
async def test_unbounded_goal_input_remains_eligible_after_accumulated_work(
    autonomous_session: tuple[Any, Conversations, str],
) -> None:
    app, owner, sid = autonomous_session

    def exercise() -> None:
        arm_goal(app, sid, condition="Continue the study", max_wallclock_s=0, max_tokens=0)
        goal = _get_goal(app, sid)
        goal["created_at"] = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
        app.state.sessions.update(sid, metadata_patch={"goal": goal}, add_tokens_input=1_000_000)
        _settle_goal_decision(app, sid, goal, GoalJudgement(met=False), "t", "tr")
        [item] = owner.history.pending_mail(sid)
        before = _get_goal(app, sid)
        assert before["max_wallclock_s"] == before["max_tokens"] == 0
        assert owner.eligible(sid, item)
        assert owner.mail.stage(owner.caller(sid)).items == (item,)
        assert _get_goal(app, sid) == before
        assert not owner.history.acknowledged_notifications(sid)

    await owner.history.io.run(exercise)


@pytest.mark.parametrize("one_shot", [False, True])
@pytest.mark.asyncio
async def test_schedule_cancellation_withdraws_only_its_pending_occurrence(
    autonomous_session: tuple[Any, Conversations, str],
    monkeypatch: pytest.MonkeyPatch,
    one_shot: bool,
) -> None:
    from clio_agent.gact.cron_tools import cancel_schedule
    from clio_agent.gact.scheduler import ScheduleStore
    from clio_agent.gact.session_input import accept_session_input

    app, owner, sid = autonomous_session
    original = ScheduleStore.mark_fired

    def exercise() -> None:
        schedule = app.state.schedules.create(
            session_id=sid,
            question="Inspect the sample",
            cron="" if one_shot else "0 1 * * *",
            delay_s=60 if one_shot else 0,
        )
        unrelated = accept_session_input(
            app, sid, "Retain unrelated work", steer_message_id="unrelated"
        )
        seen: list[MailItem] = []

        def cancel_before_advancement(
            store: ScheduleStore, identity: str, *, now: datetime | None = None
        ) -> None:
            if store is app.state.schedules and identity == schedule.id:
                [item] = [row for row in owner.history.pending_mail(sid) if row.kind == "schedule"]
                seen.append(item)
                assert cancel_schedule(app, identity)
            original(store, identity, now=now)

        # The request arrives after durable acceptance but before timer retirement.
        with monkeypatch.context() as patch:
            patch.setattr(ScheduleStore, "mark_fired", cancel_before_advancement)
            _fire_schedule(app, schedule)
        [item] = seen
        assert app.state.schedules.get(schedule.id) is None
        assert owner.history.mail_withdrawn(sid, item.id)
        assert owner.history.mail_item(sid, item.id) == item
        assert [row.id for row in owner.history.pending_mail(sid)] == [unrelated]
        assert item not in owner.mail.stage(owner.caller(sid)).items
        assert not owner.history.acknowledged_notifications(sid)

    await owner.history.io.run(exercise)


@pytest.mark.parametrize("failure_phase", ["read", "write"])
@pytest.mark.asyncio
async def test_schedule_cancellation_recording_failure_keeps_future_and_queued_work_held(
    autonomous_session: tuple[Any, Conversations, str],
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
) -> None:
    from clio_agent.gact.cron_tools import cancel_schedule
    from clio_agent.gact.scheduler import ScheduleStore

    app, owner, sid = autonomous_session
    mark_fired, read_content, record = (
        ScheduleStore.mark_fired,
        TaskHistory.read_content,
        TaskHistory._record,
    )

    def exercise() -> None:
        schedule = app.state.schedules.create(session_id=sid, question="Inspect", delay_s=60)
        seen: list[MailItem] = []

        def cancel_before_advancement(
            store: ScheduleStore, identity: str, *, now: datetime | None = None
        ) -> None:
            if store is app.state.schedules and identity == schedule.id:
                [item] = owner.history.pending_mail(sid)
                seen.append(item)

                def fail_read(history: TaskHistory, reference: Any) -> Any:
                    if history is owner.history and reference == item.content:
                        raise OSError("Core cancellation read unavailable")
                    return read_content(history, reference)

                def fail_record(
                    history: TaskHistory,
                    event: str,
                    session: str,
                    observation: str,
                    payload: dict[str, Any],
                ) -> None:
                    if history is owner.history and event == "session.mail.withdrawn":
                        raise OSError("Core cancellation write unavailable")
                    record(history, event, session, observation, payload)

                with monkeypatch.context() as fault:
                    if failure_phase == "read":
                        fault.setattr(TaskHistory, "read_content", fail_read)
                    else:
                        fault.setattr(TaskHistory, "_record", fail_record)
                    with pytest.raises(OSError, match="Core cancellation"):
                        cancel_schedule(app, identity)
            # The scheduler's concurrent advancement cannot remove the human hold.
            mark_fired(store, identity, now=now)

        with monkeypatch.context() as patch:
            patch.setattr(ScheduleStore, "mark_fired", cancel_before_advancement)
            _fire_schedule(app, schedule)
        [item] = seen
        retained = app.state.schedules.get(schedule.id)
        assert retained is not None and not retained.enabled
        owner.history.restore()
        assert owner.history.pending_mail(sid) == (item,)
        assert not owner.history.mail_withdrawn(sid, item.id)
        assert not owner.eligible(sid, item)
        assert owner.mail.stage(owner.caller(sid)).items == ()
        assert not owner.history.acknowledged_notifications(sid)
        assert cancel_schedule(app, schedule.id)
        assert app.state.schedules.get(schedule.id) is None
        assert owner.history.mail_withdrawn(sid, item.id)
        assert owner.history.pending_mail(sid) == ()

    await owner.history.io.run(exercise)


def test_clear_before_settlement_cannot_resurrect_goal(tmp_path: Path, arc: ARCMemory) -> None:
    """Recheck under the mutation lock, including the post-await executor window."""
    app = _app(tmp_path, arc)
    sid = _session(app)
    arm_goal(app, sid, condition="Finish the sweep")
    stale = _get_goal(app, sid)
    clear_goal(app, sid)
    assert _settle_goal_decision(app, sid, stale, GoalJudgement(met=False), "t", "tr") is None
    assert not _get_goal(app, sid)["active"]
    assert pending_mail(app, sid) == []


def test_stale_budget_check_cannot_clear_a_replacement_goal(tmp_path: Path, arc: ARCMemory) -> None:
    from clio_agent.gact.autonomy_guard import expire_goal_budget

    app = _app(tmp_path, arc)
    sid = _session(app)
    arm_goal(app, sid, condition="Old", max_wallclock_s=1)
    stale = _get_goal(app, sid)
    stale["created_at"] = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()
    arm_goal(app, sid, condition="New")
    assert not expire_goal_budget(app, sid, stale)
    assert _get_goal(app, sid)["active"] and _get_goal(app, sid)["condition"] == "New"


def test_exhausted_goal_does_not_call_judge(
    tmp_path: Path, monkeypatch: Any, arc: ARCMemory
) -> None:
    app = _app(tmp_path, arc)
    sid = _session(app)
    arm_goal(app, sid, condition="Finish the sweep", max_tokens=1)
    app.state.sessions.update(sid, add_tokens_input=2)

    async def unexpected(*args: Any) -> GoalJudgement:
        pytest.fail("An exhausted goal must not spend another judge call")

    monkeypatch.setattr(goal_mod, "run_llm_judge", unexpected)
    result = asyncio.run(dispatch_goal_at_finalize(app, session_id=sid))
    assert result is not None and result.reason == "goal_budget"
    assert pending_mail(app, sid) == []


def test_last_allowed_goal_continuation_can_succeed(
    tmp_path: Path, monkeypatch: Any, arc: ARCMemory
) -> None:
    app = _app(tmp_path, arc)
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
    tmp_path: Path, monkeypatch: Any, arc: ARCMemory
) -> None:
    app = _app(tmp_path, arc)
    sid = _session(app)
    arm_goal(app, sid, condition="Finish the sweep")

    async def unavailable(*args: Any) -> GoalJudgement:
        return GoalJudgement(met=False, available=False)

    monkeypatch.setattr(goal_mod, "run_llm_judge", unavailable)
    result = asyncio.run(dispatch_goal_at_finalize(app, session_id=sid))
    assert result is not None and result.reason == "goal_judge_unavailable"
    assert not result.met
    assert pending_mail(app, sid) == []


@pytest.mark.parametrize("status", ["idle", "running"])
def test_restart_preserves_only_durable_waiting_work(
    tmp_path: Path, status: str, arc: ARCMemory
) -> None:
    app = _app(tmp_path, arc)
    from clio_agent.gact.sessions import SessionStore

    app.state.sessions = SessionStore(path=tmp_path / "sessions.json", store=arc._store)
    sid = _session(app)
    arm_goal(app, sid, condition="Finish the sweep")
    start_loop(app, sid, prompt="One checkpoint", interval_s=60)
    app.state.sessions.update(sid, status=status)
    # Use fresh stores: the test must not accidentally depend on the old inbox or process.
    from clio_agent.gact.scheduler import ScheduleStore

    reloaded = SimpleNamespace(
        state=SimpleNamespace(
            sessions=SessionStore(path=tmp_path / "sessions.json", store=arc._store),
            schedules=ScheduleStore(path=tmp_path / "schedules.json"),
            deferred_schedules=set(),
            task_history=TaskHistory(arc),
        )
    )
    reloaded.state.task_history.restore()
    assert reloaded.state.sessions.get(sid) is not None
    reconcile_autonomous_controls(reloaded)
    if status == "idle":
        assert _get_loop(reloaded, sid)["active"]
        assert _get_goal(reloaded, sid)["active"]
        assert len(reloaded.state.schedules.list(session_id=sid)) == 1
    else:
        assert _get_loop(reloaded, sid)["stop_reason"] == "loop_server_interrupted"
        assert _get_goal(reloaded, sid)["phase"] == "recovering"
        assert reloaded.state.schedules.list(session_id=sid) == []


def test_restart_retains_unpaced_goal_without_manufacturing_a_wakeup(
    tmp_path: Path, arc: ARCMemory
) -> None:
    app = _app(tmp_path, arc)
    sid = _session(app)
    arm_goal(app, sid, condition="Finish the sweep")
    reconcile_autonomous_controls(app)
    assert _get_goal(app, sid)["phase"] == "recovering"
    assert _get_goal(app, sid)["active"]
    assert pending_mail(app, sid) == []


def test_normal_backend_boot_persists_schedule_without_explicit_path(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The production entry point omits sessions_path; its schedules must still persist."""
    from clio_agent.gact.app import build_app

    monkeypatch.setenv("CLIO_SESSIONS_PATH", str(tmp_path / "sessions.json"))
    app = build_app()
    with TestClient(app):
        sid = app.state.sessions.create(workspace_id="ws_default", title="Waiting loop").id
        start_loop(app, sid, prompt="One checkpoint", interval_s=60)
        pending = _get_loop(app, sid)["pending_schedule_id"]
        assert (tmp_path / "schedules.json").is_file()
    restarted = build_app()
    with TestClient(restarted):
        reconcile_autonomous_controls(restarted)
        assert restarted.state.schedules.get(pending) is not None
        assert _get_loop(restarted, sid)["active"] is True


def test_composer_limits_and_user_stop_are_deterministic(tmp_path: Path, arc: ARCMemory) -> None:
    app = _app(tmp_path, arc)
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
    assert _get_goal(app, sid)["active"]
    assert app.state.schedules.list(session_id=sid) == []


@pytest.mark.parametrize(
    "option", ["--max-iters=-1", "--max-iters=2.5", "--max-hours=nan", "--max-hours=inf", "--bad=1"]
)
def test_invalid_options_leave_prior_work_unchanged(
    tmp_path: Path, option: str, arc: ARCMemory
) -> None:
    app = _app(tmp_path, arc)
    sid = _session(app)
    arm_goal(app, sid, condition="Original")
    start_loop(app, sid, prompt="Original")
    goal, loop = _get_goal(app, sid), _get_loop(app, sid)
    assert "rejected" in run_goal_command(app, sid, {"input": option + " New goal"})
    assert "rejected" in run_loop_command(app, sid, {"input": "1m " + option + " New loop"})
    assert _get_goal(app, sid) == goal and _get_loop(app, sid) == loop


def test_judge_timeout_cancels_pending_call(
    tmp_path: Path, monkeypatch: Any, arc: ARCMemory
) -> None:
    import dspy

    app = _app(tmp_path, arc)
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


def test_goal_cap_stops_paired_loop_at_real_finalize_seam(
    tmp_path: Path, monkeypatch: Any, arc: ARCMemory
) -> None:
    from clio_agent.gact import turn_finalize_goal

    app = _app(tmp_path, arc)
    sid = _session(app)
    arm_goal(app, sid, condition="Done", max_tokens=1)
    start_loop(app, sid, prompt="One checkpoint")
    app.state.sessions.update(sid, add_tokens_input=2)
    monkeypatch.setattr(turn_finalize_goal, "finalize_turn", lambda *args, **kwargs: True)
    state = SimpleNamespace(
        app=app, sid=sid, turn_id="t", trace_id="tr", user_msg=SimpleNamespace(metadata={})
    )
    asyncio.run(
        turn_finalize_goal.finalize_turn_async(
            cast("TurnState", state),
            None,
            drain_observed_tool_calls=lambda: [],
            update_retry_attempt=lambda *args: None,
        )
    )
    assert _get_loop(app, sid)["active"]
    assert len(app.state.schedules.list(session_id=sid)) == 1
    assert _get_goal(app, sid)["clear_reason"] == "goal_budget"


def test_judge_retains_precise_artifact_facts_and_recent_chronology(
    tmp_path: Path, arc: ARCMemory
) -> None:
    """A generic UI title must not hide the filenames that prove the user's goal."""
    app = _app(tmp_path, arc)
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
@pytest.mark.asyncio
async def test_wakeup_waits_for_human_input(
    autonomous_session: tuple[Any, Conversations, str], status: str
) -> None:
    app, owner, sid = autonomous_session

    def exercise() -> None:
        start_loop(app, sid, prompt="One checkpoint", interval_s=60)
        app.state.sessions.update(
            sid, status=status, metadata_patch={"execution_gate": "pending-human-approval"}
        )
        schedule = app.state.schedules.get(_get_loop(app, sid)["pending_schedule_id"])
        _fire_schedule(app, schedule)
        [item] = owner.history.pending_mail(sid)
        assert item.kind == "schedule"
        content = owner.history.read_content(item.content)
        assert isinstance(content, dict)
        metadata = content["metadata"]
        assert isinstance(metadata, dict) and metadata["schedule_id"] == schedule.id
        assert not owner.eligible(sid, item)
        assert owner.mail.stage(owner.caller(sid)).items == ()
        # Acceptance retires the one-shot timer; the canonical mailbox retains
        # its occurrence through the human gate instead of a second defer queue.
        assert app.state.schedules.get(schedule.id) is None
        app.state.sessions.update(sid, status="idle", metadata_patch={"execution_gate": ""})
        assert owner.eligible(sid, item)
        assert owner.history.pending_mail(sid) == (item,)
        assert not owner.history.acknowledged_notifications(sid)

    await owner.history.io.run(exercise)


@pytest.mark.asyncio
async def test_exhausted_schedule_retries_settle_loop_and_goal(
    autonomous_session: tuple[Any, Conversations, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact import scheduler_runtime
    from clio_agent.gact.scheduler_runtime import _fire_one

    app, owner, sid = autonomous_session

    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Staging unavailable")

    monkeypatch.setattr(scheduler_runtime, "accept_session_input", fail)
    monkeypatch.setenv("CLIO_SCHEDULER_MAX_RETRIES", "1")

    def exercise() -> None:
        arm_goal(app, sid, condition="Done")
        start_loop(app, sid, prompt="One checkpoint")
        schedule = app.state.schedules.get(_get_loop(app, sid)["pending_schedule_id"])
        _fire_one(app, schedule)
        _fire_one(app, schedule)
        assert _get_loop(app, sid)["stop_reason"] == "loop_schedule_failed"
        assert _get_goal(app, sid)["active"]
        assert not app.state.schedules.list(session_id=sid)
        assert owner.history.pending_mail(sid) == ()

    await owner.history.io.run(exercise)


@pytest.mark.asyncio
async def test_startup_storage_failure_retries_before_firing(
    autonomous_session: tuple[Any, Conversations, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient startup read failure must neither fire unsafe work nor kill the scheduler."""
    from clio_agent.gact import scheduler_runtime

    app, _, _ = autonomous_session
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
        await scheduler_runtime._scheduler_tick(app)
    assert attempts == ["reconcile", "reconcile", "tick"]


@pytest.mark.parametrize("starting", [True, False])
@pytest.mark.asyncio
async def test_overdue_wakeup_waits_for_runtime_readiness(
    autonomous_session: tuple[Any, Conversations, str], starting: bool
) -> None:
    app, owner, sid = autonomous_session

    def exercise() -> None:
        start_loop(app, sid, prompt="One checkpoint", interval_s=60)
        schedule = app.state.schedules.get(_get_loop(app, sid)["pending_schedule_id"])
        app.state.agent = None if starting else object()
        app.state.lm_config_status = {"state": "ready" if starting else "configuring"}
        _fire_schedule(app, schedule)
        assert not owner.history.pending_mail(sid)
        assert schedule.id in app.state.deferred_schedules
        assert app.state.schedules.get(schedule.id).enabled
        app.state.agent = object()
        app.state.lm_config_status = {"state": "ready"}
        _fire_schedule(app, schedule)
        [item] = owner.history.pending_mail(sid)
        assert item.kind == "schedule" and owner.eligible(sid, item)
        assert app.state.schedules.get(schedule.id) is None
        _fire_schedule(app, schedule)
        assert owner.history.pending_mail(sid) == (item,)
        assert not owner.history.acknowledged_notifications(sid)

    await owner.history.io.run(exercise)


def test_scheduler_restores_a_sessions_own_model(
    tmp_path: Path, monkeypatch: Any, arc: ARCMemory
) -> None:
    from clio_agent.gact import session_host_agent
    from clio_agent.gact.autonomy_guard import prepare_scheduled_host

    app = _app(tmp_path, arc)
    sid = _session(app)
    app.state.sessions.update(sid, model={"provider_id": "codex", "model_id": "gpt-6-luna"})
    start_loop(app, sid, prompt="One checkpoint")
    app.state.agent = None
    app.state.agent_construction_task = SimpleNamespace(done=lambda: False)
    built: list[str] = []

    async def build(app: Any, session_id: str, request: Any) -> None:
        ref = session_host_agent.selected_ref(app, session_id, request)
        assert ref is not None
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
