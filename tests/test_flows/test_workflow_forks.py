"""A workflow step forks itself: the fork runs beside it, and its dependents wait on both."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from clio_agent.flows.run_view import flow_run
from tests.test_flows.conftest import assistant_messages, new_session, post, wait_until


def _done(client: TestClient, sid: str) -> dict[str, Any]:
    return wait_until(lambda: (assistant_messages(client, sid) or [None])[0], timeout=120)


def _workflow(client: TestClient, sid: str) -> tuple[str, dict[str, Any]]:
    """The workflow run's owning handle and checkpoint (in the child running it)."""

    history = client.app.state.task_history
    owner = history.latest_assignment(sid)
    outer = history.read_content(history.workflow_checkpoint(owner.handle).content)
    row = next(r for r in outer["calls"].values() if r["kind"] == "flow")
    child = history.task_snapshot(row["handle"])
    return child.handle, history.read_content(history.workflow_checkpoint(child.handle).content)


def test_a_forked_step_is_drawn_beside_it_and_its_dependent_reads_both(
    flow_app: TestClient,
) -> None:
    sid = new_session(flow_app, "clio/forker")
    post(flow_app, sid, "map the coast")
    _done(flow_app, sid)
    handle, run = _workflow(flow_app, sid)
    history = flow_app.app.state.task_history
    assert run["status"] == "completed"
    [forked] = flow_app.app.state.fork_results
    assert forked["forked"] is True and forked["label"] == "draft~1"
    fork = history.task_snapshot(forked["handle"])
    draft = next(row for row in run["view"]["steps"] if row["id"] == "draft")
    assert fork.fork_of == draft["handle"] and fork.owner.parent_handle == handle
    assert fork.description == "Pursue the other branch" and fork.status == "completed"
    # The fork starts where the step was: its conversation carries the step's turn.
    fork_session = flow_app.app.state.sessions.get(fork.owner.session_id)
    assert fork_session.metadata["subagent_name"] == "draft~1"
    assert fork_session.metadata["workflow_step_of"]["step"] == "draft"
    # The dependent waited on the step and its fork, and $draft read both answers.
    merge = history.task_snapshot(
        next(row for row in run["view"]["steps"] if row["id"] == "merge")["handle"]
    )
    assert "[draft]" in merge.description and "[draft~1]" in merge.description
    assert "Pursue the other branch" in merge.description
    events = [row["event"] for row in run["events"].values()]
    assert "fork_completed" in events
    # The run view draws the fork beside its step.
    view = flow_run(history, handle)["view"]
    ids = [row["id"] for row in view["steps"]]
    assert ids.index("draft~1") == ids.index("draft") + 1
    row = next(row for row in view["steps"] if row["id"] == "draft~1")
    assert row["fork_of"] == "draft" and row["status"] == "completed"
    assert row["note"] == "Pursue the other branch"


def test_a_fork_beyond_the_workflows_growth_bound_is_refused(flow_app: TestClient) -> None:
    sid = new_session(flow_app, "clio/forker")
    post(flow_app, sid, "bounded map")
    _done(flow_app, sid)
    [refused] = flow_app.app.state.fork_results
    assert refused == {
        "forked": False,
        "refused": True,
        "reason": "max_added_reached",
        "error": "the workflow may grow by at most 0 steps",
    }
    _handle, run = _workflow(flow_app, sid)
    assert [row["id"] for row in run["view"]["steps"]] == ["draft", "merge"]


def test_approve_edits_asks_before_a_step_forks(flow_app: TestClient) -> None:
    sid = new_session(flow_app, "clio/forker")
    post(flow_app, sid, "approve the map")
    question = wait_until(
        lambda: next(
            (
                row
                for row in flow_app.get("/v1/questions", params={"status": "pending"}).json()[
                    "questions"
                ]
                if row["prompt"].startswith("Fork step 'draft' as 'draft~1'")
            ),
            None,
        ),
        timeout=60,
    )
    assert question["prompt"] == ("Fork step 'draft' as 'draft~1': Pursue the other branch. Allow?")
    answered = flow_app.post(
        f"/v1/sessions/{question['session_id']}/questions/{question['id']}/answer",
        json={"answer": "No", "selected_options": ["no"]},
    )
    assert answered.status_code in (200, 202), answered.text
    _done(flow_app, sid)
    [refused] = flow_app.app.state.fork_results
    assert refused["reason"] == "not_approved"


