"""The workflow flow: dependencies without barriers, editors, growth bounds and approval."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.test_flows.conftest import assistant_messages, new_session, post, text_of, wait_until


def _done(client: TestClient, sid: str) -> dict[str, Any]:
    return wait_until(lambda: (assistant_messages(client, sid) or [None])[0], timeout=90)


def _workflow_run(client: TestClient, sid: str) -> dict[str, Any]:
    """The checkpoint of the workflow flow run (in the child running the workflow)."""

    history = client.app.state.task_history
    owner = history.latest_assignment(sid)
    outer = history.read_content(history.workflow_checkpoint(owner.handle).content)
    row = next(r for r in outer["calls"].values() if r["kind"] == "flow")
    child = history.task_snapshot(row["handle"])
    return history.read_content(history.workflow_checkpoint(child.handle).content)


def test_workflow_steps_run_after_their_dependencies(flow_app: TestClient) -> None:
    sid = new_session(flow_app, "clio/planner")
    post(flow_app, sid, "map the coast")
    message = _done(flow_app, sid)
    assert "done:" in text_of(message), message.get("error_info")
    run = _workflow_run(flow_app, sid)
    assert run["flow"] == "workflow" and run["status"] == "completed"
    history = flow_app.app.state.task_history
    handles = [row["handle"] for row in run["calls"].values()]
    # Every step ran once, as its own child assignment of the workflow run.
    assert len(set(handles)) == 4
    tasks = [history.task_snapshot(h) for h in handles]
    assert all(t.status == "completed" for t in tasks)
    # The same durable session title names each worker in questions and permissions.
    children = [flow_app.app.state.sessions.get(task.owner.session_id) for task in tasks]
    assert {child.title for child in children} == {"survey", "left", "right", "merge"}
    assert all(child.metadata["subagent_name"] == child.title for child in children)
    merge = next(t for t in tasks if '"left"' in t.description and '"right"' in t.description)
    # The merge step waited on both branches and received both answers; each
    # branch received its instructions and the survey's answer.
    assert '"inputs"' in merge.description
    left = next(t for t in tasks if "left branch" in t.description)
    assert '"survey"' in left.description
    events = [e["event"] for e in run["events"].values()]
    assert events.count("step_completed") == 4
    # Nothing bounded the workflow's growth, so it ran unbounded, as recorded.
    bounds = [row["result"] for row in run["steps"].values() if row["name"] == "_bounds"]
    assert bounds == [{"max_added_steps": None, "max_edit_rounds": None}]


def test_the_persons_ceiling_caps_a_workflows_growth(
    flow_app: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workflow asks for one added step and leaves rounds open: a ceiling above the workflow's
    own leaves the workflow's, and the rounds ceiling bounds the open rounds."""

    monkeypatch.setenv("CLIO_WORKFLOW_MAX_ADDED_STEPS", "5")
    monkeypatch.setenv("CLIO_WORKFLOW_MAX_EDIT_ROUNDS", "1")
    sid = new_session(flow_app, "clio/editor")
    post(flow_app, sid, "write a note")
    _done(flow_app, sid)
    run = _workflow_run(flow_app, sid)
    bounds = [row["result"] for row in run["steps"].values() if row["name"] == "_bounds"]
    assert bounds == [{"max_added_steps": 1, "max_edit_rounds": 1}]


@pytest.mark.parametrize(
    ("asked", "ceiling", "bound"),
    [(None, 0, None), (4, 0, 4), (None, 3, 3), (4, 3, 3), (2, 3, 2), (0, 0, 0)],
)
def test_the_tighter_of_the_callers_bound_and_the_ceiling_wins(
    asked: int | None, ceiling: int, bound: int | None
) -> None:
    from clio_agent.flows.limits import effective

    assert effective(asked, ceiling) == bound


def test_editor_adds_a_step_that_runs(flow_app: TestClient) -> None:
    sid = new_session(flow_app, "clio/editor")
    post(flow_app, sid, "write a note")
    message = _done(flow_app, sid)
    assert message.get("error_info") is None, message.get("error_info")
    run = _workflow_run(flow_app, sid)
    events = list(run["events"].values())
    added = [e for e in events if e["event"] == "step_added"]
    assert [e["step"] for e in added] == ["polish"]
    # The added step exists only once the critique that proposed it has finished.
    assert added[0]["after"] == ["draft", "critique"]
    statuses = {n["id"]: n["status"] for n in run["result"]["workflow"]["steps"]}
    assert statuses == {"draft": "completed", "critique": "completed", "polish": "completed"}
    # The caller reads every step: the final one first, then the earlier ones, labelled.
    answer = run["result"]["answer"]
    assert answer.startswith("[polish]\n") and "\n\nEarlier steps:\n\n[draft]\n" in answer
    assert "[critique]\n" in answer
    assert "done: polish done: " in text_of(message)


