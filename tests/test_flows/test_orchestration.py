"""The orchestration plugin: run_flow returns a handle at acceptance; council; replay; cancel."""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import dspy
from fastapi.testclient import TestClient

from tests.test_flows.conftest import (
    assistant_messages,
    default_program,
    new_session,
    post,
    wait_until,
)


def _orchestrator(client: TestClient, member_gate: threading.Event | None = None) -> dict[str, Any]:
    """Script the orchestrator's model to start one council through ``run_flow``."""

    from clio_agent.gact.flow_tools import start_flow

    started: dict[str, Any] = {}

    def program(question: str = "", **kwargs: Any) -> Any:
        if "Answer independently" in question and member_gate is not None:
            member_gate.wait(timeout=30)
        if question.startswith("convene the council") and not started:
            started.update(asyncio.run(start_flow("council", question, {"synthesizer": "self"})))
            return dspy.Prediction(answer=f"started {started['handle']}")
        return default_program(question=question, **kwargs)

    client.app.state.flow_program["run"] = program
    return started


def _snapshot(client: TestClient, handle: str) -> Any:
    return client.app.state.task_history.task_snapshot(handle)


def _run(client: TestClient, handle: str) -> dict[str, Any]:
    history = client.app.state.task_history
    checkpoint = history.workflow_checkpoint(handle)
    assert checkpoint is not None
    return history.read_content(checkpoint.content)


def _member_handles(client: TestClient, handle: str) -> list[str] | None:
    checkpoint = client.app.state.task_history.workflow_checkpoint(handle)
    if checkpoint is None:
        return None
    calls = client.app.state.task_history.read_content(checkpoint.content)["calls"]
    return [row["handle"] for row in calls.values()] if len(calls) == 3 else None


def _tree(client: TestClient, sid: str) -> list[dict[str, Any]]:
    response = client.get(f"/v1/sessions/{sid}/tasks/query", params={"limit": 200})
    assert response.status_code == 200, response.text
    return response.json()["tasks"]


def _council(client: TestClient) -> tuple[str, str, dict[str, Any]]:
    sid = new_session(client, "clio/orchestrator")
    started = _orchestrator(client)
    post(client, sid, "convene the council on the retention policy")
    wait_until(lambda: started.get("handle"))
    handle = started["handle"]
    wait_until(lambda: _snapshot(client, handle).status == "completed", timeout=90)
    return sid, handle, started


def test_run_flow_returns_the_handle_and_council_settles_under_it(flow_app: TestClient) -> None:
    sid, handle, started = _council(flow_app)
    assert started["accepted"] is True and started["flow"] == "council"
    assert started["kind"] == "Subagent"
    owner = flow_app.app.state.task_history.latest_assignment(sid)
    flow_task = _snapshot(flow_app, handle)
    assert flow_task.owner.parent_handle == owner.handle

    run = _run(flow_app, handle)
    assert run["flow"] == "council" and run["status"] == "completed" and run["depth"] == 1
    calls = list(run["calls"].values())
    assert len(calls) == 4 and {row["status"] for row in calls} == {"completed"}
    for row in calls:
        assert _snapshot(flow_app, row["handle"]).owner.parent_handle == handle
    assert run["result"]["synthesizer"] in {row["handle"] for row in calls}
    assert len(run["result"]["members"]) == 3

    # The flow's result reaches the starting session through its mailbox.
    wait_until(lambda: len(assistant_messages(flow_app, sid)) >= 2, timeout=60)
    # The flow's own session confirmed its input: one turn, nothing left to wake it.
    child_sid = flow_task.owner.session_id
    assert not flow_app.app.state.task_history.pending_mail(child_sid)
    assert len(assistant_messages(flow_app, child_sid)) == 1


