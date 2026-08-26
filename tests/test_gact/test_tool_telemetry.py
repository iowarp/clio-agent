"""tool.call.started + tool.call.completed events
are live lifecycle telemetry, not reconstructed from post-turn summaries.

Reads from EventBus history after the POST instead of streaming SSE
— TestClient deadlocks on unbounded SSE responses (same story as
test_sse.py). The bus history is what the endpoint replays anyway,
so testing it directly is both faithful and deadlock-free.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mcp.types import TextContent
from pydantic import BaseModel, ConfigDict, Field

from tests._config_layer import set_config

# #735: run under the xdist-load flake-hunt CI job — this file is the one that
# flaked on cross-app tool-observer contamination.
# #948 S4b: default sessions run the blueprint react ``main``; route it to each
# test's ``build_app(agent=...)`` host fake.
pytestmark = [pytest.mark.concurrency, pytest.mark.usefixtures("host_agent_executor")]


def _settled_history(
    app, sid: str, *, timeout: float = 5.0, stable_window: float = 0.12, poll: float = 0.02
):
    """Return the session's bus history once it has stopped growing.

    The live tool observer publishes ``tool.call.*`` events onto the event loop
    from the daemon turn thread, so they can land slightly AFTER ``complete_turn``
    sees the assistant message settle. Reading the history once immediately races
    that flush. Poll until the history length is stable for a short window (or the
    timeout fires) so the assertions see the fully-flushed event stream.
    """

    deadline = time.monotonic() + timeout
    last_n: int = -1
    stable_start: float | None = None
    while time.monotonic() < deadline:
        history = app.state.bus._history.get(sid, [])
        n = len(history)
        if n > 0 and n == last_n:
            if stable_start is None:
                stable_start = time.monotonic()
            elif time.monotonic() - stable_start >= stable_window:
                return history
        else:
            stable_start = None
            last_n = n
        time.sleep(poll)
    return app.state.bus._history.get(sid, [])


from clio_agent.gact.app import (
    _bounded_tool_call_result,
    _is_bounded_tool_result,
    _make_tool_observer,
    _merge_tool_call_rows,
    _propose_edit_diffs_from_pred,
    build_app,
)


class _FakePred:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_propose_edit_promotes_tool_result_to_file_diff() -> None:
    # iowarp/clio-agent#674: a dynamic tool agent's fs_propose_edit result must
    # be promoted into a file-diff proposal (it never lands in pred.file_diffs).
    pred = _FakePred(
        tools_called=[
            {"name": "fs_read_file", "ok": True, "result": {"content": "x"}},
            {
                "name": "fs_propose_edit",
                "ok": True,
                "result": {
                    "path": "handlers.go",
                    "unified_diff": "@@ -1 +1 @@\n-old\n+new\n",
                    "new_content": "new\n",
                    "lines_added": 1,
                    "lines_removed": 1,
                },
            },
        ]
    )
    diffs = _propose_edit_diffs_from_pred(pred)
    assert len(diffs) == 1
    assert diffs[0]["path"] == "handlers.go"
    assert diffs[0]["unified_diff"].startswith("@@")
    assert diffs[0]["new_content"] == "new\n"
    assert diffs[0]["lines_added"] == 1


def test_propose_edit_skips_failed_calls_and_dedups() -> None:
    pred = _FakePred(
        tools_called=[
            {
                "name": "fs_propose_edit",
                "ok": False,
                "result": {"path": "a.py", "unified_diff": "d"},
            },
            {
                "name": "fs_propose_edit",
                "ok": True,
                "result": {"path": "b.py", "unified_diff": "x"},
            },
            {
                "name": "fs_propose_edit",
                "ok": True,
                "result": {"path": "b.py", "unified_diff": "x"},
            },
        ]
    )
    diffs = _propose_edit_diffs_from_pred(pred)
    assert [d["path"] for d in diffs] == ["b.py"]  # failed dropped, dup collapsed


def test_propose_edit_falls_back_to_trajectory() -> None:
    pred = _FakePred(
        tools_called=[],
        trajectory={
            "tool_name_0": "fs_propose_edit",
            "tool_args_0": {"path": "c.py"},
            "observation_0": {"path": "c.py", "unified_diff": "@@ x @@", "new_content": "c"},
        },
    )
    diffs = _propose_edit_diffs_from_pred(pred)
    assert len(diffs) == 1 and diffs[0]["path"] == "c.py"


@pytest.fixture(autouse=True)
def _isolate_tool_runtime_globals():
    """Reset the retained app-less tool-runtime fallback around every test.

    Turns run in daemon threads; the in-turn observer/gate resolve per-app from
    ``active_app().state.pending_*`` (isolated), but the single retained
    ``_FALLBACK_TOOL_RUNTIME`` bundle is the app-less net and persists across
    tests. These tests use a bare ``TestClient`` (no ``with``), so clearing the
    fallback to an empty bundle before and after each test makes a late stray
    app-less call a no-op, leaving each test's own ``build_app`` install as the
    only authority during its turn.
    """
    from clio_agent.tools import execution  # noqa: PLC0415

    def _clear() -> None:
        execution.set_tool_runtime_fallback(execution.ToolRuntimeHooks())

    _clear()
    yield
    _clear()


@dataclass
class _Pred:
    answer: str = "ok"
    selected_expert: str = "data_expert"
    routing_rationale: str = ""
    tools_called: list = None  # type: ignore[assignment]


class _Agent:
    def forward(self, question: str, session_id: str):
        return _Pred(
            tools_called=[
                {"name": "hdf5.analyze", "ok": True, "duration_ms": 14.0, "cached": False},
                {"name": "parquet.summarise", "ok": True, "duration_ms": 22.3, "cached": True},
            ]
        )


class _LiveObservedAgent:
    def forward(self, question: str, session_id: str):
        from clio_agent.tools.execution import current_tool_runtime, notify_global_tool_observer

        assert current_tool_runtime().tool_observer is not None
        notify_global_tool_observer("hdf5_list_datasets", {"filepath": "x.h5"}, "started", None)
        notify_global_tool_observer("hdf5_list_datasets", {"filepath": "x.h5"}, "completed", None)
        return _Pred()


class _LiveObservedWithPosthocTraceAgent:
    def forward(self, question: str, session_id: str):
        from clio_agent.tools.execution import current_tool_runtime, notify_global_tool_observer

        assert current_tool_runtime().tool_observer is not None
        args = {"filepath": "x.h5"}
        notify_global_tool_observer("hdf5_list_datasets", args, "started", None)
        notify_global_tool_observer("hdf5_list_datasets", args, "completed", None)
        return _Pred(
            tools_called=[
                {
                    "name": "hdf5_list_datasets",
                    "args": args,
                    "ok": True,
                    "duration_ms": 999.0,
                    "cached": True,
                    "result": {"datasets": ["safe_float"], "checksum": "abc123"},
                }
            ]
        )


class _LiveObservedResultAgent:
    def forward(self, question: str, session_id: str):
        from clio_agent.tools.execution import current_tool_runtime, notify_global_tool_observer

        assert current_tool_runtime().tool_observer is not None
        args = {"filepath": "x.h5"}
        result = {"datasets": ["safe_float"], "checksum": "abc123"}
        notify_global_tool_observer("hdf5_list_datasets", args, "started", None)
        notify_global_tool_observer("hdf5_list_datasets", args, "completed", None, result)
        return _Pred()


class _LiveObservedWaitAgentTasksAgent:
    """Drives the observer directly for ``wait_agent_tasks`` (#... P5 wire
    semantics): the started tool_call Part must carry ``metadata.waited_tasks``
    resolved from the agent-task registry AT CALL TIME — never a raw task-id
    array the UI would have to render/derive a name for."""

    def __init__(self) -> None:
        self.app: Any = None

    def forward(self, question: str, session_id: str):
        from clio_agent.gact.agent_tasks import AgentTask
        from clio_agent.tools.execution import current_tool_runtime, notify_global_tool_observer

        assert current_tool_runtime().tool_observer is not None
        registry = self.app.state.agent_task_registry
        registry.register(
            AgentTask(
                task_id="task_a",
                parent_session_id=session_id,
                child_session_id="child_a",
                agent_ref={"expert_id": "geospatial", "requesting_expert_id": "main"},
                run_index=0,
                run_label="LA dense scan",
            )
        )
        registry.register(
            AgentTask(
                task_id="task_b",
                parent_session_id=session_id,
                child_session_id="child_b",
                agent_ref={"expert_id": "ndp", "requesting_expert_id": "main"},
                run_index=0,
            )
        )
        args = {"task_ids": ["task_a", "task_b", "task_missing"], "timeout_s": 30.0}
        notify_global_tool_observer("wait_agent_tasks", args, "started", None)
        notify_global_tool_observer(
            "wait_agent_tasks",
            args,
            "completed",
            None,
            '{"results": [], "workflow_state_conflicts": [], "merged_workflow_state": {}}',
        )
        return _Pred()


class _LiveObservedDeclaredStructuredContentAgent:
    """Drives the observer directly to prove a native tool's DECLARED structured
    payload (:func:`declare_structured_content`) wins over the plain-string
    return for ``structured_content`` (owner ruling, P5), never mirrors into
    metadata, and never leaks onto a LATER unrelated call on the same thread
    (one-shot pop)."""

    def forward(self, question: str, session_id: str):
        from clio_agent.gact.agents.tool_instrumentation import declare_structured_content
        from clio_agent.tools.execution import current_tool_runtime, notify_global_tool_observer

        assert current_tool_runtime().tool_observer is not None
        args = {"task_ids": ["task_a"], "timeout_s": 5.0}
        notify_global_tool_observer("wait_agent_tasks", args, "started", None)
        declare_structured_content(
            {
                "summary": "waited 0.0s for 1 task — 1 completed",
                "results": [
                    {
                        "name": "geospatial #1",
                        "status": "completed",
                        "duration_ms": 12.0,
                        "answer_excerpt": "ok",
                    }
                ],
                "workflow_state_conflicts": [],
                "merged_workflow_state": {},
            }
        )
        notify_global_tool_observer(
            "wait_agent_tasks",
            args,
            "completed",
            None,
            '{"results": [], "workflow_state_conflicts": [], "merged_workflow_state": {}}',
        )
        # A SECOND, unrelated call on the SAME thread that never declares anything
        # must NEVER see the prior call's declared shape leak onto it.
        notify_global_tool_observer("hdf5_list_datasets", {"filepath": "x.h5"}, "started", None)
        notify_global_tool_observer(
            "hdf5_list_datasets", {"filepath": "x.h5"}, "completed", None, "some string result"
        )
        return _Pred()


class _LiveObservedLargeMcpResultAgent:
    def forward(self, question: str, session_id: str) -> object:
        from clio_agent.tools.execution import current_tool_runtime, notify_global_tool_observer

        assert current_tool_runtime().tool_observer is not None
        args = {"execution_id": "execution-structured"}
        structured = {
            "schema_version": "jarvis.execution.v1",
            "execution_id": "execution-structured",
            "payload": "x" * 13_000,
        }
        result = {
            "content": [{"type": "text", "text": "display projection"}],
            "structuredContent": structured,
        }
        notify_global_tool_observer("jarvis_get_execution", args, "started", None)
        notify_global_tool_observer(
            "jarvis_get_execution",
            args,
            "completed",
            None,
            result,
        )
        return _Pred()


class _RootExecutionResult(BaseModel):
    """Production-shaped FastMCP ``data`` object named like the live result."""

    model_config = ConfigDict(populate_by_name=True)

    schema_version: str
    execution_id: str
    scheduler_native_id: str | None = Field(alias="schedulerNativeId")
    payload: str


class _RootDataClient:
    """Return a parsed FastMCP result whose public JSON is available via data."""

    def __init__(self, root: _RootExecutionResult) -> None:
        self.root = root

    async def __aenter__(self) -> "_RootDataClient":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    async def list_tools(self) -> list[Any]:
        return [
            SimpleNamespace(
                name="relay_jarvis_get_execution",
                description="Query one durable JARVIS execution.",
                inputSchema={"properties": {"execution_id": {"type": "string"}}},
            )
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return SimpleNamespace(
            content=[TextContent(type="text", text=f"Root({self.root!s})")],
            structured_content=None,
            data=self.root,
            is_error=False,
            meta={"private": {"capability": "must-not-enter-telemetry"}},
        )

    async def read_resource(self, uri: str) -> list[Any]:
        """Satisfy the workspace bridge client protocol for this tool-only test."""

        raise AssertionError(f"unexpected resource read: {uri}")


class _LiveWorkspaceMcpRootDataAgent:
    """Drive the same workspace MCP bridge used by blueprint tools in production."""

    def __init__(self) -> None:
        self.root = _RootExecutionResult(
            schema_version="jarvis.execution.v1",
            execution_id="execution-live-root",
            schedulerNativeId=None,
            payload="x" * 13_000,
        )
        self.model_text = ""

    def forward(self, question: str, session_id: str) -> object:
        from clio_agent.tools.execution import SyncMCPToolExecutor

        client = _RootDataClient(self.root)
        with SyncMCPToolExecutor(
            object(),
            timeout=2.0,
            client_factory=lambda _server: client,
            permission_gate=lambda _name, _args: "allow",
        ) as executor:
            self.model_text = executor.call_tool(
                "relay_jarvis_get_execution",
                {"execution_id": self.root.execution_id},
            )
        return _Pred()


class _LiveObservedStructuredErrorResultAgent:
    def forward(self, question: str, session_id: str):
        from clio_agent.tools.execution import current_tool_runtime, notify_global_tool_observer

        assert current_tool_runtime().tool_observer is not None
        args = {"output_path": "/missing/plot.png"}
        result = {
            "error": {
                "type": "file_policy",
                "code": "parent_not_found",
                "message": "Output directory does not exist",
            }
        }
        notify_global_tool_observer("ndp_plot_csv_timeseries", args, "started", None)
        notify_global_tool_observer(
            "ndp_plot_csv_timeseries",
            args,
            "completed",
            "parent_not_found: Output directory does not exist",
            result,
        )
        return _Pred()


class _ToolRoutingAgent:
    def _selected_expert_for_tool(self, tool_name: str) -> str:
        assert tool_name == "NdpSearchDatasets"
        return "ndp_catalog"

    def _parent_route_for_child(self, expert_id: str) -> str:
        assert expert_id == "ndp_catalog"
        return "data"


@pytest.fixture()
def app_client(tmp_path: Path):
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    client = TestClient(app)
    return app, client


def test_bounded_tool_result_is_idempotent_no_nested_preview() -> None:
    # A bounded result flows across stages (tool -> catalog -> data -> main) and was
    # re-bounded each hop, nesting preview-of-preview (observed: a geo_filter result
    # wrapped 22x by turn.completed -> data buried -> staged station unverifiable).
    big = {
        "ok": True,
        "count": 144,
        "points": [{"Site": f"S{i}", "lat": 47.6, "lon": -122.3} for i in range(1200)],
    }
    b1 = _bounded_tool_call_result(big)
    assert _is_bounded_tool_result(b1)  # got bounded (was > limit)
    b2 = _bounded_tool_call_result(b1)
    assert b2 is b1  # idempotent: already-bounded payload is never re-wrapped
    assert json.dumps(b2).count("original_chars") == 1  # exactly one preview layer
    # A small (unbounded) result passes through untouched.
    small = {"ok": True, "count": 1}
    assert _bounded_tool_call_result(small) == small
    assert not _is_bounded_tool_result(small)


def test_bounded_tool_result_uses_configured_model_lane_limit(caplog) -> None:
    """The model preview limit is configurable and every truncation is typed."""

    set_config("limits.tool_result_chars", 80)
    with caplog.at_level("INFO", logger="clio_agent.gact.evidence"):
        bounded = _bounded_tool_call_result({"payload": "x" * 400})

    assert bounded["truncated"] is True
    assert bounded["original_chars"] > 400
    assert len(bounded["preview"]) <= 80
    matching = [
        record.getMessage()
        for record in caplog.records
        if "reason=tool_result_oversize" in record.getMessage()
    ]
    assert len(matching) == 1
    assert "original_chars=" in matching[0]
    assert "preview_chars=80" in matching[0]


def test_posthoc_tools_called_metadata_does_not_emit_lifecycle_events(app_client) -> None:
    from .conftest import complete_turn

    app, client = app_client
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    assistant = complete_turn(client, sid, "analyze")

    history = _settled_history(app, sid)
    started = [e for e in history if e.type == "tool.call.started"]
    completed = [e for e in history if e.type == "tool.call.completed"]

    assert started == []
    assert completed == []
    assert assistant["metadata"]["tools_called"] == [
        {
            "name": "hdf5.analyze",
            "ok": True,
            "duration_ms": 14.0,
            "cached": False,
            "telemetry_source": "posthoc_prediction",
        },
        {
            "name": "parquet.summarise",
            "ok": True,
            "duration_ms": 22.3,
            "cached": True,
            "telemetry_source": "posthoc_prediction",
        },
    ]


def test_live_observed_tool_call_is_not_reemitted_post_turn(tmp_path: Path) -> None:
    from .conftest import complete_turn

    app = build_app(sessions_path=tmp_path / "s.json", agent=_LiveObservedAgent())
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        assistant = complete_turn(client, sid, "analyze")

        history = _settled_history(app, sid)
        started = [e for e in history if e.type == "tool.call.started"]
        completed = [e for e in history if e.type == "tool.call.completed"]

        assert [e.payload["tool"] for e in started] == ["hdf5_list_datasets"]
        assert [e.payload["tool"] for e in completed] == ["hdf5_list_datasets"]
        assert started[0].payload["telemetry_source"] == "live_observer"
        assert completed[0].payload["telemetry_source"] == "live_observer"
        # WS1: clio transmits, it does not author UI captions -- the tool-response payload
        # carries the FACTS (ok/duration_ms/cached/result), not ui_summary/result_summary.
        assert "ui_summary" not in completed[0].payload
        assert "result_summary" not in completed[0].payload
        assert completed[0].payload["ok"] is True
        assert assistant["metadata"]["tools_called"][0]["name"] == "hdf5_list_datasets"
        assert assistant["metadata"]["tools_called"][0]["args"] == {"filepath": "x.h5"}
        assert assistant["metadata"]["tools_called"][0]["telemetry_source"] == "live_observer"


def test_concurrent_apps_do_not_cross_tool_telemetry(tmp_path: Path) -> None:
    """#735: two live-observed apps in ONE process must not share the observer.

    Building app_b rebinds the retained app-less fallback bundle to b's, so a
    stale reader would attribute a's turn to b. A turn on app_a must STILL land
    a's ``tools_called`` on a's message — the observer is resolved per tool call
    from the LIVE app (the installed resolver dispatches on ``active_app()`` into
    that app's ``pending_*``), never from the last-installed fallback. This is the
    deterministic form of the cross-file flake: it fails on `develop` (KeyError
    'tools_called' on whichever app didn't win the global) and passes once the
    hooks are resolved per-app.
    """
    from .conftest import complete_turn

    app_a = build_app(sessions_path=tmp_path / "a.json", agent=_LiveObservedAgent())
    app_b = build_app(sessions_path=tmp_path / "b.json", agent=_LiveObservedAgent())

    with TestClient(app_a) as client_a, TestClient(app_b) as client_b:
        sid_a = client_a.post("/v1/sessions", json={"title": "a"}).json()["id"]
        sid_b = client_b.post("/v1/sessions", json={"title": "b"}).json()["id"]
        assistant_a = complete_turn(client_a, sid_a, "analyze")
        assistant_b = complete_turn(client_b, sid_b, "analyze")

    # Each app's telemetry landed on ITS OWN message — no cross-contamination.
    assert assistant_a["metadata"]["tools_called"][0]["name"] == "hdf5_list_datasets"
    assert assistant_a["metadata"]["tools_called"][0]["telemetry_source"] == "live_observer"
    assert assistant_b["metadata"]["tools_called"][0]["name"] == "hdf5_list_datasets"
    assert assistant_b["metadata"]["tools_called"][0]["telemetry_source"] == "live_observer"


def test_live_observer_upgrades_matching_posthoc_trace_metadata(tmp_path: Path) -> None:
    from .conftest import complete_turn

    app = build_app(
        sessions_path=tmp_path / "s.json",
        agent=_LiveObservedWithPosthocTraceAgent(),
    )
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        assistant = complete_turn(client, sid, "analyze")

        history = _settled_history(app, sid)
        started = [e for e in history if e.type == "tool.call.started"]
        completed = [e for e in history if e.type == "tool.call.completed"]
        tools_called = assistant["metadata"]["tools_called"]

        assert len(started) == 1
        assert len(completed) == 1
        assert tools_called[0]["name"] == "hdf5_list_datasets"
        assert tools_called[0]["telemetry_source"] == "live_observer"
        assert tools_called[0]["duration_ms"] != 999.0
        assert tools_called[0]["cached"] is False
        assert tools_called[0]["result"] == {"datasets": ["safe_float"], "checksum": "abc123"}


def test_live_observer_records_completed_tool_result_evidence(tmp_path: Path) -> None:
    from .conftest import complete_turn

    app = build_app(
        sessions_path=tmp_path / "s.json",
        agent=_LiveObservedResultAgent(),
    )
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        assistant = complete_turn(client, sid, "analyze")

        history = _settled_history(app, sid)
        completed = [e for e in history if e.type == "tool.call.completed"]
        tools_called = assistant["metadata"]["tools_called"]
        tool_results = [
            e.payload["part"]
            for e in history
            if e.type == "message.part.added"
            and e.payload.get("part", {}).get("type") == "tool_result"
        ]

        assert completed[0].payload["result"] == {
            "datasets": ["safe_float"],
            "checksum": "abc123",
        }
        assert tools_called[0]["result"] == {
            "datasets": ["safe_float"],
            "checksum": "abc123",
        }
        assert tool_results[0]["metadata"]["result"] == {
            "datasets": ["safe_float"],
            "checksum": "abc123",
        }
        # #1190: a result WITHOUT structuredContent serves NO structured_content
        # field at all (absent-when-None wire semantics), and never a metadata copy.
        assert "structured_content" not in tool_results[0]
        assert "structured_content" not in tool_results[0]["metadata"]


def test_wait_agent_tasks_tool_call_stamps_waited_tasks_display_rows(tmp_path: Path) -> None:
    """P5 wire semantics: the wait_agent_tasks STARTED tool_call Part carries
    ``metadata.waited_tasks`` — one resolved display row per requested id,
    including a typed fallback row for an id the registry does not know — so
    the UI never renders a raw task-id array."""

    from .conftest import complete_turn

    agent = _LiveObservedWaitAgentTasksAgent()
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    agent.app = app
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        complete_turn(client, sid, "wait")

        history = _settled_history(app, sid)
        tool_call = next(
            e.payload["part"]
            for e in history
            if e.type == "message.part.added"
            and e.payload.get("part", {}).get("type") == "tool_call"
            and e.payload["part"].get("tool_name") == "wait_agent_tasks"
        )

    assert tool_call["metadata"]["waited_tasks"] == [
        {
            "task_id": "task_a",
            "agent_id": "geospatial",
            "run_index": 0,
            "run_label": "LA dense scan",
            "child_session_id": "child_a",
            "name": "LA dense scan",
        },
        {
            "task_id": "task_b",
            "agent_id": "ndp",
            "run_index": 0,
            "run_label": "",
            "child_session_id": "child_b",
            "name": "ndp #1",
        },
        {
            "task_id": "task_missing",
            "agent_id": "",
            "run_index": 0,
            "run_label": "",
            "child_session_id": "",
            "name": "task_missing",
        },
    ]


def test_declared_structured_content_wins_over_raw_result_and_is_one_shot(
    tmp_path: Path,
) -> None:
    """Owner ruling (P5 wire semantics): a native tool's DECLARED structured
    payload (declare_structured_content) is served as ``structured_content`` —
    NOT derived from the plain-string return value — never mirrored into
    metadata (the #1190 ONE-home rule), and never leaks onto a LATER unrelated
    tool call on the same thread (one-shot: read + cleared per completed call)."""

    from .conftest import complete_turn

    app = build_app(
        sessions_path=tmp_path / "s.json",
        agent=_LiveObservedDeclaredStructuredContentAgent(),
    )
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        complete_turn(client, sid, "wait")

        history = _settled_history(app, sid)
        tool_results = [
            e.payload["part"]
            for e in history
            if e.type == "message.part.added"
            and e.payload.get("part", {}).get("type") == "tool_result"
        ]

    assert len(tool_results) == 2
    wait_result, other_result = tool_results
    assert wait_result["structured_content"] == {
        "summary": "waited 0.0s for 1 task — 1 completed",
        "results": [
            {
                "name": "geospatial #1",
                "status": "completed",
                "duration_ms": 12.0,
                "answer_excerpt": "ok",
            }
        ],
        "workflow_state_conflicts": [],
        "merged_workflow_state": {},
    }
    assert "structured_content" not in wait_result["metadata"]
    # The plain string this tool ACTUALLY returned to the model is untouched —
    # served as the ordinary result preview, never replaced.
    assert wait_result["metadata"]["result"] == (
        '{"results": [], "workflow_state_conflicts": [], "merged_workflow_state": {}}'
    )
    # The unrelated NEXT call never declared anything: no leak, no field at all.
    assert "structured_content" not in other_result


def test_live_observer_keeps_exact_large_mcp_structured_content(tmp_path: Path) -> None:
    """The public structured result remains exact when the display preview is bounded.

    #1190: ``structured_content`` is a TOP-LEVEL field on the wire ``tool_result``
    part — the exact path the shipped UI render ladder reads
    (``extractStructuredContent`` → ``part['structured_content']``) — with ONE
    home: no ``metadata`` mirror. The served part (GET /messages) carries the
    same top-level copy with the object intact.
    """

    from .conftest import complete_turn

    structured = {
        "schema_version": "jarvis.execution.v1",
        "execution_id": "execution-structured",
        "payload": "x" * 13_000,
    }
    app = build_app(
        sessions_path=tmp_path / "s.json",
        agent=_LiveObservedLargeMcpResultAgent(),
    )
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        assistant = complete_turn(client, sid, "analyze")

        history = _settled_history(app, sid)
        tool_result = next(
            event.payload["part"]
            for event in history
            if event.type == "message.part.added"
            and event.payload.get("part", {}).get("type") == "tool_result"
        )

        assert tool_result["metadata"]["result"]["truncated"] is True
        # The wire part serves the structured copy at the TOP LEVEL, intact.
        assert tool_result["structured_content"] == structured
        # ONE home: the metadata mirror is gone.
        assert "structured_content" not in tool_result["metadata"]

        # The persisted/served message (GET /v1/sessions/{sid}/messages — what
        # complete_turn returns) carries the same top-level copy, intact.
        served_result = next(
            part for part in assistant["parts"] if part.get("type") == "tool_result"
        )
        assert served_result["structured_content"] == structured
        assert "structured_content" not in served_result.get("metadata", {})


def test_workspace_mcp_root_data_reaches_exact_gact_structured_content(tmp_path: Path) -> None:
    """The wire keeps exact Root/data while the model receives a bounded projection."""

    from .conftest import complete_turn

    agent = _LiveWorkspaceMcpRootDataAgent()
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        complete_turn(client, sid, "query the execution")

        history = _settled_history(app, sid)
        tool_result = next(
            event.payload["part"]
            for event in history
            if event.type == "message.part.added"
            and event.payload.get("part", {}).get("type") == "tool_result"
        )

        model_result = json.loads(agent.model_text)
        assert model_result["_clio"]["reason"] == "model_tool_result_oversize"
        assert model_result["_clio"]["original_chars"] == len(str(agent.root))
        assert len(agent.model_text) <= 12_000
        assert tool_result["metadata"]["result"]["truncated"] is True
        # #1190: the structured copy is served at the part TOP LEVEL (the UI
        # render ladder's read path), with no metadata mirror (ONE home).
        assert tool_result["structured_content"] == {
            "schema_version": "jarvis.execution.v1",
            "execution_id": "execution-live-root",
            "schedulerNativeId": None,
            "payload": "x" * 13_000,
        }
        assert "structured_content" not in tool_result["metadata"]
        assert "must-not-enter-telemetry" not in json.dumps(tool_result)
        # #1190 model-context contract: the ReAct observation is a bounded
        # projection of the ``.data`` result and NEVER a second serialization of
        # the exact structuredContent payload the wire part carries. The complete
        # structured copy is trace/UI-only.
        assert json.dumps(tool_result["structured_content"]) not in agent.model_text
        assert '"schema_version"' not in agent.model_text  # no JSON-keyed twin


def test_live_observer_preserves_failed_structured_tool_result_evidence(tmp_path: Path) -> None:
    from .conftest import complete_turn

    app = build_app(
        sessions_path=tmp_path / "s.json",
        agent=_LiveObservedStructuredErrorResultAgent(),
    )
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        assistant = complete_turn(client, sid, "analyze")

        history = _settled_history(app, sid)
        completed = [e for e in history if e.type == "tool.call.completed"]
        tools_called = assistant["metadata"]["tools_called"]
        tool_results = [
            e.payload["part"]
            for e in history
            if e.type == "message.part.added"
            and e.payload.get("part", {}).get("type") == "tool_result"
        ]

        expected_result = {
            "error": {
                "type": "file_policy",
                "code": "parent_not_found",
                "message": "Output directory does not exist",
            }
        }
        assert completed[0].payload["ok"] is False
        assert completed[0].payload["error"] == "parent_not_found: Output directory does not exist"
        assert completed[0].payload["result"] == expected_result
        assert tools_called[0]["ok"] is False
        assert tools_called[0]["error"] == "parent_not_found: Output directory does not exist"
        assert tools_called[0]["result"] == expected_result
        assert tool_results[0]["is_error"] is True
        assert tool_results[0]["metadata"]["result"] == expected_result


def test_tool_call_merge_does_not_attach_success_result_to_failed_attempt() -> None:
    rows = _merge_tool_call_rows(
        [
            {
                "name": "ndp_get_dataset_details",
                "call_id": "call_failed",
                "args": {"dataset_identifier": "abc"},
                "ok": False,
                "error": "ClosedResourceError()",
                "telemetry_source": "live_observer",
            }
        ],
        [
            {
                "name": "ndp_get_dataset_details",
                "args": {"dataset_identifier": "abc"},
                "ok": True,
                "result": {"dataset": {"id": "abc", "title": "EarthScope Stations Dataset"}},
            }
        ],
    )

    assert len(rows) == 2
    assert rows[0]["ok"] is False
    assert "result" not in rows[0]
    assert rows[1]["ok"] is True
    assert rows[1]["result"] == {"dataset": {"id": "abc", "title": "EarthScope Stations Dataset"}}


def test_live_tool_observer_emits_route_context_before_tool_part(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json", agent=_ToolRoutingAgent())
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    observer = _make_tool_observer(app)

    observer("NdpSearchDatasets", {"search_terms": "seismic"}, "started", None)

    history = _settled_history(app, sid)
    added_parts = [e.payload["part"] for e in history if e.type == "message.part.added"]
    # Clean-wire rule (owner 2026-08-05): the routing decision is an
    # OBSERVABILITY event (routing.decision on the semantic highway), never a
    # transcript part — the transcript carries only the delegation + the call.
    assert [p["type"] for p in added_parts] == [
        "expert_handoff",
        "tool_call",
    ]
    assert all(p["type"] != "routing_decision" for p in added_parts)
    assert added_parts[0]["metadata"]["agent_id"] == "ndp_catalog"
    assert added_parts[0]["metadata"]["parent_id"] == "data"
    # The expert_handoff part exposes the delegation as structured fields (consumed
    # by the UI) instead of forcing it to parse the prose ``text`` label.
    assert added_parts[0]["parent_agent"] == "data"
    assert added_parts[0]["child_agent"] == "ndp_catalog"
    assert added_parts[0]["stage"] == "tool.started"
    assert added_parts[0]["agent_id"] == "data"  # generated by the parent
    assert added_parts[1]["tool_name"] == "NdpSearchDatasets"
    # The tool_call part is attributed to the expert that runs the tool.
    assert added_parts[1]["agent_id"] == "ndp_catalog"


def test_tool_result_full_in_trace_but_bounded_in_ledger(tmp_path: Path, monkeypatch) -> None:
    """T1: the canonical trace keeps the FULL tool result (never capped) while the
    ledger/assistant-metadata projection stays bounded. Drives the observer
    DIRECTLY (no full turn) under the off-loop file backend, then reads the trace."""
    import json as _json

    trace_dir = tmp_path / "traces"
    set_config("trace.backend", "file")  # file-layer (file > env); #985 config-first
    set_config("trace.path", str(trace_dir))
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]

    observer = _make_tool_observer(app)
    big_result = {"rows": [f"station_{i:05d}" for i in range(4000)]}  # >>12000 chars
    observer("hdf5_dump_all", {"filepath": "x.h5"}, "started", None)
    observer("hdf5_dump_all", {"filepath": "x.h5"}, "completed", None, big_result)

    # T1: durable trace carries the FULL result (all 4000 rows, not a preview).
    app.state.semantic_trace_backend.flush()
    rows = [
        _json.loads(line) for line in (trace_dir / f"{sid}.semantic.jsonl").read_text().splitlines()
    ]
    tcc = next(r for r in rows if r["event_type"] == "tool.call.completed")
    assert isinstance(tcc["payload"]["result"], dict)
    assert len(tcc["payload"]["result"]["rows"]) == 4000
    assert tcc["payload"]["result"].get("truncated") is not True

    # ...while the per-session ledger (assistant-metadata projection) is BOUNDED.
    led = app.state.tool_call_ledger.get(sid, [])
    assert led and isinstance(led[0]["result"], dict) and led[0]["result"].get("truncated") is True


# --------------------------------------------------------------------------- #
# Finding A (proven leak, end-to-end through the REAL observe() chain): a     #
# declare_structured_content() payload must never ride onto a call it was    #
# never declared for -- neither via an unresolved session id (leak path #1)  #
# nor via an observer exception execution.notify_tool_observer swallows      #
# (leak path #2).                                                             #
# --------------------------------------------------------------------------- #


def test_declared_structured_content_does_not_leak_when_sid_is_unresolved(tmp_path: Path) -> None:
    """Leak path #1: ``if not sid: return`` (no session yet, no recency
    fallback) exits BEFORE the completed-phase pop ever runs. The observer's
    own started-phase clear must discard the leak once a session exists,
    before it can be misread as belonging to the NEXT, unrelated call."""

    from clio_agent.gact.agents.tool_instrumentation import declare_structured_content

    app = build_app(sessions_path=tmp_path / "s.json")
    observer = _make_tool_observer(app)

    # No session exists yet: _resolve_tool_session(app) resolves "" for both
    # calls below -- observe() returns before ever reaching its own pop.
    observer("wait_agent_tasks", {"task_ids": []}, "started", None)
    declare_structured_content({"leaked": "should never survive an unresolved sid"})
    observer("wait_agent_tasks", {"task_ids": []}, "completed", None, "result-text")

    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    observer("hdf5_list_datasets", {"filepath": "x.h5"}, "started", None)
    observer("hdf5_list_datasets", {"filepath": "x.h5"}, "completed", None, "some string result")

    parts = (app.state.live_assistant_parts or {}).get(sid, [])
    result_parts = [p for p in parts if p.type == "tool_result"]
    assert len(result_parts) == 1
    assert result_parts[0].structured_content is None


def test_declared_structured_content_does_not_leak_when_observer_raises_before_its_own_pop(
    tmp_path: Path,
) -> None:
    """Leak path #2: execution.notify_tool_observer swallows any exception the
    observer raises. Forcing the REAL observe() to raise partway through its
    completed-phase bookkeeping (well before its own pop, via a broken
    ``app.state.cancel_events``) must not let the declaration ride onto a
    later, unrelated call."""

    from clio_agent.gact.agents.tool_instrumentation import declare_structured_content
    from clio_agent.tools import execution as _execution

    app = build_app(sessions_path=tmp_path / "s.json")
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    observer = _make_tool_observer(app)

    observer("wait_agent_tasks", {"task_ids": []}, "started", None)
    declare_structured_content({"leaked": "should never survive an observer crash"})
    app.state.cancel_events = None  # forces an AttributeError deep in "completed"
    _execution.notify_tool_observer(
        observer, "wait_agent_tasks", {"task_ids": []}, "completed", None, "result-text"
    )  # swallowed exactly like production's notify_global_tool_observer

    app.state.cancel_events = {}
    observer("hdf5_list_datasets", {"filepath": "x.h5"}, "started", None)
    observer("hdf5_list_datasets", {"filepath": "x.h5"}, "completed", None, "some string result")

    parts = (app.state.live_assistant_parts or {}).get(sid, [])
    result_parts = [p for p in parts if p.type == "tool_result"]
    assert result_parts  # the crashed call emitted none; the later call did
    assert all(p.structured_content is None for p in result_parts)


# --------------------------------------------------------------------------- #
# Finding B (proven, MAJOR): create_artifact's own ``content`` (a            #
# model-authored deliverable the minted artifact file already durably        #
# stores) must never ride the wire a second time, unbounded, inside the      #
# started tool_call Part's ``input``.                                        #
# --------------------------------------------------------------------------- #


def test_create_artifact_content_is_elided_not_mirrored_unbounded(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    observer = _make_tool_observer(app)

    big_content = "x" * 50_000
    args = {"name": "report.md", "kind": "document", "content": big_content}
    observer("create_artifact", args, "started", None)

    parts = (app.state.live_assistant_parts or {}).get(sid, [])
    call_part = next(p for p in parts if p.type == "tool_call")
    assert call_part.input["name"] == "report.md"
    assert call_part.input["content"] == {"elided": "artifact_content", "bytes": 50_000}
    assert big_content not in json.dumps(call_part.to_wire())


def test_create_artifact_batch_content_is_elided_per_item(tmp_path: Path) -> None:
    """The batch form (``artifacts=[{...content...}, ...]``) elides EACH
    item's own content independently; a path-only item is untouched."""

    app = build_app(sessions_path=tmp_path / "s.json")
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    observer = _make_tool_observer(app)

    args = {
        "artifacts": [
            {"name": "a.md", "content": "y" * 20_000},
            {"name": "b.md", "path": "already/on/disk.md"},
        ]
    }
    observer("create_artifact", args, "started", None)

    parts = (app.state.live_assistant_parts or {}).get(sid, [])
    call_part = next(p for p in parts if p.type == "tool_call")
    assert call_part.input["artifacts"][0]["content"] == {
        "elided": "artifact_content",
        "bytes": 20_000,
    }
    assert call_part.input["artifacts"][1] == {"name": "b.md", "path": "already/on/disk.md"}


def test_other_tools_input_is_not_elided_only_generically_bounded(tmp_path: Path) -> None:
    """The elision is scoped STRICTLY to create_artifact; an unrelated tool's
    ``content``-named argument passes through untouched (small enough to
    never hit the generic 12000-char bound tool RESULTS already respect)."""

    app = build_app(sessions_path=tmp_path / "s.json")
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    observer = _make_tool_observer(app)

    observer("fs_write_file", {"path": "a.txt", "content": "small"}, "started", None)

    parts = (app.state.live_assistant_parts or {}).get(sid, [])
    call_part = next(p for p in parts if p.type == "tool_call")
    assert call_part.input == {"path": "a.txt", "content": "small"}


# --------------------------------------------------------------------------- #
# Finding C (proven, wire-corrupting): a model emitting ``task_ids`` as       #
# anything other than a list of ids must never be turned into fabricated     #
# rows -- ``list("task_a")`` iterates a STRING's characters.                  #
# --------------------------------------------------------------------------- #


def test_wait_agent_tasks_string_task_ids_does_not_fabricate_per_character_rows(
    tmp_path: Path,
) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    observer = _make_tool_observer(app)

    observer("wait_agent_tasks", {"task_ids": "task_a"}, "started", None)

    parts = (app.state.live_assistant_parts or {}).get(sid, [])
    call_part = next(p for p in parts if p.type == "tool_call")
    assert call_part.metadata["waited_tasks"] == [{"invalid": "task_ids_not_a_list"}]


def test_wait_agent_tasks_non_string_list_items_does_not_fabricate_rows(
    tmp_path: Path,
) -> None:
    """A list with non-string items is ALSO not a valid task_ids array."""

    app = build_app(sessions_path=tmp_path / "s.json")
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    observer = _make_tool_observer(app)

    observer("wait_agent_tasks", {"task_ids": [1, 2, 3]}, "started", None)

    parts = (app.state.live_assistant_parts or {}).get(sid, [])
    call_part = next(p for p in parts if p.type == "tool_call")
    assert call_part.metadata["waited_tasks"] == [{"invalid": "task_ids_not_a_list"}]


def test_wait_agent_tasks_missing_task_ids_resolves_empty_not_invalid(tmp_path: Path) -> None:
    """Absent task_ids keeps its ORIGINAL behavior (an empty resolved list) --
    only a genuinely malformed value gets the typed marker."""

    app = build_app(sessions_path=tmp_path / "s.json")
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    observer = _make_tool_observer(app)

    observer("wait_agent_tasks", {}, "started", None)

    parts = (app.state.live_assistant_parts or {}).get(sid, [])
    call_part = next(p for p in parts if p.type == "tool_call")
    assert call_part.metadata["waited_tasks"] == []


# --------------------------------------------------------------------------- #
# Finding D (refactor): the generic observer path must never hardcode a      #
# tool name -- per-tool STARTED metadata comes from a registry declared in   #
# tool_instrumentation.py.                                                    #
# --------------------------------------------------------------------------- #


def test_wait_agent_tasks_call_metadata_is_registered_not_hardcoded() -> None:
    import inspect

    from clio_agent.gact import tool_observer as _tool_observer_module
    from clio_agent.gact.agents.tool_instrumentation import tool_call_metadata_resolver

    assert tool_call_metadata_resolver("wait_agent_tasks") is not None

    source = inspect.getsource(_tool_observer_module._make_tool_observer)
    assert 'name == "wait_agent_tasks"' not in source