def test_approval_question_gates_an_added_step(flow_app: TestClient) -> None:
    sid = new_session(flow_app, "clio/editor")
    post(flow_app, sid, "write a note, approve edits")
    history = flow_app.app.state.task_history

    def question() -> Any:
        owner = history.latest_assignment(sid)
        checkpoint = history.workflow_checkpoint(owner.handle) if owner else None
        outer = history.read_content(checkpoint.content) if checkpoint else {}
        rows = [
            r for r in outer.get("calls", {}).values() if r["kind"] == "flow" and r.get("handle")
        ]
        if not rows:
            return None
        child = history.task_snapshot(rows[0]["handle"])
        inner = history.workflow_checkpoint(child.handle)
        run = history.read_content(inner.content) if inner else {}
        asks = [r for r in run.get("calls", {}).values() if r.get("kind") == "ask"]
        proposed = [e for e in run.get("events", {}).values() if e["event"] == "step_proposed"]
        return (asks[0]["handle"], child.owner.session_id, proposed, child.handle) if asks else None

    handle, workflow_session, proposed, workflow_handle = wait_until(question, timeout=90)
    assert proposed and proposed[0]["step"] == "polish"
    snapshot = history.task_snapshot(handle)
    assert snapshot.kind == "Question" and snapshot.terminal_at is None
    run = _workflow_run(flow_app, sid)
    assert not any(e["event"] == "step_added" for e in run["events"].values())
    # The run view draws the live workflow: the proposal awaits approval behind its Question.
    live = flow_app.get(f"/v1/sessions/{sid}/tasks/{workflow_handle}/flow")
    assert live.status_code == 200, live.text
    view = live.json()
    assert view["flow"] == "workflow" and view["status"] == "running"
    assert [row["id"] for row in view["view"]["awaiting_approval"]] == ["polish"]
    assert [row["handle"] for row in view["questions"]] == [handle]
    steps = {row["id"]: row for row in view["view"]["steps"]}
    assert set(steps) == {"draft", "critique"}
    assert steps["draft"]["status"] == "completed" and steps["draft"]["task_status"] == "completed"
    assert steps["draft"]["handle"].startswith("task_")
    answered = flow_app.post(
        f"/v1/sessions/{workflow_session}/questions/{handle}/answer",
        json={"answer": "yes", "selected_options": ["yes"]},
    )
    assert answered.status_code in (200, 202), answered.text
    message = _done(flow_app, sid)
    assert "done: polish done: " in text_of(message), message.get("error_info")
    run = _workflow_run(flow_app, sid)
    assert [e["step"] for e in run["events"].values() if e["event"] == "step_added"] == ["polish"]
    final = flow_app.get(f"/v1/sessions/{sid}/tasks/{workflow_handle}/flow").json()
    assert final["status"] == "completed" and final["questions"] == []
    assert final["view"]["awaiting_approval"] == []
    polish = next(row for row in final["view"]["steps"] if row["id"] == "polish")
    assert polish["status"] == "completed" and polish["task_status"] == "completed"


def test_cancelling_a_workflow_waiting_for_approval_settles_it(flow_app: TestClient) -> None:
    """A workflow blocked on its approval Question is cancelled with the Question."""

    sid = new_session(flow_app, "clio/editor")
    post(flow_app, sid, "write a note, approve edits")
    history = flow_app.app.state.task_history

    def question() -> Any:
        owner = history.latest_assignment(sid)
        checkpoint = history.workflow_checkpoint(owner.handle) if owner else None
        outer = history.read_content(checkpoint.content) if checkpoint else {}
        rows = [
            r for r in outer.get("calls", {}).values() if r["kind"] == "flow" and r.get("handle")
        ]
        if not rows:
            return None
        child = history.task_snapshot(rows[0]["handle"])
        inner = history.workflow_checkpoint(child.handle)
        run = history.read_content(inner.content) if inner else {}
        asks = [r for r in run.get("calls", {}).values() if r.get("kind") == "ask"]
        return (asks[0]["handle"], child.handle) if asks else None

    ask, workflow_handle = wait_until(question, timeout=90)
    response = flow_app.post(f"/v1/sessions/{sid}/tasks/cancel", json={"tasks": workflow_handle})
    assert response.status_code == 200 and response.json()["errors"] == [], response.text
    wait_until(lambda: history.task_snapshot(workflow_handle).terminal_at is not None, timeout=60)
    assert history.task_snapshot(workflow_handle).status == "cancelled"
    assert history.task_snapshot(ask).terminal_at is not None


