"""An observed command completion survives a failure to record its trace."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import tool_observer
from clio_agent.gact.app import build_app
from clio_agent.gact.runtime.globals import _gact_app_context, _tool_session_context

pytestmark = pytest.mark.usefixtures("host_agent_executor")


@pytest.mark.parametrize("active_transcript", [False, True])
def test_completed_tool_is_delivered_and_retained_when_trace_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, active_transcript: bool
) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    events: list[Any] = []
    real_publish = app.state.bus.publish

    def publish(event: Any) -> None:
        events.append(event)
        real_publish(event)

    monkeypatch.setattr(app.state.bus, "publish", publish)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "storage failure"}).json()["id"]
        if active_transcript:
            tool_observer._open_turn_transcript(app, sid, "turn_storage_failure")
        observer = tool_observer._make_tool_observer(app)
        with _gact_app_context(app), _tool_session_context(sid):
            observer("run", {"command": "write test output"}, "started", None)

            def refuse(*args: Any, **kwargs: Any) -> None:
                raise RuntimeError("native storage write refused")

            monkeypatch.setattr(tool_observer, "_emit_semantic_event", refuse)
            with pytest.raises(RuntimeError, match="native storage write refused"):
                observer(
                    "run",
                    {"command": "write test output"},
                    "completed",
                    None,
                    {"exit_code": 0, "stdout": "components=26"},
                )
        completed = [event.payload for event in events if event.type == "tool.call.completed"]
        assert len(completed) == 1
        assert completed[0]["ok"] is True
        assert completed[0]["recording_status"] == "failed"
        assert "components=26" in str(completed[0]["result"])
        assert tool_observer._OBSERVER_CALL_T0.get() is None
        parts = app.state.live_assistant_parts[sid]
        assert any(
            part.type == "tool_result" and part.call_id == completed[0]["call_id"] for part in parts
        )
        assert len(app.state.tool_call_ledger[sid]) == 1


@pytest.mark.usefixtures("builtin_tool_context")
@pytest.mark.parametrize(
    ("interrupted", "reason"),
    [
        (None, "Interrupted by session cancellation"),
        ("mail_received", "Interrupted by mail"),
        ("stopped", "Interrupted by a stop request"),
    ],
)
def test_cancelled_wait_completion_preserves_snapshot_as_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupted: str | None, reason: str
) -> None:
    """Stop changes wait presentation, not the truthful result or tool success."""
    from clio_agent.gact.agents.tool_instrumentation import instrument_tools
    from clio_agent.gact.task_declarations import build_task_tools

    app = build_app(sessions_path=tmp_path / "sessions.json")
    wait_tool = next(tool for tool in build_task_tools() if tool.name == "wait_tasks")
    instrument_tools([wait_tool])
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "interrupted wait"}).json()["id"]
        observer = tool_observer._make_tool_observer(app)
        semantic_events: list[dict[str, Any]] = []
        monkeypatch.setattr(
            tool_observer,
            "_emit_semantic_event",
            lambda *args, **kwargs: semantic_events.append(kwargs),
        )
        args = {"tasks": ["child"], "return_when": "all", "timeout_s": None}
        result = {"outcomes": [], "pending": ["child"], "timed_out": False}
        if interrupted:
            result["interrupted"] = interrupted
        with _gact_app_context(app), _tool_session_context(sid):
            observer("wait_tasks", args, "started", None)
            app.state.cancel_flags.add(sid)
            observer("wait_tasks", args, "completed", None, result)

        event = next(
            event
            for event in reversed(app.state.bus._history[sid])
            if event.type == "tool.call.completed" and event.payload.get("tool") == "wait_tasks"
        )
        assert event.payload["ok"] is True
        assert event.payload.get("error") is None
        assert event.payload["presentation"]["status"] == "interrupted"
        assert reason in event.payload["presentation"]["diagnostic"]
        assert "execution-error" not in {
            block["id"] for block in event.payload["presentation"]["blocks"]
        }
        assert event.payload["result"]["pending"] == ["child"]
        assert event.payload["result"].get("interrupted") == interrupted
        result_part = next(
            part
            for part in app.state.live_assistant_parts[sid]
            if part.type == "tool_result" and part.call_id == event.payload["call_id"]
        )
        assert result_part.is_error is False
        assert result_part.presentation["status"] == "interrupted"
        semantic_completion = semantic_events[-1]
        assert semantic_completion["status"] == "interrupted"
        assert semantic_completion["summary"] == "Tool wait_tasks interrupted."


def test_cancelled_non_wait_tool_remains_an_execution_failure(tmp_path: Path) -> None:
    """The wait exception cannot reclassify writes or other tool completions."""
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "cancelled tool"}).json()["id"]
        observer = tool_observer._make_tool_observer(app)
        args = {"command": "write a file"}
        with _gact_app_context(app), _tool_session_context(sid):
            observer("write_file", args, "started", None)
            app.state.cancel_flags.add(sid)
            observer("write_file", args, "completed", None, {"written": True})
        event = next(
            event
            for event in reversed(app.state.bus._history[sid])
            if event.type == "tool.call.completed" and event.payload.get("tool") == "write_file"
        )
        assert event.payload["ok"] is False
        assert event.payload["error"] == "tool call completed after session cancellation"
