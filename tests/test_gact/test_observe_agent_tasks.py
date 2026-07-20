"""observe_agent_tasks — cursor-based incremental child observation (#1000).

The OBSERVE posture: read a child's event stream from a resumable cursor without
consuming it, with optional regex pattern-return and bounded excerpts. Mirrors
clio-relay's ``relay_observe`` semantics on clio-agent's spawn substrate.

Covers (failing-first / sabotage-checked): cursor resume (no repeat / no gap;
sabotage = cursor ignored), pattern early-return vs no-pattern-at-timeout,
non-consumption (notify_pending survives observe; a later wait collects + emits the
terminal exactly once; sabotage = observe consuming), invalid regex → typed row,
bounded excerpts + truncation note, and tool gating on declared children.
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import context as ctx
from clio_agent.gact.agent_tasks import (
    STATUS_RUNNING,
    AgentTask,
    persist_agent_task,
)
from clio_agent.gact.app import build_app
from clio_agent.gact.events import Event
from clio_agent.gact.runtime.globals import _gact_app_context

pytestmark = pytest.mark.usefixtures("host_agent_executor")


# --------------------------------------------------------------------------- #
# Harness                                                                       #
# --------------------------------------------------------------------------- #


class _Agent:
    def forward(self, question: str, session_id: str, **_kw: Any) -> Any:
        return SimpleNamespace(answer="ok", selected_expert="", routing_rationale="")


class _Def:
    def __init__(self, agent_id: str) -> None:
        self.id = agent_id
        self.metadata = {"agent_blueprint_id": "bp"}
        self.fanout = None


def _declare(monkeypatch, *child_ids: str) -> None:
    monkeypatch.setattr(
        "clio_agent.gact.agents.resolution._runtime_declared_child_ids",
        lambda app, pid, session_id="": set(child_ids),
    )


@contextmanager
def _active_turn(app: Any, session_id: str) -> Iterator[None]:
    with _gact_app_context(app):
        token = ctx.set_session_id(session_id)
        try:
            yield
        finally:
            ctx.reset(token)


def _tools(app: Any, parent: str) -> dict[str, Any]:
    from clio_agent.gact.agents import spawn_runtime

    with _active_turn(app, parent):
        return {t.name: t for t in spawn_runtime.build_spawn_runtime_tools(_Agent(), _Def("main"))}


def _observe(app: Any, parent: str, tools: dict[str, Any], **kw: Any) -> dict[str, Any]:
    with _active_turn(app, parent):
        return json.loads(tools["observe_agent_tasks"].func(**kw))


def _emit(app: Any, sid: str, event_type: str, *, status: str = "completed",
          summary: str = "", payload: dict[str, Any] | None = None) -> None:
    """Publish one bus event shaped like the SSE ``semantic.event`` projection the
    child's react loop produces (event_type/status/summary + body ``payload``)."""

    app.state.bus.publish(
        Event(
            type="semantic.event",
            session_id=sid,
            payload={
                "event_type": event_type,
                "status": status,
                "summary": summary,
                "payload": payload or {},
            },
        )
    )


def _seed_running_task(app: Any, parent: str, *, task_id: str = "task_run",
                       expert: str = "data_expert") -> AgentTask:
    child = app.state.sessions.create(
        workspace_id="ws_default", title="c", parent_session_id=parent
    )
    task = AgentTask(
        task_id=task_id,
        parent_session_id=parent,
        child_session_id=child.id,
        agent_ref={"expert_id": expert, "requesting_expert_id": "main"},
        status=STATUS_RUNNING,
        run_index=0,
        created_at="2026-07-20T00:00:00+00:00",
        updated_at="2026-07-20T00:00:00+00:00",
    )
    persist_agent_task(app, task)
    return task


def _seed_terminal_task(app: Any, parent: str, *, task_id: str = "task_done",
                        expert: str = "data_expert") -> AgentTask:
    from clio_agent.gact.agent_tasks import STATUS_COMPLETED

    child = app.state.sessions.create(
        workspace_id="ws_default", title="c", parent_session_id=parent
    )
    task = AgentTask(
        task_id=task_id,
        parent_session_id=parent,
        child_session_id=child.id,
        agent_ref={"expert_id": expert, "requesting_expert_id": "main"},
        status=STATUS_COMPLETED,
        notify_pending=True,
        run_index=0,
        result={"answer_excerpt": "done", "message_ref": "msg_x", "workflow_state": {}},
        created_at="2026-07-20T00:00:00+00:00",
        updated_at="2026-07-20T00:00:00+00:00",
    )
    persist_agent_task(app, task)
    return task


def _from_parent(row_list: list[dict[str, Any]], tid: str) -> dict[str, Any]:
    return next(r for r in row_list if r["task_id"] == tid)


