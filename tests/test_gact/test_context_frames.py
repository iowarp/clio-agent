from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


@dataclass
class FakePrediction:
    answer: str
    selected_expert: str = "analysis"
    routing_rationale: str = "matched analysis keywords"
    route_source: str = "test"
    route_reason: str = "test route"
    error_info: dict[str, Any] | None = None


class FakeAgent:
    def __init__(self, *, error_info: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.error_info = error_info

    def forward(self, question: str, session_id: str) -> FakePrediction:
        self.calls.append((question, session_id))
        return FakePrediction(answer="context answer", error_info=self.error_info)


@pytest.fixture()
def fake_agent() -> FakeAgent:
    return FakeAgent()


@pytest.fixture()
def client(tmp_path: Path, fake_agent: FakeAgent) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "sessions.json", agent=fake_agent))


def _create_session(client: TestClient) -> str:
    return client.post(
        "/v1/sessions",
        json={
            "title": "context frame",
            "metadata": {"prompt_profile": "heavy"},
            "agent": {"id": "main"},
            "routing_mode": "reasoning_only",
        },
    ).json()["id"]


def test_context_frame_recorded_for_successful_turn(client: TestClient) -> None:
    from .conftest import complete_turn

    sid = _create_session(client)
    assistant = complete_turn(client, sid, "inspect this context")

    body = client.get(f"/v1/sessions/{sid}/context/frames").json()
    frames = body["frames"]
    assert len(frames) == 1
    frame = frames[0]
    assert frame["status"] == "completed"
    assert frame["assistant_message_id"] == assistant["id"]
    assert frame["user_message_id"].startswith("msg_user_")
    assert frame["agent"]["id"] == "main"
    assert frame["agent"]["routing_mode"] == "reasoning_only"
    assert frame["prompt"]["profile"] == "heavy"
    assert frame["model"] == {"provider_id": "", "model_id": "", "variant": ""}
    assert frame["tokens_estimated"] >= 1
    assert [item["kind"] for item in frame["items"]] == ["message"]
    assert frame["items"][0]["reason"] == "visible_transcript"

    fetched = client.get(f"/v1/sessions/{sid}/context/frames/{frame['id']}").json()
    assert fetched["frame"]["id"] == frame["id"]
    # WS1: context frames are substrate the UI does not render live -- they are
    # recorded + queryable on demand (asserted above via GET /context/frames), but
    # the per-frame created/completed events do NOT ride the served SSE bus.
    history_types = [event.type for event in client.app.state.bus._history.get(sid, [])]
    assert "context.frame.created" not in history_types
    assert "context.frame.completed" not in history_types


def test_context_frame_includes_attached_context_file(
    client: TestClient,
    tmp_path: Path,
    fake_agent: FakeAgent,
) -> None:
    from .conftest import complete_turn

    sid = _create_session(client)
    path = tmp_path / "data.csv"
    path.write_text("a,b\n1,2\n", encoding="utf-8")
    resp = client.post(
        f"/v1/sessions/{sid}/context/files",
        json={"path": str(path), "mode": "read", "size": path.stat().st_size},
    )
    assert resp.status_code == 200, resp.text

    complete_turn(client, sid, "summarize @data.csv")

    frame = client.get(f"/v1/sessions/{sid}/context/frames").json()["frames"][0]
    file_items = [item for item in frame["items"] if item["kind"] == "context_file"]
    assert len(file_items) == 1
    assert file_items[0]["path"] == str(path)
    assert file_items[0]["included"] is True
    assert file_items[0]["reason"] == "attached_context_file"
    assert frame["metadata"]["context_file_injected_chars"] > 0
    assert str(path) in fake_agent.calls[0][0]


def test_context_frame_records_turn_error(tmp_path: Path) -> None:
    agent = FakeAgent(
        error_info={
            "error": "tool_error",
            "message": "simulated failure",
            "recoverable": True,
        }
    )
    client = TestClient(build_app(sessions_path=tmp_path / "sessions.json", agent=agent))
    from .conftest import complete_turn

    sid = _create_session(client)
    complete_turn(client, sid, "fail with metadata")

    frame = client.get(f"/v1/sessions/{sid}/context/frames").json()["frames"][0]
    assert frame["status"] == "error"
    assert frame["metadata"]["turn_error"]["error"] == "tool_error"


def test_context_frames_unknown_session_404s(client: TestClient) -> None:
    resp = client.get("/v1/sessions/sess_missing/context/frames")

    assert resp.status_code == 404
    assert resp.json()["error"]["error"] == "not_found"


def test_capabilities_advertise_context_frames(client: TestClient) -> None:
    caps = client.get("/v1/capabilities").json()["capabilities"]

    assert caps["x_clio_context_frames"] is True

