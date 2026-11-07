"""Visible asynchronous tool work can yield without a fabricated final answer."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import context
from clio_agent.gact.app import build_app
from clio_agent.gact.task_declarations import build_ask_user_tool
from clio_agent.tools.execution import current_tool_runtime
from tests.turn_signals import wait_for_terminal_status

pytestmark = pytest.mark.usefixtures("host_agent_executor", "no_session_facts")


@pytest.mark.parametrize("disposition", [None, "yield"], ids=["default", "explicit-yield"])
def test_question_tool_output_yields_without_an_empty_response_error(
    tmp_path: Path,
    disposition: str | None,
) -> None:
    """The real Question remains open under the original finite assignment."""
    handles: list[str] = []

    class Model:
        def forward(self, question: str, session_id: str) -> Any:
            """Post a real Question; replace only the test's model response."""
            app = context.active_app()
            installed = current_tool_runtime().tool_observer
            assert app is not None and installed is not None
            observer = cast(
                Callable[[str, dict[str, Any], str | None, str | None, Any], Any], installed
            )
            arguments = {"question": "Which sample should I analyze?"}
            observer("ask_user", arguments, "started", None, None)
            value = asyncio.run(
                app.state.builtin_boundary.bind(build_ask_user_tool()).acall(**arguments)
            )
            observer("ask_user", arguments, "completed", None, value)
            assert value["accepted"] and value["kind"] == "Question"
            handles.append(value["handle"])
            return SimpleNamespace(
                answer="",
                selected_expert="main",
                routing_rationale="",
                assignment_disposition=disposition,
            )

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=Model())
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "Question yield"}).json()["id"]
        cursor = app.state.bus.latest_event_id(sid)
        accepted = client.post(
            f"/v1/sessions/{sid}/messages", json={"text": "Ask for the sample and yield."}
        )
        assert accepted.status_code == 202, accepted.text
        wait_for_terminal_status(app.state.bus, sid, after_event_id=cursor)
        messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        [message] = [
            row
            for row in messages
            if row["role"] == "assistant" and row.get("turn_id") == accepted.json()["message_id"]
        ]
        assert message.get("error_info") is None
        assert message["stop_reason"] == "end_turn"
        assert [part["type"] for part in message["parts"]] == ["tool_call", "tool_result"]
        assert len(handles) == 1
        question = app.state.task_history.task_snapshot(handles[0])
        assignment = app.state.task_history.latest_assignment(sid)
        assert question is not None and question.raw_status == "pending"
        assert question.terminal_at is None and question.recording == "confirmed"
        assert assignment is not None and question.owner.assignment_id == assignment.handle
        assert assignment.terminal_at is None and assignment.activity == "yielded"
