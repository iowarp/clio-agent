"""Goal checking is visible, single-owner and cancellable independently of work."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact import goal
from tests.test_gact.test_goal import _app, _session


@pytest.mark.parametrize("cancel", [False, True])
def test_pending_judge_has_one_visible_owner(
    tmp_path: Path, monkeypatch: Any, cancel: bool
) -> None:
    app = _app(tmp_path)
    sid = _session(app)
    goal.arm_goal(app, sid, condition="Finish verified phases")

    async def exercise() -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def judge(*args: Any) -> goal.GoalJudgement:
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return goal.GoalJudgement(met=False, reason="Another phase remains")

        monkeypatch.setattr(goal, "run_llm_judge", judge)
        pending = asyncio.create_task(
            goal.dispatch_goal_at_finalize(app, session_id=sid, turn_id="one")
        )
        await entered.wait()
        assert goal._get_goal(app, sid)["phase"] == "judging"
        assert await goal.dispatch_goal_at_finalize(app, session_id=sid, turn_id="one") is None
        assert calls == 1
        if cancel:
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            state = goal._get_goal(app, sid)
            assert state["phase"] == "paused" and not state["active"]
        else:
            release.set()
            decision = await pending
            assert decision and decision.outcome == "redrive"
            state = goal._get_goal(app, sid)
            assert state["phase"] == "working"
            assert state["judge_duration_s"] >= 0

    asyncio.run(exercise())
