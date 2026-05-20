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

from clio_agent.gact.app import build_app


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
                }
            ]
        )


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
