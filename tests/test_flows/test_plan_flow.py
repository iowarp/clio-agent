"""The plan flow: dependencies without barriers, editors, growth bounds and approval."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from tests.test_flows.conftest import assistant_messages, new_session, post, text_of, wait_until


def _done(client: TestClient, sid: str) -> dict[str, Any]:
    return wait_until(lambda: (assistant_messages(client, sid) or [None])[0], timeout=90)


def _plan_run(client: TestClient, sid: str) -> dict[str, Any]:
    """The checkpoint of the plan flow run (in the child running the plan)."""

    history = client.app.state.task_history
    owner = history.latest_assignment(sid)
    outer = history.read_content(history.workflow_checkpoint(owner.handle).content)
    row = next(r for r in outer["calls"].values() if r["kind"] == "flow")
    child = history.task_snapshot(row["handle"])
    return history.read_content(history.workflow_checkpoint(child.handle).content)


def test_plan_nodes_run_after_their_dependencies(flow_app: TestClient) -> None:
    sid = new_session(flow_app, "clio/planner")
    post(flow_app, sid, "map the coast")
    message = _done(flow_app, sid)
    assert "done:" in text_of(message), message.get("error_info")
    run = _plan_run(flow_app, sid)
    assert run["flow"] == "plan" and run["status"] == "completed"
    history = flow_app.app.state.task_history
    handles = [row["handle"] for row in run["calls"].values()]
    # Every node ran once, as its own child assignment of the plan run.
    assert len(set(handles)) == 4
    tasks = [history.task_snapshot(h) for h in handles]
    assert all(t.status == "completed" for t in tasks)
    merge = next(t for t in tasks if '"left"' in t.description and '"right"' in t.description)
    # The merge node waited on both branches and received both answers; each
    # branch received its instructions and the survey's answer.
    assert '"inputs"' in merge.description
    left = next(t for t in tasks if "left branch" in t.description)
    assert '"survey"' in left.description
    events = [e["event"] for e in run["events"].values()]
    assert events.count("node_completed") == 4


def test_editor_adds_a_node_that_runs(flow_app: TestClient) -> None:
    sid = new_session(flow_app, "clio/editor")
    post(flow_app, sid, "write a note")
    message = _done(flow_app, sid)
    assert message.get("error_info") is None, message.get("error_info")
    run = _plan_run(flow_app, sid)
    events = list(run["events"].values())
    added = [e for e in events if e["event"] == "node_added"]
    assert [e["node"] for e in added] == ["polish"]
    statuses = {n["id"]: n["status"] for n in run["result"]["plan"]["nodes"]}
    assert statuses == {"draft": "completed", "critique": "completed", "polish": "completed"}
    assert "polish the draft" in text_of(message)


def test_approval_question_gates_an_added_node(flow_app: TestClient) -> None:
    sid = new_session(flow_app, "clio/editor")
    post(flow_app, sid, "write a note, approve edits")
    history = flow_app.app.state.task_history

    def question() -> Any:
        owner = history.latest_assignment(sid)
        checkpoint = history.workflow_checkpoint(owner.handle) if owner else None
        outer = history.read_content(checkpoint.content) if checkpoint else {}
        rows = [r for r in outer.get("calls", {}).values() if r["kind"] == "flow"]
        if not rows:
            return None
        child = history.task_snapshot(rows[0]["handle"])
        inner = history.workflow_checkpoint(child.handle)
        run = history.read_content(inner.content) if inner else {}
        asks = [r for r in run.get("calls", {}).values() if r.get("kind") == "ask"]
        proposed = [e for e in run.get("events", {}).values() if e["event"] == "node_proposed"]
        return (asks[0]["handle"], child.owner.session_id, proposed, child.handle) if asks else None

    handle, plan_session, proposed, plan_handle = wait_until(question, timeout=90)
    assert proposed and proposed[0]["node"] == "polish"
    snapshot = history.task_snapshot(handle)
    assert snapshot.kind == "Question" and snapshot.terminal_at is None
    run = _plan_run(flow_app, sid)
    assert not any(e["event"] == "node_added" for e in run["events"].values())
    # The run view draws the live plan: the proposal awaits approval behind its Question.
    live = flow_app.get(f"/v1/sessions/{sid}/tasks/{plan_handle}/flow")
    assert live.status_code == 200, live.text
    view = live.json()
    assert view["flow"] == "plan" and view["status"] == "running"
    assert [row["id"] for row in view["view"]["awaiting_approval"]] == ["polish"]
    assert [row["handle"] for row in view["questions"]] == [handle]
    nodes = {row["id"]: row for row in view["view"]["nodes"]}
    assert set(nodes) == {"draft", "critique"}
    assert nodes["draft"]["status"] == "completed" and nodes["draft"]["task_status"] == "completed"
    assert nodes["draft"]["handle"].startswith("task_")
    answered = flow_app.post(
        f"/v1/sessions/{plan_session}/questions/{handle}/answer",
        json={"answer": "yes", "selected_options": ["yes"]},
    )
    assert answered.status_code in (200, 202), answered.text
    message = _done(flow_app, sid)
    assert "polish the draft" in text_of(message), message.get("error_info")
    run = _plan_run(flow_app, sid)
    assert [e["node"] for e in run["events"].values() if e["event"] == "node_added"] == ["polish"]
    final = flow_app.get(f"/v1/sessions/{sid}/tasks/{plan_handle}/flow").json()
    assert final["status"] == "completed" and final["questions"] == []
    assert final["view"]["awaiting_approval"] == []
    polish = next(row for row in final["view"]["nodes"] if row["id"] == "polish")
    assert polish["status"] == "completed" and polish["task_status"] == "completed"


def test_failed_node_is_a_typed_stall_in_the_run_view(flow_app: TestClient) -> None:
    sid = new_session(flow_app, "clio/planner")
    post(flow_app, sid, "FAIL the survey")
    message = _done(flow_app, sid)
    history = flow_app.app.state.task_history
    owner = history.latest_assignment(sid)
    outer = history.read_content(history.workflow_checkpoint(owner.handle).content)
    plan_handle = next(r["handle"] for r in outer["calls"].values() if r["kind"] == "flow")
    view = flow_app.get(f"/v1/sessions/{sid}/tasks/{plan_handle}/flow").json()
    assert view["status"] == "flow_stall", view
    assert view["outcome"]["stall"] == "node_failed" and view["outcome"]["step"] == "survey"
    nodes = {row["id"]: row["status"] for row in view["view"]["nodes"]}
    assert nodes["survey"] == "failed"
    assert {nodes[n] for n in ("left", "right", "merge")} == {"cancelled"}
    assert message.get("error_info"), message
