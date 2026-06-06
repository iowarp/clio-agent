"""CLIO-BBBBBBBBBB18: tool.call.started + tool.call.completed events
are live lifecycle telemetry, not reconstructed from post-turn summaries.

Reads from EventBus history after the POST instead of streaming SSE
— TestClient deadlocks on unbounded SSE responses (same story as
test_sse.py). The bus history is what the endpoint replays anyway,
so testing it directly is both faithful and deadlock-free.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import _make_tool_observer, _merge_tool_call_rows, build_app


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
        from clio_agent.tools.execution import _GLOBAL_TOOL_OBSERVER

        assert _GLOBAL_TOOL_OBSERVER is not None
        _GLOBAL_TOOL_OBSERVER("hdf5_list_datasets", {"filepath": "x.h5"}, "started", None)
        _GLOBAL_TOOL_OBSERVER("hdf5_list_datasets", {"filepath": "x.h5"}, "completed", None)
        return _Pred()


class _LiveObservedWithPosthocTraceAgent:
    def forward(self, question: str, session_id: str):
        from clio_agent.tools.execution import _GLOBAL_TOOL_OBSERVER

        assert _GLOBAL_TOOL_OBSERVER is not None
        args = {"filepath": "x.h5"}
        _GLOBAL_TOOL_OBSERVER("hdf5_list_datasets", args, "started", None)
        _GLOBAL_TOOL_OBSERVER("hdf5_list_datasets", args, "completed", None)
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
        from clio_agent.tools.execution import _GLOBAL_TOOL_OBSERVER

        assert _GLOBAL_TOOL_OBSERVER is not None
        args = {"filepath": "x.h5"}
        result = {"datasets": ["safe_float"], "checksum": "abc123"}
        _GLOBAL_TOOL_OBSERVER("hdf5_list_datasets", args, "started", None)
        _GLOBAL_TOOL_OBSERVER("hdf5_list_datasets", args, "completed", None, result)
        return _Pred()


class _LiveObservedStructuredErrorResultAgent:
    def forward(self, question: str, session_id: str):
        from clio_agent.tools.execution import _GLOBAL_TOOL_OBSERVER

        assert _GLOBAL_TOOL_OBSERVER is not None
        args = {"output_path": "/missing/plot.png"}
        result = {
            "error": {
                "type": "file_policy",
                "code": "parent_not_found",
                "message": "Output directory does not exist",
            }
        }
        _GLOBAL_TOOL_OBSERVER("ndp_plot_csv_timeseries", args, "started", None)
        _GLOBAL_TOOL_OBSERVER(
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


def test_posthoc_tools_called_metadata_does_not_emit_lifecycle_events(app_client) -> None:
    from .conftest import complete_turn

    app, client = app_client
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    assistant = complete_turn(client, sid, "analyze")

    history = app.state.bus._history.get(sid, [])
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
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    assistant = complete_turn(client, sid, "analyze")

    history = app.state.bus._history.get(sid, [])
    started = [e for e in history if e.type == "tool.call.started"]
    completed = [e for e in history if e.type == "tool.call.completed"]

    assert [e.payload["tool"] for e in started] == ["hdf5_list_datasets"]
    assert [e.payload["tool"] for e in completed] == ["hdf5_list_datasets"]
    assert started[0].payload["telemetry_source"] == "live_observer"
    assert completed[0].payload["telemetry_source"] == "live_observer"
    assert completed[0].payload["ui_summary"] == "Tool hdf5_list_datasets completed."
    assert completed[0].payload["result_summary"] == "Tool hdf5_list_datasets completed."
    assert assistant["metadata"]["tools_called"][0]["name"] == "hdf5_list_datasets"
    assert assistant["metadata"]["tools_called"][0]["args"] == {"filepath": "x.h5"}
    assert assistant["metadata"]["tools_called"][0]["telemetry_source"] == "live_observer"


def test_live_observer_upgrades_matching_posthoc_trace_metadata(tmp_path: Path) -> None:
    from .conftest import complete_turn

    app = build_app(
        sessions_path=tmp_path / "s.json",
        agent=_LiveObservedWithPosthocTraceAgent(),
    )
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    assistant = complete_turn(client, sid, "analyze")

    history = app.state.bus._history.get(sid, [])
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
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    assistant = complete_turn(client, sid, "analyze")

    history = app.state.bus._history.get(sid, [])
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


def test_live_observer_preserves_failed_structured_tool_result_evidence(tmp_path: Path) -> None:
    from .conftest import complete_turn

    app = build_app(
        sessions_path=tmp_path / "s.json",
        agent=_LiveObservedStructuredErrorResultAgent(),
    )
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    assistant = complete_turn(client, sid, "analyze")

    history = app.state.bus._history.get(sid, [])
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
    assert rows[1]["result"] == {
        "dataset": {"id": "abc", "title": "EarthScope Stations Dataset"}
    }


def test_live_tool_observer_emits_route_context_before_tool_part(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json", agent=_ToolRoutingAgent())
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    observer = _make_tool_observer(app)

    observer("NdpSearchDatasets", {"search_terms": "seismic"}, "started", None)

    history = app.state.bus._history.get(sid, [])
    added_parts = [
        e.payload["part"]
        for e in history
        if e.type == "message.part.added"
    ]
    assert [p["type"] for p in added_parts] == [
        "routing_decision",
        "expert_handoff",
        "tool_call",
    ]
    assert added_parts[0]["selected_agent"] == "data"
    assert added_parts[0]["execution_path"] == "orchestrator -> data"
    assert added_parts[1]["metadata"]["agent_id"] == "ndp_catalog"
    assert added_parts[1]["metadata"]["parent_id"] == "data"
    assert added_parts[2]["tool_name"] == "NdpSearchDatasets"
