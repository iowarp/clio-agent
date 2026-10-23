"""A native built-in call is one ``tools_called`` row with the tool's own arguments.

The agent's recording wrapper and the live observer both report every native
call; ``_merge_tool_call_rows`` folds the two into one row by name and
arguments. When the observed callable took ``**keywords`` (the recording
wrapper's ``call_tool(**kwargs)``), the observer reported the call nested under
the container's name (``{"kwargs": {...}}``), the two rows never merged, and
the assistant message listed the call twice.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import dspy
import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import context
from clio_agent.gact.agents.tool_instrumentation import instrument_tools, native_tool
from clio_agent.gact.agents.tool_recording import recording_tool
from clio_agent.gact.app import build_app
from clio_agent.gact.runtime.globals import _gact_app_context, _tool_session_context
from clio_agent.gact.tool_observer import _install_tool_runtime_hooks, _merge_tool_call_rows
from tests.test_gact.task_records import active_turn_fixture, record_assignment_fixture

pytestmark = pytest.mark.usefixtures("host_agent_executor")


def _tools_called(tmp_path: Path, tool: Any, call: dict[str, Any]) -> list[dict[str, Any]]:
    """Run one recorded, instrumented call and merge its rows as a turn does."""
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t", "approval_mode": "bypass"}).json()[
            "id"
        ]
        record_assignment_fixture(app, sid)
        _install_tool_runtime_hooks(app)
        with _gact_app_context(app):
            (tool,) = instrument_tools([recording_tool(tool)])
        rows: list[dict[str, Any]] = []
        token = context.set_agent_tool_rows(rows)
        try:
            with active_turn_fixture(app, sid), _tool_session_context(sid):
                tool(**call)
        finally:
            context.reset(token)
        observed = app.state.tool_call_ledger.pop(sid, [])
    assert [row["telemetry_source"] for row in rows] == ["agent_react_tool_wrapper"]
    assert [row["telemetry_source"] for row in observed] == ["live_observer"]
    return _merge_tool_call_rows(rows, observed)


def test_native_builtin_call_is_one_row_with_its_own_arguments(tmp_path: Path) -> None:
    def recall_probe(query: str = "", ids: list[str] | None = None, limit: int = 10) -> str:
        """Read back earlier context."""
        return f"recalled {query}"

    tool = native_tool(
        recall_probe,
        name="recall_probe",
        desc="Read back earlier context.",
        args={
            "query": {"type": "string"},
            "ids": {"type": "array", "items": {"type": "string"}},
            "limit": {"type": "integer"},
        },
        presentation="memory",
        domain="memory",
        read_only=True,
    )
    call = {"ids": None, "query": "anything", "limit": 10}

    (row,) = _tools_called(tmp_path, tool, call)

    assert row["name"] == "recall_probe"
    assert row["args"] == call
    assert row["ok"] is True
    assert row["call_id"]


def test_keyword_container_is_reported_as_the_call_arguments(tmp_path: Path) -> None:
    """A recorded plain tool is observed through ``call_tool(**kwargs)``: still its own names."""

    def search(**options: Any) -> str:
        return f"searched {options['query']}"

    tool = dspy.Tool(
        func=search,
        name="keyword_probe",
        desc="Search.",
        args={"query": {"type": "string"}, "limit": {"type": "integer"}},
    )
    call = {"query": "anything", "limit": 10}

    (row,) = _tools_called(tmp_path, tool, call)

    assert row["args"] == call