def test_cancelling_one_step_cancels_it_and_ends_the_workflow_cancelled(
    flow_app: TestClient,
) -> None:
    """A person cancels a running step: it reads cancelled, its dependents are skipped,
    and the run ends cancelled rather than as a failed step's stall."""
    import time

    from clio_agent.agent import cancellation_requested
    from clio_agent.gact.agents.clio_react import _turn_cancelled
    from tests.test_flows.conftest import default_program

    def program(question: str = "", **kwargs: Any) -> Any:
        if "HOLD" in question and "left branch" not in question:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if cancellation_requested():
                    raise _turn_cancelled()
                time.sleep(0.02)
        return default_program(question=question, **kwargs)

    flow_app.app.state.flow_program["run"] = program
    sid = new_session(flow_app, "clio/planner")
    post(flow_app, sid, "HOLD the survey")
    history = flow_app.app.state.task_history

    def workflow_and_survey() -> tuple[str, str] | None:
        owner = history.latest_assignment(sid)
        checkpoint = owner and history.workflow_checkpoint(owner.handle)
        if checkpoint is None:
            return None
        outer = history.read_content(checkpoint.content)
        rows = [r for r in (outer.get("calls") or {}).values() if r["kind"] == "flow"]
        if not rows or not rows[0].get("handle"):
            return None
        view = flow_app.get(f"/v1/sessions/{sid}/tasks/{rows[0]['handle']}/flow").json()
        steps = (view.get("view") or {}).get("steps") or []
        survey = next((r for r in steps if r["id"] == "survey" and r.get("handle")), None)
        return (rows[0]["handle"], survey["handle"]) if survey else None

    workflow, survey = wait_until(workflow_and_survey, timeout=60)
    response = flow_app.post(f"/v1/sessions/{sid}/tasks/cancel", json={"tasks": survey})
    assert response.status_code == 200 and response.json()["errors"] == [], response.text

    def settled() -> dict[str, Any] | None:
        view = flow_app.get(f"/v1/sessions/{sid}/tasks/{workflow}/flow").json()
        return view if view.get("status") not in {None, "running"} else None

    view = wait_until(settled, timeout=60)
    assert view["status"] == "cancelled", view.get("events")
    steps = {row["id"]: row["status"] for row in view["view"]["steps"]}
    assert set(steps.values()) == {"cancelled"}, steps


def test_failed_step_is_a_typed_stall_in_the_run_view(flow_app: TestClient) -> None:
    sid = new_session(flow_app, "clio/planner")
    post(flow_app, sid, "FAIL the survey")
    message = _done(flow_app, sid)
    history = flow_app.app.state.task_history
    owner = history.latest_assignment(sid)
    outer = history.read_content(history.workflow_checkpoint(owner.handle).content)
    workflow_handle = next(r["handle"] for r in outer["calls"].values() if r["kind"] == "flow")
    view = flow_app.get(f"/v1/sessions/{sid}/tasks/{workflow_handle}/flow").json()
    assert view["status"] == "flow_stall", view
    assert view["outcome"]["stall"] == "step_failed" and view["outcome"]["step"] == "survey"
    steps = {row["id"]: row["status"] for row in view["view"]["steps"]}
    assert steps["survey"] == "failed"
    assert {steps[n] for n in ("left", "right", "merge")} == {"cancelled"}
    assert message.get("error_info"), message


def test_workflow_editor_tools_build_and_only_propose() -> None:
    """The editor tools an editor's call is granted construct and return proposals."""

    from clio_agent.gact.flow_tools import WORKFLOW_EDITOR_TOOLS, build_workflow_editor_tools

    tools = build_workflow_editor_tools({"run": "flowrun_x", "step": "review"})
    assert [tool.name for tool in tools] == list(WORKFLOW_EDITOR_TOOLS)
    add, cancel = tools
    proposed = add.func(step_id="polish", target="self", after=["draft"])
    assert proposed["proposed"] is True and proposed["run"] == "flowrun_x"
    assert proposed["after"] == ["draft"]
    assert cancel.func(step_id="polish")["cancel"] is True


