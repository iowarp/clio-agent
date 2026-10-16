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


def test_completed_tool_is_delivered_and_retained_when_trace_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
        assert tool_observer._OBSERVER_CALL_T0.value is None
        parts = app.state.live_assistant_parts[sid]
        assert any(
            part.type == "tool_result" and part.call_id == completed[0]["call_id"] for part in parts
        )
        assert len(app.state.tool_call_ledger[sid]) == 1