def test_replaying_a_settled_flow_reattaches_to_its_handles(flow_app: TestClient) -> None:
    from clio_agent.flows.runner import run_agent_flow
    from clio_agent.gact import context

    sid, handle, _ = _council(flow_app)
    before = _run(flow_app, handle)
    tasks_before = sorted(row["handle"] for row in _tree(flow_app, sid))
    child_sid = _snapshot(flow_app, handle).owner.session_id
    module = flow_app.app.state.flow_modules[child_sid]

    def replay() -> Any:
        app_token = context.set_app(flow_app.app)
        session_token = context.set_session_id(child_sid)
        try:
            return run_agent_flow(module, {"question": "replay", "session_id": child_sid})
        finally:
            context.reset(session_token)
            context.reset(app_token)

    with ThreadPoolExecutor(1) as pool:
        prediction = pool.submit(replay).result(timeout=60)

    after = _run(flow_app, handle)
    assert prediction.answer == before["result"]["answer"]
    assert {k: v["handle"] for k, v in after["calls"].items()} == {
        k: v["handle"] for k, v in before["calls"].items()
    }
    assert sorted(row["handle"] for row in _tree(flow_app, sid)) == tasks_before


def test_cancelling_a_started_flow_settles_its_subtree(flow_app: TestClient) -> None:
    gate = threading.Event()
    sid = new_session(flow_app, "clio/orchestrator")
    started = _orchestrator(flow_app, gate)
    try:
        post(flow_app, sid, "convene the council on the incident")
        wait_until(lambda: started.get("handle"))
        handle = started["handle"]
        members = wait_until(lambda: _member_handles(flow_app, handle), timeout=60)
        response = flow_app.post(f"/v1/sessions/{sid}/tasks/cancel", json={"tasks": handle})
        assert response.status_code == 200, response.text
        assert response.json()["errors"] == []
    finally:
        gate.set()
    terminal = {"cancelled", "completed", "failed"}
    wait_until(lambda: _snapshot(flow_app, handle).status == "cancelled", timeout=60)
    for member in members:
        wait_until(lambda m=member: _snapshot(flow_app, m).status in terminal, timeout=60)
        assert _snapshot(flow_app, member).status == "cancelled"


def test_cancelling_a_flow_whose_members_stop_cooperatively_settles_its_subtree(
    flow_app: TestClient,
) -> None:
    """Live members stop mid-model-call: they raise the typed turn cancellation and the
    flow then sees failed calls. The whole subtree must still settle cancelled."""
    import time

    from clio_agent.agent import cancellation_requested
    from clio_agent.gact.agents.clio_react import _turn_cancelled
    from clio_agent.gact.flow_tools import start_flow

    started: dict[str, Any] = {}

    def program(question: str = "", **kwargs: Any) -> Any:
        if "Answer independently" in question:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if cancellation_requested():
                    raise _turn_cancelled()
                time.sleep(0.02)
        if question.startswith("convene the council") and not started:
            started.update(asyncio.run(start_flow("council", question, {"synthesizer": "self"})))
            return dspy.Prediction(answer=f"started {started['handle']}")
        return default_program(question=question, **kwargs)

    flow_app.app.state.flow_program["run"] = program
    sid = new_session(flow_app, "clio/orchestrator")
    post(flow_app, sid, "convene the council on the outage")
    wait_until(lambda: started.get("handle"))
    handle = started["handle"]
    members = wait_until(lambda: _member_handles(flow_app, handle), timeout=60)
    response = flow_app.post(f"/v1/sessions/{sid}/tasks/cancel", json={"tasks": handle})
    assert response.status_code == 200 and response.json()["errors"] == []

    wait_until(lambda: _snapshot(flow_app, handle).status == "cancelled", timeout=60)
    for member in members:
        wait_until(lambda m=member: _snapshot(flow_app, m).terminal_at is not None, timeout=60)
        assert _snapshot(flow_app, member).status == "cancelled"


def _plan_starter(client: TestClient, fail_once: set[str]) -> dict[str, Any]:
    """Script the orchestrator to start a three-node plan; nodes in ``fail_once`` fail
    on their first attempt only."""

    from clio_agent.gact.flow_tools import start_flow

    started: dict[str, Any] = {}
    attempts: dict[str, int] = {}
    plan = {
        "nodes": [
            {"id": "gather", "target": "self", "input": "STEP gather"},
            {"id": "measure", "target": "self", "input": "STEP measure", "after": ["gather"]},
            {"id": "report", "target": "self", "input": "STEP report", "after": ["measure"]},
        ]
    }

    def program(question: str = "", **kwargs: Any) -> Any:
        if question.startswith("STEP "):
            step = question.split()[1]
            attempts[step] = attempts.get(step, 0) + 1
            if step in fail_once and attempts[step] == 1:
                raise RuntimeError(f"{step} failed on its first attempt")
            return dspy.Prediction(answer=f"{step} done")
        if question.startswith("run the plan") and not started:
            started.update(asyncio.run(start_flow("plan", {"plan": plan, "task": "t"}, None)))
            started["attempts"] = attempts
            return dspy.Prediction(answer=f"started {started['handle']}")
        return default_program(question=question, **kwargs)

    client.app.state.flow_program["run"] = program
    return started


