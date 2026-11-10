from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app

# Every session runs its pinned plugin agent (the hermetic ``standard``); route its
# module to each test's ``build_app(agent=...)`` host fake.
pytestmark = pytest.mark.usefixtures("host_agent_executor", "no_session_facts")


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
def client(tmp_path: Path, fake_agent: FakeAgent) -> Iterator[TestClient]:
    with TestClient(
        build_app(sessions_path=tmp_path / "sessions.json", agent=fake_agent)
    ) as test_client:
        yield test_client


def _create_session(client: TestClient) -> str:
    return client.post(
        "/v1/sessions",
        json={
            "title": "context frame",
            "metadata": {"prompt_profile": "heavy"},
            "agent": {"id": "standard"},
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
    assert frame["agent"]["id"] == "standard"
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
    from .conftest import complete_turn

    with TestClient(build_app(sessions_path=tmp_path / "sessions.json", agent=agent)) as client:
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


def _message(mid: str, role: str, text: str, **tokens: int) -> Any:
    from clio_agent.gact.types import Message, Part, Tokens

    return Message(
        id=mid,
        session_id="",
        role=role,
        created_at="2026-11-10T00:00:00+00:00",
        updated_at="2026-11-10T00:00:00+00:00",
        parts=[Part(id=f"{mid}_p", type="text", text=text)],
        tokens=Tokens(**tokens),
    )


def test_context_frame_message_items_ignore_provider_call_usage(client: TestClient) -> None:
    """An assistant message's usage is the whole call (system prompt + history);
    the frame item must count only what the message itself adds (gact-tui#739)."""

    from clio_agent.gact.enrichment import _finalize_context_frame, _record_context_frame

    app = client.app
    sid = _create_session(client)
    app.state.sessions.update(
        sid,
        metadata_patch={
            "context_usage_by_scope": {
                "standard": {"used_tokens": 38_000, "source": "provider", "turn_id": "t0"}
            }
        },
    )
    user_1 = _message("msg_user_1", "user", "hello")
    answer = _message(
        "msg_asst_1",
        "assistant",
        "Hi! " * 20,
        input=4_000,
        output=40,
        cache_read=33_000,
        cache_write=600,
    )
    user_2 = _message("msg_user_2", "user", "what is my name?")
    app.state.messages[sid] = [user_1, answer, user_2]

    frame = _record_context_frame(
        app,
        sid,
        app.state.sessions.get(sid),
        user_2,
        user_text="what is my name?",
        enriched_text="what is my name?",
        context_error=None,
    )

    by_id = {item["source_id"]: item["tokens_estimated"] for item in frame["items"]}
    assert by_id == {"msg_user_1": 1, "msg_asst_1": 20, "msg_user_2": 4}
    assert frame["tokens_estimated"] == 25
    assert frame["metadata"]["token_estimate"] == "chars_div_4"
    assert frame["metadata"]["prompt_input_tokens"] == 38_000
    assert frame["metadata"]["prompt_input_tokens_turn_id"] == "t0"

    _finalize_context_frame(
        app,
        sid,
        frame["id"],
        "msg_asst_2",
        "completed",
        error_info=None,
        prompt_usage={"used_tokens": 38_150, "source": "provider", "cache_read_tokens": 37_000},
    )
    meta = frame["metadata"]
    assert meta["prompt_input_tokens"] == 38_150
    assert meta["prompt_input_tokens_source"] == "provider"
    assert meta["prompt_input_tokens_turn_id"] == user_2.id
    assert meta["prompt_input_cache_read_tokens"] == 37_000
    assert frame["tokens_estimated"] == 25


def test_context_frame_total_skips_compacted_messages(client: TestClient) -> None:
    from clio_agent.gact.enrichment import _record_context_frame
    from clio_agent.gact.summarization_record import summarization_part
    from clio_agent.gact.types import Message

    app = client.app
    sid = _create_session(client)
    old = _message("msg_old", "assistant", "x" * 4_000)
    checkpoint = Message(
        id="msg_checkpoint",
        session_id=sid,
        role="assistant",
        created_at="2026-11-10T00:00:01+00:00",
        updated_at="2026-11-10T00:00:01+00:00",
        parts=[
            summarization_part(
                "s" * 40,
                trigger="manual",
                compaction_id="cmp_1",
                derived_from=["msg_old"],
                compacted_message_ids=["msg_old"],
            )
        ],
    )
    user = _message("msg_user_9", "user", "next question")
    app.state.messages[sid] = [old, checkpoint, user]

    frame = _record_context_frame(
        app,
        sid,
        app.state.sessions.get(sid),
        user,
        user_text="next question",
        enriched_text="next question",
        context_error=None,
    )

    items = {item["source_id"]: item for item in frame["items"]}
    assert items["msg_old"]["included"] is False
    assert items["msg_old"]["tokens_estimated"] == 1_000
    assert frame["tokens_estimated"] == sum(
        item["tokens_estimated"] for item in frame["items"] if item["included"]
    )
    assert frame["tokens_estimated"] < 1_000