def test_a_delegated_childs_forks_run_beside_it_within_the_per_task_limit(
    flow_app: TestClient, monkeypatch: Any
) -> None:
    """Outside a workflow a fork is a sibling of its child; the parent's wait covers it,
    and Forks per task refuses the one past the limit with ``fork_limit_reached``."""

    monkeypatch.setenv("CLIO_MAX_FORKS_PER_TASK", "1")
    sid = new_session(flow_app, "clio/childforkerer")
    post(flow_app, sid, "ideas")
    message = _done(flow_app, sid)
    first, second = flow_app.app.state.fork_results
    assert first["forked"] is True and first["kind"] == "task"
    assert second["reason"] == "fork_limit_reached" and second["forked"] is False
    history = flow_app.app.state.task_history
    fork = history.task_snapshot(first["handle"])
    original = history.task_snapshot(fork.fork_of)
    assert fork.owner.parent_handle == original.owner.parent_handle
    # The flow's wait on its child covered the fork: both answers came back.
    text = " ".join(part.get("text", "") for part in message.get("parts", []))
    assert "first idea" in text and "done: Try the second idea" in text


def test_an_agent_forks_a_root_conversation_into_a_new_branch(flow_app: TestClient) -> None:
    sid = new_session(flow_app, "clio/orchestrator")
    post(flow_app, sid, "FORKME please")
    _done(flow_app, sid)
    [forked] = flow_app.app.state.fork_results
    assert forked["kind"] == "conversation" and forked["forked"] is True
    branch = flow_app.app.state.sessions.get(forked["session_id"])
    assert branch.parent_session_id == sid and branch.title.endswith("(fork)")
    origin = branch.metadata["fork_origin"]
    assert origin["author"]["kind"] == "agent" and origin["note"] == "Try the second idea"
    # The branch carries the conversation so far and runs the note as its next input.
    messages = wait_until(
        lambda: len(assistant_messages(flow_app, branch.id))
        and assistant_messages(flow_app, branch.id)
    )
    assert messages
    stored = flow_app.app.state.messages.get(branch.id, [])
    assert any(m.role == "user" and "FORKME" in str(m.parts) for m in stored)
    assert any(m.role == "user" and "Try the second idea" in str(m.parts) for m in stored)


def test_an_isolated_run_gives_each_step_and_fork_its_own_folder(
    flow_app: TestClient, monkeypatch: Any
) -> None:
    from pathlib import Path

    from clio_agent.gact import node_folders

    # The OS fence is qualified live; here it is reported as able to narrow.
    monkeypatch.setattr(node_folders, "isolation_refusal", lambda: None)
    sid = new_session(flow_app, "clio/forker")
    post(flow_app, sid, "isolated map")
    _done(flow_app, sid)
    _handle, run = _workflow(flow_app, sid)
    assert run["status"] == "completed"
    sessions = flow_app.app.state.sessions
    history = flow_app.app.state.task_history
    folders = {
        row["id"]: sessions.get(history.task_snapshot(row["handle"]).owner.session_id).metadata[
            "node_folder"
        ]
        for row in [*run["view"]["steps"], *run["view"]["forks"]]
    }
    draft, fork, merge = (Path(folders[name]) for name in ("draft", "draft~1", "merge"))
    assert draft.parent == merge.parent == fork.parent
    assert draft.parent.parent.name == "runs" and draft.name == "draft"
    assert fork.name == "draft~1"
    # The fork started with a copy of its step's folder, made a recorded way.
    assert (fork / "notes.txt").read_text(encoding="utf-8") == "first half"
    assert (draft / "notes.txt").stat().st_ino != (fork / "notes.txt").stat().st_ino
    [forked] = flow_app.app.state.fork_results
    record = sessions.get(forked["session_id"]).metadata["folder_copy"]
    assert record["method"] in {"clone", "copy"} and record["from"] == str(draft)
    assert record["cloned"] + record["copied"] == 1


def test_an_isolated_run_without_a_fence_stalls_at_start(
    flow_app: TestClient, monkeypatch: Any
) -> None:
    from clio_agent.gact.node_folders import ISOLATION_UNCONFINED
    from clio_agent.runtime import sandbox

    monkeypatch.setattr(sandbox, "current_state", lambda: None)

    sid = new_session(flow_app, "clio/forker")
    post(flow_app, sid, "isolated map")
    _done(flow_app, sid)
    _handle, run = _workflow(flow_app, sid)
    assert run["status"] == "flow_stall" and ISOLATION_UNCONFINED in str(run)
    assert not run.get("calls")
    assert not getattr(flow_app.app.state, "fork_results", None)