def _node_handles(client: TestClient, handle: str) -> dict[str, str]:
    sid = _snapshot(client, handle).owner.session_id
    view = client.get(f"/v1/sessions/{sid}/tasks/{handle}/flow").json()
    return {row["id"]: row.get("handle", "") for row in (view.get("view") or {}).get("nodes", [])}


def test_a_restarted_plan_keeps_finished_nodes_and_reruns_the_rest(flow_app: TestClient) -> None:
    sid = new_session(flow_app, "clio/orchestrator")
    started = _plan_starter(flow_app, {"measure"})
    post(flow_app, sid, "run the plan")
    handle = wait_until(lambda: started.get("handle"))
    wait_until(lambda: _snapshot(flow_app, handle).terminal_at is not None, timeout=60)
    assert _snapshot(flow_app, handle).status == "failed"
    before = _node_handles(flow_app, handle)
    plan_session = _snapshot(flow_app, handle).owner.session_id

    response = flow_app.post(f"/v1/sessions/{sid}/agent-controls/{handle}/restart")
    assert response.status_code == 200, response.text
    history = flow_app.app.state.task_history
    restarted = wait_until(
        lambda: (latest := history.latest_assignment(plan_session)).handle != handle and latest
    )
    wait_until(lambda: _snapshot(flow_app, restarted.handle).terminal_at is not None, timeout=60)
    assert _snapshot(flow_app, restarted.handle).status == "completed"
    after = _node_handles(flow_app, restarted.handle)
    # The finished node is the same task; only the failed one and its dependent ran again.
    assert after["gather"] == before["gather"]
    assert after["measure"] != before["measure"] and after["report"]
    assert started["attempts"] == {"gather": 1, "measure": 2, "report": 1}


def test_a_message_after_the_run_ended_does_not_run_the_flow_again(flow_app: TestClient) -> None:
    sid = new_session(flow_app, "clio/orchestrator")
    started = _plan_starter(flow_app, set())
    post(flow_app, sid, "run the plan")
    handle = wait_until(lambda: started.get("handle"))
    wait_until(lambda: _snapshot(flow_app, handle).status == "completed", timeout=60)
    plan_session = _snapshot(flow_app, handle).owner.session_id
    post(flow_app, plan_session, "one more thing")
    history = flow_app.app.state.task_history
    later = wait_until(
        lambda: (latest := history.latest_assignment(plan_session)).handle != handle
        and latest.terminal_at is not None
        and latest
    )
    assert started["attempts"] == {"gather": 1, "measure": 1, "report": 1}
    result = history.read_content(later.result)
    assert "already ended" in json.dumps(result) and "one more thing" in json.dumps(result)


def test_run_flow_describes_the_flows_this_session_resolves(monkeypatch: Any) -> None:
    """The model sees what each of its flows does and takes, not just a name."""

    from types import SimpleNamespace

    from clio_agent.gact import flow_tools

    rows = [
        {"id": "react", "description": "default"},
        {"id": "plan", "description": "plans"},
        {"id": "arena", "description": "Several contestants attempt the same task."},
        {"id": "council", "description": "Several members answer the same question."},
    ]
    monkeypatch.setattr(flow_tools, "_runtime", lambda: (object(), SimpleNamespace(session_id="s")))
    monkeypatch.setattr(flow_tools, "_flows", lambda app, sid: rows)
    desc = flow_tools.build_run_flow_tool().desc
    assert "- arena: Several contestants attempt the same task." in desc
    assert "- council: Several members answer the same question." in desc
    assert "- react:" not in desc and "- plan:" not in desc


