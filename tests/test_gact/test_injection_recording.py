"""How the recorder records CLIO's additions: event notes at each firing, state once, and
the plan the agent wrote itself only when its ``plan_update`` call has left the context.

These go through the real :class:`StepRecorder` on the app's ARC working set, not a
stand-in recorder."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent import injections
from clio_agent.gact.app import build_app
from clio_agent.gact.session_plan import build_plan_update_tool, update_plan
from clio_agent.gact.working_state import PLAN_STALE_SOURCE, working_state_injections
from tests._compaction_summary import keep_open_request_only

pytestmark = pytest.mark.usefixtures("host_agent_executor")

_PLAN = "# Objective\nFind the regression.\n\n## Threads\n- [open] bisect: where it began\n"


@pytest.fixture(autouse=True)
def _open_request_only(monkeypatch: pytest.MonkeyPatch) -> None:
    keep_open_request_only(monkeypatch)


@pytest.fixture
def app_session(tmp_path: Path) -> Any:
    from fastapi.testclient import TestClient

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "regression hunt"}).json()["id"]
        yield app, sid, client


@pytest.fixture
def active(app_session: Any) -> Any:
    """The app and session made current, as during a turn."""
    from clio_agent.gact import context as ctx

    app, sid, _ = app_session
    tokens = [ctx.set_app(app), ctx.set_session_id(sid)]
    try:
        yield app, sid
    finally:
        for token in reversed(tokens):
            ctx.reset(token)


def _recorder(app: Any, sid: str) -> Any:
    from clio_agent.gact.agents.clio_react_record import StepRecorder

    return StepRecorder(app.state.arc, sid, "main", expert_id="main")


def _recorded(app: Any, sid: str, source: str) -> list[str]:
    return [
        str(seg.content.get("text"))
        for seg in app.state.arc.render_working_set(sid, "main")
        if seg.kind == "user" and seg.content.get("source") == source
    ]


def test_an_event_note_is_recorded_at_every_firing(active: Any) -> None:
    """The stale-plan note fires with the same words each time; the second firing was
    dropped as a repeat of the first, still in context. SABOTAGE: compare ``latest`` for a
    fired note too (drop ``firing or`` in ``StepRecorder.injections``) and this fails."""
    app, sid = active
    recorder = _recorder(app, sid)
    text = "The plan (p.md) has not been updated in 25 steps."
    first = injections.fired(text)
    recorder.injections([(PLAN_STALE_SOURCE, first)])
    recorder.injections([(PLAN_STALE_SOURCE, first)])  # one firing offered twice
    recorder.injections([(PLAN_STALE_SOURCE, injections.fired(text))])
    assert _recorded(app, sid, PLAN_STALE_SOURCE) == [text, text]


def test_state_is_recorded_only_when_it_changed(active: Any) -> None:
    app, sid = active
    recorder = _recorder(app, sid)
    recorder.injections([("todos", "- [ ] a")])
    recorder.injections([("todos", "- [ ] a")])
    recorder.injections([("todos", "- [x] a")])
    assert _recorded(app, sid, "todos") == ["- [ ] a", "- [x] a"]


def test_the_plan_the_agent_wrote_is_not_offered_again(active: Any) -> None:
    """After plan_update the next turn start re-offered the whole plan, already in context
    as the call. It is held while a plan_update call is in the working set, offered after
    a compaction (``force_full``) and when the person edited it. SABOTAGE: drop the
    ``held_by`` return in ``plan_injection`` and the second turn records the plan."""
    from dspy.lm15 import ToolCallPart

    app, sid = active
    tool = build_plan_update_tool()
    tool.func(content=_PLAN)  # outside plan mode: the plan is active and offered
    session = app.state.sessions.get(sid)
    [plan] = [text for src, text in working_state_injections(app, sid, session) if src == "plan"]
    assert getattr(plan, "held_by", "") == "plan_update"
    recorder = _recorder(app, sid)
    recorder.injections([("plan", plan)])
    assert _recorded(app, sid, "plan") == [str(plan)]  # no call in context yet: recorded
    call = ToolCallPart(id="c1", name="plan_update", input={"content": _PLAN + "- [open] b\n"})
    tool.func(content=_PLAN + "- [open] b\n")
    recorder.step_done(0, "s", text="", thinking=(), calls=(call,), results={"c1": ("ok", False)})
    session = app.state.sessions.get(sid)
    recorder.injections(working_state_injections(app, sid, session))
    assert len(_recorded(app, sid, "plan")) == 1  # held: the call carries it
    recorder.injections(working_state_injections(app, sid, session, force_full=True))
    assert len(_recorded(app, sid, "plan")) == 2  # after a compaction it is offered


def test_a_persons_edit_is_not_held(app_session: Any) -> None:
    """The rail's save goes through update_plan too, but the agent has not seen it."""
    app, sid, client = app_session
    update_plan(app, sid, content=_PLAN)
    edited = _PLAN.replace("where it began", "check the config first")
    assert client.put(f"/v1/sessions/{sid}/plan", json={"content": edited}).status_code == 200
    from clio_agent.gact.session_plan import plan_injection

    app.state.sessions.update(sid, metadata_patch={"plan_person_edit": False})
    text = plan_injection(app, sid, app.state.sessions.get(sid))
    assert "check the config first" in text and not hasattr(text, "held_by")


