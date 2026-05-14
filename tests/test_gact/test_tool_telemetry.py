"""CLIO-BBBBBBBBBB18: tool.call.started + tool.call.completed events
get published for every tool the agent reports.

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
        return _Pred(tools_called=[
            {"name": "hdf5.analyze", "ok": True, "duration_ms": 14.0, "cached": False},
            {"name": "parquet.summarise", "ok": True, "duration_ms": 22.3, "cached": True},
        ])


@pytest.fixture()
def app_client(tmp_path: Path):
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    client = TestClient(app)
    return app, client


def test_tool_call_events_emit_in_pairs(app_client) -> None:
    app, client = app_client
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    client.post(
        f"/v1/sessions/{sid}/messages",
        json={"parts": [{"type": "text", "text": "analyze"}]},
    )

    history = app.state.bus._history.get(sid, [])
    started = [e for e in history if e.type == "tool.call.started"]
    completed = [e for e in history if e.type == "tool.call.completed"]

    assert [e.payload["tool"] for e in completed] == [
        "hdf5.analyze", "parquet.summarise",
    ]
    assert completed[0].payload["cached"] is False
    assert completed[1].payload["cached"] is True
    assert completed[0].payload["duration_ms"] == 14.0
    # started + completed line up by call_id.
    assert {e.payload["call_id"] for e in started} == {
        e.payload["call_id"] for e in completed
    }
    # Started appears before completed for every call.
    for s, c in zip(started, completed, strict=True):
        assert s.id < c.id
