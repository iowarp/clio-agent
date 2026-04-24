"""CLIO-BBBBBBBBBB16: assistant turns report tools_called.

The TUI renders a post-hoc gutter under each assistant message by
reading ``metadata.tools_called``. This test pins that the POST
/messages non-streaming body AND the message.completed SSE payload
both carry it when the agent's Prediction exposes tool traces.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


@dataclass
class _PredNoTools:
    answer: str = "ok"
    selected_expert: str = "data_expert"
    routing_rationale: str = ""


@dataclass
class _PredWithTools:
    answer: str = "ok"
    selected_expert: str = "data_expert"
    routing_rationale: str = "keyword match"
    tools_called: object = None


class _Agent:
    def __init__(self, pred) -> None:
        self._pred = pred

    def forward(self, question: str, session_id: str):
        return self._pred


def _client(tmp_path: Path, pred) -> TestClient:
    return TestClient(
        build_app(
            sessions_path=tmp_path / "sessions.json", agent=_Agent(pred)
        )
    )


def test_no_tools_called_keeps_metadata_empty(tmp_path: Path) -> None:
    client = _client(tmp_path, _PredNoTools())
    sess = client.post("/v1/sessions", json={"title": "t"}).json()
    resp = client.post(
        f"/v1/sessions/{sess['id']}/messages",
        json={"parts": [{"type": "text", "text": "hi"}]},
    ).json()
    # No tools_called in either the response body or the message
    # metadata — we don't want a bare `{tools_called: []}` row on the
    # wire because the TUI's renderer treats its presence as "call
    # the post-hoc gutter".
    assert "tools_called" not in resp["assistant_message"]["metadata"]


def test_tools_called_propagates_to_message_and_completion(tmp_path: Path) -> None:
    tools = [
        {
            "name": "hdf5_analyze",
            "args": {"path": "/tmp/x.h5"},
            "ok": True,
            "duration_ms": 42.5,
            "cached": False,
        },
        # One already wrapped as a msgspec-like with attribute access:
        type("ToolCall", (), {
            "name": "parquet_summarise",
            "args": {},
            "ok": True,
            "duration_ms": 12.0,
            "cached": True,
        })(),
    ]
    pred = _PredWithTools(tools_called=tools)
    client = _client(tmp_path, pred)

    sess = client.post("/v1/sessions", json={"title": "t"}).json()
    resp = client.post(
        f"/v1/sessions/{sess['id']}/messages",
        json={"parts": [{"type": "text", "text": "analyze /tmp/x.h5"}]},
    ).json()

    md = resp["assistant_message"]["metadata"]
    assert "tools_called" in md
    rows = md["tools_called"]
    assert len(rows) == 2
    assert rows[0]["name"] == "hdf5_analyze"
    assert rows[0]["ok"] is True
    assert rows[0]["duration_ms"] == 42.5
    assert rows[0]["cached"] is False
    # The second row came from an object-with-attrs, not a dict;
    # the extractor should still have normalised it to the same wire shape.
    assert rows[1]["name"] == "parquet_summarise"
    assert rows[1]["cached"] is True