def test_a_read_only_role_keeps_the_editor_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """The review role is read-only; as a workflow's editor it still sees the editor tools.

    Live, a ``review`` editor step reported it had no ``workflow_add_step``: its read-only
    posture drops natives that do not declare ``read_only``, and proposing changes nothing.
    """

    from types import SimpleNamespace

    from clio_agent.gact.agents import session_tool_posture
    from clio_agent.gact.agents.tool_instrumentation import instrument_tools
    from clio_agent.gact.flow_tools import WORKFLOW_EDITOR_TOOLS, build_workflow_editor_tools
    from clio_agent.gact.side_sessions import READ_ONLY_SIDE_PROFILE

    session = SimpleNamespace(id="sess_editor", approval_profile=READ_ONLY_SIDE_PROFILE)
    monkeypatch.setattr(session_tool_posture, "_active_session", lambda: session)
    tools = instrument_tools(build_workflow_editor_tools({"run": "flowrun_x", "step": "edit"}))
    kept = session_tool_posture.restrict_tools_to_session_posture(tools)
    assert [tool.name for tool in kept] == list(WORKFLOW_EDITOR_TOOLS)


def test_a_cancelled_workflow_ends_cancelled_not_stalled(flow_app: TestClient) -> None:
    """Cancelling the workflow run cancels its running step: the step and its dependents
    read cancelled and the run ends cancelled, never as a failed step's stall."""
    import time

    from clio_agent.agent import cancellation_requested
    from clio_agent.gact.agents.clio_react import _turn_cancelled
    from tests.test_flows.conftest import default_program

    def program(question: str = "", **kwargs: Any) -> Any:
        if "HOLD" in question and "left branch" not in question:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if cancellation_requested():
                    raise _turn_cancelled()
                time.sleep(0.02)
        return default_program(question=question, **kwargs)

    flow_app.app.state.flow_program["run"] = program
    sid = new_session(flow_app, "clio/planner")
    post(flow_app, sid, "HOLD the survey")
    history = flow_app.app.state.task_history

    def workflow_handle() -> str | None:
        owner = history.latest_assignment(sid)
        checkpoint = owner and history.workflow_checkpoint(owner.handle)
        if checkpoint is None:
            return None
        outer = history.read_content(checkpoint.content)
        rows = [r for r in (outer.get("calls") or {}).values() if r["kind"] == "flow"]
        return rows[0]["handle"] if rows and rows[0].get("handle") else None

    handle = wait_until(workflow_handle, timeout=60)

    def survey_started() -> bool:
        view = flow_app.get(f"/v1/sessions/{sid}/tasks/{handle}/flow").json()
        steps = (view.get("view") or {}).get("steps") or []
        return any(row["id"] == "survey" and row.get("handle") for row in steps)

    wait_until(survey_started, timeout=60)
    response = flow_app.post(f"/v1/sessions/{sid}/tasks/cancel", json={"tasks": handle})
    assert response.status_code == 200 and response.json()["errors"] == [], response.text

    def settled() -> dict[str, Any] | None:
        view = flow_app.get(f"/v1/sessions/{sid}/tasks/{handle}/flow").json()
        return view if view.get("status") not in {None, "running"} else None

    view = wait_until(settled, timeout=60)
    assert view["status"] == "cancelled", view["events"][0]
    steps = {row["id"]: row["status"] for row in view["view"]["steps"]}
    assert set(steps.values()) == {"cancelled"}, steps


def _workflow_module() -> Any:
    import importlib.util
    from pathlib import Path

    from clio_agent.plugins.store import builtin_records

    record = next(r for r in builtin_records() if any(f.id == "workflow" for f in r.flows))
    flow = next(f for f in record.flows if f.id == "workflow")
    path = Path(record.root) / "ai.iowarp.clio" / flow.module
    spec = importlib.util.spec_from_file_location("_flow_workflow_refs", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("Check this claim: $draft.", "Check this claim: 8 MiB.", id="full-stop"),
        pytest.param("($draft), then $draft-", "(8 MiB), then 8 MiB-", id="punctuation"),
        pytest.param("Compare $step-two with $task", "Compare B with T", id="dashed-id"),
        pytest.param("Unknown $nothing. stays", "Unknown $nothing. stays", id="unknown"),
    ],
)
def test_a_step_reference_ends_where_a_step_id_can(text: str, expected: str) -> None:
    """Live, a critic got "Check this claim: $draft." with $draft unresolved: the full stop
    was read as part of the name."""

    from types import SimpleNamespace

    results = {"draft": SimpleNamespace(answer="8 MiB"), "step-two": SimpleNamespace(answer="B")}
    assert _workflow_module()._substitute(text, "T", results) == expected
