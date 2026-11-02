"""Steers supersede unexecuted tools and goal verdicts retain delivery order."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

import dspy
import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import MailItem
from clio_agent.gact import context as ctx
from clio_agent.gact import goal as goal_mod
from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.gact.autonomy_guard import goal_redrive_is_current
from clio_agent.gact.goal import GoalJudgement, arm_goal, dispatch_goal_at_finalize
from clio_agent.gact.goal_evidence import JudgeRoom, _build_transcript
from clio_agent.gact.native_work import now
from clio_agent.gact.task_publication import publish_task_notice
from tests._scripted_engine import Reply, calls, scripted_lm, wire
from tests.test_gact.task_records import record_delegation_fixture
from tests.test_gact.test_goal import _app, _session


@pytest.mark.parametrize("proposals", [1, 3])
def test_mid_generation_steer_replans_before_any_proposed_tool_runs(
    clio_core_plane: Any, monkeypatch: pytest.MonkeyPatch, proposals: int
) -> None:
    ran: list[str] = []

    def run(command: str) -> str:
        ran.append(command)
        return f"ran {command}"

    app, sid = clio_core_plane, ctx.active_session_id()
    monkeypatch.setattr(app.state.conversations, "execution_gate", lambda _sid: "test_direct_loop")
    from clio_agent.gact.agents import clio_react

    original = clio_react._call_lm
    submitted = False

    def call(lm: Any, request: Any) -> Any:
        nonlocal submitted
        result = original(lm, request)
        if not submitted:
            submitted = True
            _pending(app, sid, None, text="Do not launch a browser; inspect the saved file.")
        return result

    monkeypatch.setattr(clio_react, "_call_lm", call)
    lm, engine = scripted_lm(
        [
            calls(*[("run", {"command": "launch browser"})] * proposals, text="Trying a browser."),
            calls(("run", {"command": "inspect saved file"}), text="Inspecting the file instead."),
            Reply(text="File checked."),
        ]
    )
    with dspy.context(lm=lm):
        prediction = ClioReAct("question -> answer", tools=[run])(question="check the page")
    assert ran == ["inspect saved file"]
    assert prediction.answer == "File checked."
    observed = wire(engine.requests[1])
    assert observed[-2] == ("assistant", [("text", "Trying a browser.")])
    assert observed[-1][0] == "user"
    assert observed[-1][1][0][1].endswith("Do not launch a browser; inspect the saved file.")
    assert "launch browser" not in str(wire(engine.requests[2]))


def test_child_result_does_not_discard_a_tool_proposal(
    clio_core_plane: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.gact.agents import clio_react

    app, sid = clio_core_plane, ctx.active_session_id()
    monkeypatch.setattr(app.state.conversations, "execution_gate", lambda _sid: "test_direct_loop")
    ran: list[str] = []
    original = clio_react._call_lm
    posted = False

    def call(lm: Any, request: Any) -> Any:
        nonlocal posted
        result = original(lm, request)
        if not posted:
            posted = True
            child = record_delegation_fixture(
                app,
                sid,
                handle="task_steer_child",
                completed=True,
                result={"answer": "child finished"},
            )
            publish_task_notice(app.state.task_history, child)
        return result

    def inspect(path: str) -> str:
        ran.append(path)
        return "valid"

    monkeypatch.setattr(clio_react, "_call_lm", call)
    lm, engine = scripted_lm([calls(("inspect", {"path": "page.html"})), Reply(text="done")])
    with dspy.context(lm=lm):
        ClioReAct("question -> answer", tools=[inspect])(question="check")
    assert ran == ["page.html"]
    observed = wire(engine.requests[1])
    assert observed[-2][0] == "tool"
    assert observed[-1][0] == "user" and "child finished" in observed[-1][1][0][1]


def _message(id: str, role: str, *parts: Any, **metadata: Any) -> NS:
    return NS(id=id, role=role, session_id="s", parts=list(parts), metadata=metadata)


def _text(id: str, text: str) -> NS:
    return NS(id=id, type="text", text=text)


def _steer(id: str, anchor: str, *, owner: str = "a", consumed: bool = True) -> NS:
    return _message(
        id,
        "user",
        _text(f"{id}-text", id),
        mid_turn_steer=consumed,
        steer_delivery={"assistant_message_id": owner, "after_part_id": anchor},
    )


def _transcript(*messages: Any, room: JudgeRoom | None = None) -> str:
    return _build_transcript(NS(state=NS(messages={"s": list(messages)})), "s", room=room)


def test_judge_receives_steer_between_earlier_and_later_assistant_prose() -> None:
    assistant = _message(
        "a", "assistant", _text("before", "old plan"), _text("after", "corrected plan")
    )
    transcript = _transcript(assistant, _steer("change plan", "before"))
    assert (
        transcript.index("old plan")
        < transcript.index("user: change plan")
        < transcript.index("corrected plan")
    )


def test_tool_result_shares_its_call_boundary_like_the_public_transcript() -> None:
    assistant = _message(
        "a",
        "assistant",
        NS(
            id="call-part",
            type="tool_call",
            call_id="call",
            tool_name="inspect",
            input={"token": "private"},
        ),
        NS(
            id="result-part",
            type="tool_result",
            call_id="call",
            tool_name="inspect",
            is_error=False,
            content=[_text("result", "valid")],
        ),
        _text("after", "updated answer"),
    )
    transcript = _transcript(assistant, _steer("new scope", "call-part"))
    assert (
        transcript.index("inspect completed: valid")
        < transcript.index("user: new scope")
        < transcript.index("updated answer")
    )
    assert "private" not in transcript


def test_multiple_steers_are_each_visible_once_at_their_recorded_boundaries() -> None:
    transcript = _transcript(
        _message("a", "assistant", _text("x", "one"), _text("y", "two"), _text("z", "three")),
        _steer("before all", ""),
        _steer("first", "x"),
        _steer("second", "x"),
        _steer("third", "y"),
    )
    values = [
        "user: before all",
        "assistant: one",
        "user: first",
        "user: second",
        "assistant: two",
        "user: third",
        "assistant: three",
    ]
    assert [transcript.index(value) for value in values] == sorted(
        transcript.index(value) for value in values
    )
    assert all(transcript.count(value) == 1 for value in values)


@pytest.mark.parametrize(
    "owner,anchor,consumed", [("missing", "x", True), ("a", "missing", True), ("a", "x", False)]
)
def test_invalid_or_pending_receipts_never_hide_the_user_message(
    owner: str, anchor: str, consumed: bool
) -> None:
    transcript = _transcript(
        _message("a", "assistant", _text("x", "answer")),
        _steer("visible", anchor, owner=owner, consumed=consumed),
    )
    assert transcript.index("assistant: answer") < transcript.index("user: visible")
    assert transcript.count("user: visible") == 1


def test_a_window_that_holds_only_the_newest_entry_keeps_the_steer() -> None:
    # Room for the steer's entry and the omission note, not for its owner's entry.
    narrow = _transcript(
        _message("a", "assistant", _text("x", "answer " * 40)),
        _steer("visible", "x"),
        room=JudgeRoom(tokens=20, window=100),
    )
    assert "user: visible" in narrow and "answer" not in narrow
    assert "evidence_exceeds_judge_context" in narrow


def test_harness_injections_remain_algorithmic() -> None:
    transcript = _transcript(
        _message("auto", "user", NS(type="injection", source="goal_judgment", text="continue"))
    )
    assert "recorded harness injection (goal_judgment): continue" in transcript
    assert "user: continue" not in transcript


def _pending(
    app: Any,
    sid: str,
    tmp_path: Path | None,
    *,
    state: str = "pending",
    text: str = "change scope",
) -> None:
    """Pending and interrupted-staging cases are unacknowledged Core input."""
    del tmp_path, state
    history = app.state.task_history
    history.record_mail(
        MailItem(
            id="human",
            recipient_session=sid,
            kind="human",
            created_at=now(),
            sender_human="test-human",
            content=history.write_content(sid, {"text": text}),
        )
    )


@pytest.mark.parametrize("state", ["pending", "claimed"])
def test_goal_does_not_judge_before_a_pending_steer_is_delivered(
    tmp_path: Path, arc: ARCMemory, monkeypatch: Any, state: str
) -> None:
    app = _app(tmp_path, arc)
    sid = _session(app)
    arm_goal(app, sid, condition="finish")
    _pending(app, sid, tmp_path, state=state)

    async def judge(*args: Any) -> GoalJudgement:
        pytest.fail("pending human input must be delivered before judging")

    monkeypatch.setattr(goal_mod, "run_llm_judge", judge)
    assert asyncio.run(dispatch_goal_at_finalize(app, session_id=sid, turn_id="t")) is None
    assert goal_mod._get_goal(app, sid)["iters_elapsed"] == 0
    assert not [item for item in app.state.task_history.pending_mail(sid) if item.kind == "goal"]


@pytest.mark.parametrize("met", [True, False])
def test_steer_arriving_during_judge_discards_the_stale_verdict(
    tmp_path: Path, arc: ARCMemory, monkeypatch: Any, met: bool
) -> None:
    app = _app(tmp_path, arc)
    sid = _session(app)
    arm_goal(app, sid, condition="finish")

    async def judge(*args: Any) -> GoalJudgement:
        await asyncio.to_thread(_pending, app, sid, tmp_path)
        await asyncio.sleep(0)
        return GoalJudgement(met=met, reason="old verdict")

    monkeypatch.setattr(goal_mod, "run_llm_judge", judge)
    assert asyncio.run(dispatch_goal_at_finalize(app, session_id=sid, turn_id="t")) is None
    goal = goal_mod._get_goal(app, sid)
    assert goal["active"] and goal["iters_elapsed"] == 0
    assert goal["phase"] == "working"
    assert not [item for item in app.state.task_history.pending_mail(sid) if item.kind == "goal"]


def test_pending_steer_does_not_bypass_an_explicit_goal_limit(
    tmp_path: Path, arc: ARCMemory
) -> None:
    app = _app(tmp_path, arc)
    sid = _session(app)
    arm_goal(app, sid, condition="finish", max_goal_iters=1)
    _pending(app, sid, tmp_path)
    decision = asyncio.run(dispatch_goal_at_finalize(app, session_id=sid, turn_id="t"))
    assert decision is not None and decision.reason == "goal_max_iters"
    assert [item for item in app.state.task_history.pending_mail(sid) if item.kind == "human"]
    assert not [item for item in app.state.task_history.pending_mail(sid) if item.kind == "goal"]


def test_an_already_queued_goal_wake_yields_to_a_later_human_steer(
    tmp_path: Path, arc: ARCMemory
) -> None:
    app = _app(tmp_path, arc)
    sid = _session(app)
    goal = arm_goal(app, sid, condition="finish")
    metadata = {"goal_redrive": True, "goal_id": goal["goal_id"], "goal_iters": 0}
    assert goal_redrive_is_current(app, sid, metadata)
    _pending(app, sid, tmp_path)
    assert not goal_redrive_is_current(app, sid, metadata)
    # Ordinary human/resume events keep their existing admission behavior.
    assert goal_redrive_is_current(app, sid, {})


def test_goal_wake_from_an_older_evaluation_cannot_replay(tmp_path: Path, arc: ARCMemory) -> None:
    app = _app(tmp_path, arc)
    sid = _session(app)
    goal = arm_goal(app, sid, condition="finish")
    goal_mod._put_goal(app, sid, {**goal_mod._get_goal(app, sid), "iters_elapsed": 2})
    metadata = {"goal_redrive": True, "goal_id": goal["goal_id"], "goal_iters": 1}
    assert not goal_redrive_is_current(app, sid, metadata)
    assert goal_redrive_is_current(app, sid, {**metadata, "goal_iters": 2})