# --------------------------------------------------------------------------- #
# 1. Tool gating                                                               #
# --------------------------------------------------------------------------- #


def test_observe_tool_present_only_with_declared_children(tmp_path: Path, monkeypatch) -> None:
    from clio_agent.gact.agents import spawn_runtime

    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app):
        with _active_turn(app, "sess_x"):
            monkeypatch.setattr(
                "clio_agent.gact.agents.resolution._runtime_declared_child_ids",
                lambda app, pid, session_id="": set(),
            )
            assert spawn_runtime.build_spawn_runtime_tools(_Agent(), _Def("main")) == []
        _declare(monkeypatch, "data_expert")
        names = {t.name for t in _tools(app, "sess_x").values()}
    assert "observe_agent_tasks" in names


# --------------------------------------------------------------------------- #
# 2. Cursor semantics — resume, no repeat, no gap (sabotage: cursor ignored)   #
# --------------------------------------------------------------------------- #


def test_cursor_resume_no_repeat_no_gap(tmp_path: Path, monkeypatch) -> None:
    _declare(monkeypatch, "data_expert")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        task = _seed_running_task(app, parent)
        tools = _tools(app, parent)

        _emit(app, task.child_session_id, "react.step.completed",
              summary="step 0", payload={"thought": "look up station", "tool_name": "search"})
        _emit(app, task.child_session_id, "react.step.completed",
              summary="step 1", payload={"thought": "stage csv", "tool_name": "stage"})

        first = _observe(app, parent, tools, task_ids=[task.task_id], cursor=1)
        row1 = _from_parent(first["tasks"], task.task_id)
        seqs_1 = [e["seq"] for e in row1["new_events"]]
        assert len(seqs_1) == 2, "first observe returns both existing events"
        next_cursor = first["next_cursor"]
        assert next_cursor == max(seqs_1) + 1

        # A third event lands AFTER the first read.
        _emit(app, task.child_session_id, "react.step.completed",
              summary="step 2", payload={"thought": "download", "tool_name": "get"})

        second = _observe(app, parent, tools, task_ids=[task.task_id], cursor=next_cursor)
        row2 = _from_parent(second["tasks"], task.task_id)
        seqs_2 = [e["seq"] for e in row2["new_events"]]
        # Resume: exactly the ONE new event, and none of the first batch repeated.
        assert len(seqs_2) == 1, "resume returns only events after the cursor (no re-read)"
        assert not set(seqs_1) & set(seqs_2), "sabotage lock: a repeated seq means cursor ignored"
        assert min(seqs_2) >= next_cursor, "no gap: resumed events start at/after next_cursor"


# --------------------------------------------------------------------------- #
# 3. Pattern early-return vs no-pattern-at-timeout                             #
# --------------------------------------------------------------------------- #


def test_pattern_returns_early_before_terminal(tmp_path: Path, monkeypatch) -> None:
    _declare(monkeypatch, "data_expert")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        task = _seed_running_task(app, parent)  # stays RUNNING (never terminal)
        tools = _tools(app, parent)

        def _slow_emit() -> None:
            time.sleep(0.2)
            _emit(app, task.child_session_id, "expert.extract.completed", summary="landed",
                  payload={"structured": {"workflow_state": {"station_id": "AZ.MONP"}}})

        t = threading.Thread(target=_slow_emit)
        t.start()
        start = time.monotonic()
        out = _observe(app, parent, tools, task_ids=[task.task_id], cursor=1,
                       pattern=r"AZ\.MONP", timeout_s=5.0)
        elapsed = time.monotonic() - start
        t.join()

        assert out["matched"] is True, "pattern must return matched=true on the landed evidence"
        assert elapsed < 4.0, "must return EARLY on match, not block the full timeout"
        row = _from_parent(out["tasks"], task.task_id)
        assert row["status"] == STATUS_RUNNING, "returned BEFORE the child terminal"
        assert any(e.get("matched") for e in row["new_events"])


def test_no_pattern_blocks_until_timeout_returns_events_so_far(tmp_path: Path, monkeypatch) -> None:
    _declare(monkeypatch, "data_expert")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        task = _seed_running_task(app, parent)
        tools = _tools(app, parent)

        def _slow_emit() -> None:
            time.sleep(0.15)
            _emit(app, task.child_session_id, "react.step.completed", summary="mid",
                  payload={"thought": "progress", "tool_name": "stage"})

        t = threading.Thread(target=_slow_emit)
        t.start()
        start = time.monotonic()
        out = _observe(app, parent, tools, task_ids=[task.task_id], cursor=1, timeout_s=0.6)
        elapsed = time.monotonic() - start
        t.join()

        assert out["matched"] is False
        assert elapsed >= 0.5, "without a pattern it blocks the full timeout (child still running)"
        row = _from_parent(out["tasks"], task.task_id)
        assert row["new_events"], "returns the events accumulated during the window"