def test_an_arena_returns_the_winner_with_the_judges_verdict(flow_app: TestClient) -> None:
    """Three contestants answer; the judge's choice, rationale and the other entries reach
    the caller in the answer, and the votes are recorded in the run."""

    from dspy.utils.dummies import DummyLM

    from clio_agent.gact.flow_tools import start_flow

    started: dict[str, Any] = {}
    count = {"n": 0}

    def program(question: str = "", **kwargs: Any) -> Any:
        if question.startswith("ENTRY"):
            count["n"] += 1
            return dspy.Prediction(answer=f"entry {count['n']}")
        if question.startswith("hold an arena") and not started:
            started.update(asyncio.run(start_flow("arena", "ENTRY please", None)))
            return dspy.Prediction(answer=f"started {started['handle']}")
        return default_program(question=question, **kwargs)

    flow_app.app.state.flow_program["run"] = program
    judge = DummyLM([{"reasoning": "r", "winner": "1", "rationale": "The second is clearest."}])
    dspy.configure(lm=judge)
    try:
        sid = new_session(flow_app, "clio/orchestrator")
        post(flow_app, sid, "hold an arena")
        handle = wait_until(lambda: started.get("handle"))
        wait_until(lambda: _snapshot(flow_app, handle).status == "completed", timeout=60)
    finally:
        dspy.configure(lm=None)
    history = flow_app.app.state.task_history
    answer = json.dumps(history.read_content(_snapshot(flow_app, handle).result))
    assert "Judge's choice: entry [1] of 3" in answer and "The second is clearest." in answer
    assert "Other entries" in answer
    run = _run(flow_app, handle)
    assert sum(1 for row in run["calls"].values() if row["kind"] == "agent") == 3


def test_restarting_a_flow_after_late_messages_resumes_its_last_run(
    flow_app: TestClient,
) -> None:
    """Late input leaves pass-on assignments after the run; a restart named on the
    flow's own handle still resumes the run, keeping its finished nodes."""

    sid = new_session(flow_app, "clio/orchestrator")
    started = _plan_starter(flow_app, {"measure"})
    post(flow_app, sid, "run the plan")
    handle = wait_until(lambda: started.get("handle"))
    wait_until(lambda: _snapshot(flow_app, handle).terminal_at is not None, timeout=60)
    before = _node_handles(flow_app, handle)
    plan_session = _snapshot(flow_app, handle).owner.session_id
    history = flow_app.app.state.task_history
    post(flow_app, plan_session, "a late note")
    passed_on = wait_until(
        lambda: (latest := history.latest_assignment(plan_session)).handle != handle
        and latest.terminal_at is not None
        and latest
    )

    response = flow_app.post(f"/v1/sessions/{sid}/agent-controls/{handle}/restart")
    assert response.status_code == 200, response.text
    restarted = wait_until(
        lambda: (latest := history.latest_assignment(plan_session)).handle
        not in {handle, passed_on.handle}
        and latest
    )
    wait_until(lambda: _snapshot(flow_app, restarted.handle).terminal_at is not None, timeout=60)
    assert _snapshot(flow_app, restarted.handle).status == "completed"
    after = _node_handles(flow_app, restarted.handle)
    assert after["gather"] == before["gather"] and after["measure"] != before["measure"]
    assert started["attempts"] == {"gather": 1, "measure": 2, "report": 1}


def test_each_restart_keeps_everything_finished_before_it(flow_app: TestClient) -> None:
    sid = new_session(flow_app, "clio/orchestrator")
    started = _plan_starter(flow_app, {"measure", "report"})
    post(flow_app, sid, "run the plan")
    handle = wait_until(lambda: started.get("handle"))
    plan_session = _snapshot(flow_app, handle).owner.session_id
    history = flow_app.app.state.task_history
    seen = {handle}
    for _ in range(2):
        latest = wait_until(
            lambda: (task := history.latest_assignment(plan_session)).terminal_at is not None
            and task
        )
        assert latest.status == "failed"
        response = flow_app.post(f"/v1/sessions/{sid}/agent-controls/{handle}/restart")
        assert response.status_code == 200, response.text
        seen.add(latest.handle)
        wait_until(lambda: history.latest_assignment(plan_session).handle not in seen)
    final = wait_until(
        lambda: (task := history.latest_assignment(plan_session)).terminal_at is not None and task
    )
    assert final.status == "completed"
    assert started["attempts"] == {"gather": 1, "measure": 2, "report": 2}