def test_a_step_without_tool_calls_is_not_counted(
    active: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A direct answer or a proposal a steer superseded made no call. SABOTAGE: remove the
    ``if not call_names`` return and the note fires after three empty steps."""
    from clio_agent.gact import working_state

    app, sid = active
    update_plan(app, sid, content=_PLAN)
    monkeypatch.setattr(working_state, "stale_steps", lambda: 3)
    noted: list[Any] = []
    loop = SimpleNamespace(recorder=SimpleNamespace(injections=noted.append))
    for _ in range(5):
        working_state.note_stale_plan(loop, [])
    assert noted == []
    for _ in range(3):
        working_state.note_stale_plan(loop, ["fs_read_file"])
    [[(source, text)]] = noted
    assert source == PLAN_STALE_SOURCE and getattr(text, "firing_id", "")


def test_no_stale_note_in_plan_mode_and_deleted_sessions_are_pruned(
    active: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A draft counts as active, but in plan mode the plan is being written. SABOTAGE:
    drop the plan-mode check and the note fires while drafting."""
    from clio_agent.gact import working_state
    from clio_agent.gact.session_plan import enter_plan

    app, sid = active
    enter_plan(app, sid)
    update_plan(app, sid, content=_PLAN)
    monkeypatch.setattr(working_state, "stale_steps", lambda: 2)
    noted: list[Any] = []
    loop = SimpleNamespace(recorder=SimpleNamespace(injections=noted.append))
    for _ in range(4):
        working_state.note_stale_plan(loop, ["fs_read_file"])
    assert noted == []
    app.state.plan_quiet_steps["sess_gone"] = 7
    app.state.plan_quiet_steps.pop(sid)
    working_state.note_stale_plan(loop, ["fs_read_file"])
    assert "sess_gone" not in app.state.plan_quiet_steps and sid in app.state.plan_quiet_steps


@pytest.mark.parametrize(
    ("plan", "unfinished", "marked"),
    [
        ("- [closed] a\n- [active] b\n1. [open] c\n", 2, 3),
        ("- bisect: open\n- config (active)\n- d (closed: commit 4)\n", 2, 3),
        ("- e: dropped — not needed\n* f: closed.\n- note: open questions remain\n", 0, 2),
        ("- just a list\n- of items\n", 0, 0),
    ],
)
def test_thread_states_in_the_forms_plans_are_written(
    app_session: Any, plan: str, unfinished: int, marked: int
) -> None:
    """The repo's own plans wrote ``- bisect: open``; only ``- [open]`` was counted.
    SABOTAGE: restore the bracket-only pattern and the second case counts 0."""
    app, sid, _ = app_session
    update_plan(app, sid, content=f"# Objective\nx\n\n{plan}")
    result = update_plan(app, sid, status="done")
    assert (result["unfinished_threads"], result["marked_threads"]) == (unfinished, marked)


def test_a_done_plan_without_markers_says_it_cannot_count(active: Any) -> None:
    app, sid = active
    tool = build_plan_update_tool()
    tool.func(content="# Objective\nx\n\n- one\n- two\n")
    reply = tool.func(status="done")
    assert "No thread in the plan carries a state marker" in reply
    tool.func(content="# Objective\nx\n\n- [open] one\n")
    assert "still marks 1 thread open or active" in tool.func(status="done")