# --------------------------------------------------------------------------- #
# 4. Non-consumption (sabotage: observe consuming)                            #
# --------------------------------------------------------------------------- #


def test_observe_never_consumes_wait_still_collects_once(tmp_path: Path, monkeypatch) -> None:
    from clio_agent.gact.agents import spawn_runtime

    _declare(monkeypatch, "data_expert")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        task = _seed_terminal_task(app, parent)
        _emit(app, task.child_session_id, "expert.extract.completed", summary="fin",
              payload={"output": "the staged CSV is ready"})
        tools = _tools(app, parent)

        # Observe repeatedly — the read-only sibling never collects.
        for _ in range(3):
            _observe(app, parent, tools, task_ids=[task.task_id], cursor=1)
        rec = app.state.agent_task_registry.get(task.task_id)
        assert rec.notify_pending is True, "sabotage lock: observe must NOT consume (notify_pending)"
        assert not rec.consumed_at
        assert not any(
            e.type == "semantic.event"
            and (e.payload or {}).get("event_type", "").startswith("blueprint.delegation")
            for e in app.state.bus._history.get(parent, [])
        ), "observe emits no delegation terminal on the parent wire"

        # A later wait DOES collect + emits the terminal exactly once.
        events: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "clio_agent.gact.agents.spawn_runtime._emit_semantic_event",
            lambda app, sid, event_type, **kw: (events.append({"event_type": event_type}) or {}),
        )
        monkeypatch.setattr(
            "clio_agent.gact.agents.spawn_runtime._append_live_assistant_part",
            lambda *a, **k: None,
        )
        with _active_turn(app, parent):
            tools["wait_agent_tasks"].func(task_ids=[task.task_id], timeout_s=2.0)
        assert [e["event_type"] for e in events] == [
            "blueprint.delegation.completed",
            "blueprint.delegation.parent_resumed",
        ], "wait after observe emits the terminal exactly once"
        collected = app.state.agent_task_registry.get(task.task_id)
        assert collected.notify_pending is False and collected.consumed_at
        assert spawn_runtime is not None  # import kept for the patch targets


# --------------------------------------------------------------------------- #
# 5. Regex safety + bounded excerpts                                          #
# --------------------------------------------------------------------------- #


def test_invalid_regex_returns_typed_row(tmp_path: Path, monkeypatch) -> None:
    _declare(monkeypatch, "data_expert")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        task = _seed_running_task(app, parent)
        tools = _tools(app, parent)
        out = _observe(app, parent, tools, task_ids=[task.task_id], cursor=1,
                       pattern="(unclosed[", timeout_s=None)
    assert out["error"] == "invalid_pattern"
    assert "pattern" in out and out["tasks"] == []


def test_huge_event_text_is_bounded_with_truncation_note(tmp_path: Path, monkeypatch) -> None:
    from clio_agent.gact.agents.observe_runtime import OBSERVE_EXCERPT_MAX_CHARS

    _declare(monkeypatch, "data_expert")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        task = _seed_running_task(app, parent)
        tools = _tools(app, parent)
        huge = "X" * 50_000
        _emit(app, task.child_session_id, "react.step.completed", summary="big",
              payload={"thought": huge, "tool_name": "noop"})
        out = _observe(app, parent, tools, task_ids=[task.task_id], cursor=1)
    row = _from_parent(out["tasks"], task.task_id)
    excerpt = row["new_events"][0]["excerpt"]
    assert len(excerpt) < 50_000, "excerpt must be bounded, not a raw dump"
    assert len(excerpt) <= OBSERVE_EXCERPT_MAX_CHARS + 64
    assert "chars]" in excerpt and row["new_events"][0].get("truncated") is True


# --------------------------------------------------------------------------- #
# 6. Unknown task + workflow_state snapshot                                    #
# --------------------------------------------------------------------------- #


def test_unknown_task_returns_typed_row_and_state_snapshot(tmp_path: Path, monkeypatch) -> None:
    _declare(monkeypatch, "data_expert")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        task = _seed_running_task(app, parent)
        _emit(app, task.child_session_id, "expert.extract.completed", summary="s",
              payload={"structured": {"workflow_state": {"selected_station": "CI.PASA"}}})
        tools = _tools(app, parent)
        out = _observe(app, parent, tools, task_ids=[task.task_id, "task_missing"],
                       cursor=1, include_state=True)
    unknown = _from_parent(out["tasks"], "task_missing")
    assert unknown["error"] == "unknown_task"
    known = _from_parent(out["tasks"], task.task_id)
    assert known["workflow_state"] == {"selected_station": "CI.PASA"}
