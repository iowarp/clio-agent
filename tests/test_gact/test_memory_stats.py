"""CLIO-BBBBBBBBBB11: tests for /v1/memory/stats.

Drives the app with a FakeARC so we don't need a real ARC instance
(which would touch disk + spin up indexes). Covers:

- ARC wired -> cache + global stats reported
- ARC not wired -> zeros (per SPEC §6.19 'zeros are a valid signal')
- ?session_id with known session -> session block populated
- ?session_id with unknown session -> empty session block (NOT a 404)
- Wire shape uses 'global' (not 'global_') as the JSON key
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.types import Message, Part, Tokens


class FakeARC:
    """Stand-in for ARCMemory exposing the get_cache_stats() shape
    the GACT layer actually depends on."""

    def __init__(
        self,
        *,
        hits: int = 0,
        misses: int = 0,
        capacity: int = 1000,
        conv_index_size: int = 0,
        inv_index_size: int = 0,
    ) -> None:
        total = hits + misses
        self._stats = {
            "hits": hits,
            "misses": misses,
            "hit_rate": (hits / total) if total else 0.0,
            "capacity": capacity,
            "conv_index_size": conv_index_size,
            "inv_index_size": inv_index_size,
        }

    def get_cache_stats(self) -> dict[str, Any]:
        return dict(self._stats)


class FakeAgent:
    def __init__(self) -> None:
        self.questions: list[str] = []

    def forward(self, question: str, session_id: str) -> Any:
        self.questions.append(question)
        return type(
            "Pred",
            (),
            {
                "answer": "memory-aware answer",
                "selected_expert": "main",
                "routing_rationale": "fake",
            },
        )()


@pytest.fixture()
def client_with_arc(tmp_path: Path) -> TestClient:
    arc = FakeARC(hits=80, misses=20, conv_index_size=12, inv_index_size=42)
    return TestClient(build_app(sessions_path=tmp_path / "s.json", arc=arc))


def test_memory_stats_reports_cache_counters(client_with_arc: TestClient) -> None:
    resp = client_with_arc.get("/v1/memory/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cache"]["hits"] == 80
    assert body["cache"]["misses"] == 20
    assert body["cache"]["hit_rate"] == pytest.approx(0.8)
    assert body["cache"]["capacity"] == 1000


def test_memory_stats_reports_global_counters(client_with_arc: TestClient) -> None:
    body = client_with_arc.get("/v1/memory/stats").json()
    # 'global' is the JSON key (Python keyword aliased server-side).
    assert "global" in body, f"missing global key: {body}"
    g = body["global"]
    assert g["conversations_total"] == 12
    assert g["invocations_total"] == 42


def test_memory_stats_session_block_populated_when_session_id_set(
    client_with_arc: TestClient,
) -> None:
    sid = client_with_arc.post("/v1/sessions", json={"title": "x"}).json()["id"]
    client_with_arc.app.state.sessions.update(
        sid,
        message_count=3,
        add_tokens_input=120,
        add_tokens_output=80,
    )
    client_with_arc.app.state.messages[sid] = [
        Message(
            id="msg_1",
            session_id=sid,
            role="user",
            created_at="2026-05-27T00:00:00+00:00",
            updated_at="2026-05-27T00:00:00+00:00",
            parts=[Part(id="part_1", type="text", text="hello")],
            tokens=Tokens(input=12),
        ),
        Message(
            id="msg_2",
            session_id=sid,
            role="assistant",
            created_at="2026-05-27T00:00:01+00:00",
            updated_at="2026-05-27T00:00:01+00:00",
            parts=[Part(id="part_2", type="text", text="answer")],
            tokens=Tokens(output=18),
        ),
    ]
    body = client_with_arc.get(f"/v1/memory/stats?session_id={sid}").json()
    assert body["session"] is not None
    s = body["session"]
    assert s["session_id"] == sid
    assert s["messages_retained"] == 2
    assert s["tokens_retained"] == 30
    assert s["tokens_budget"] == 4000
    assert s["token_pressure"] == pytest.approx(30 / 4000)
    assert s["threshold_state"] == "normal"
    assert s["compaction_recommended"] is False
    assert body["metadata"]["session"]["recorded_lifetime_tokens"] == 200


def test_memory_stats_reports_retained_context_files_and_compaction_pressure(
    client_with_arc: TestClient,
) -> None:
    sid = client_with_arc.post("/v1/sessions", json={"title": "x"}).json()["id"]
    client_with_arc.app.state.messages[sid] = [
        Message(
            id="msg_compact",
            session_id=sid,
            role="assistant",
            created_at="2026-05-27T00:00:00+00:00",
            updated_at="2026-05-27T00:00:00+00:00",
            parts=[
                Part(
                    id="part_compact",
                    type="text",
                    text="[compact summary]\n" + ("x" * 12000),
                    metadata={"synthetic": "compact_summary"},
                )
            ],
            metadata={"synthetic": "compact_summary"},
        )
    ]
    client_with_arc.app.state.context_files[sid] = {
        "large.txt": {"path": "large.txt", "mode": "read", "size": 8192}
    }

    body = client_with_arc.get(f"/v1/memory/stats?session_id={sid}").json()

    s = body["session"]
    assert s["messages_retained"] == 1
    assert s["context_files_attached"] == 1
    assert s["compact_summaries"] == 1
    assert s["tokens_retained"] >= 5000
    assert s["threshold_state"] == "critical"
    assert s["compaction_recommended"] is True
    assert body["metadata"]["retained_context_source"] == "visible_gact_transcript"


def test_memory_stats_session_block_counts_context_files_by_mode(
    client_with_arc: TestClient,
    tmp_path: Path,
) -> None:
    sid = client_with_arc.post("/v1/sessions", json={"title": "x"}).json()["id"]
    read_file = tmp_path / "notes.md"
    pin_file = tmp_path / "dataset.csv"
    read_file.write_text("notes\n")
    pin_file.write_text("a,b\n")

    for path, mode in [(read_file, "read"), (pin_file, "pin"), ("scratch.txt", "edit")]:
        resp = client_with_arc.post(
            f"/v1/sessions/{sid}/context/files",
            json={"path": str(path), "mode": mode},
        )
        assert resp.status_code == 200

    body = client_with_arc.get(f"/v1/memory/stats?session_id={sid}").json()
    session = body["session"]
    assert session["context_files_attached"] == 3
    assert session["context_files_by_mode"] == {"edit": 1, "pin": 1, "read": 1}


def test_memory_stats_unknown_session_returns_empty_block_not_404(
    client_with_arc: TestClient,
) -> None:
    """SPEC §6.19 implies the endpoint always 200s — the TUI's
    footer chip can poll continuously without spamming 404s on a
    session that's about to be created."""

    resp = client_with_arc.get("/v1/memory/stats?session_id=sess_nope")
    assert resp.status_code == 200
    s = resp.json()["session"]
    assert s["session_id"] == "sess_nope"
    assert s["messages_retained"] == 0
    assert s["context_files_attached"] == 0
    assert s["context_files_by_mode"] == {}


def test_memory_stats_no_session_query_returns_no_session_block(
    client_with_arc: TestClient,
) -> None:
    body = client_with_arc.get("/v1/memory/stats").json()
    assert body["session"] is None, (
        f"session block should be absent without ?session_id; got {body['session']}"
    )


def test_memory_stats_without_arc_returns_zeros(tmp_path: Path) -> None:
    """When ARC isn't wired (smoke-boot scenarios, scaffold tests),
    the endpoint returns 200 with zero counters rather than crashing
    or 503ing — so the chip just renders 'cache --' instead of
    breaking."""

    app = build_app(sessions_path=tmp_path / "s.json", arc=None)
    with TestClient(app) as c:
        body = c.get("/v1/memory/stats").json()
        assert body["cache"]["hits"] == 0
        assert body["cache"]["misses"] == 0
        assert body["cache"]["hit_rate"] == 0.0
        assert body["global"]["conversations_total"] == 0


def _add_text_message(
    client: TestClient,
    session_id: str,
    *,
    message_id: str,
    role: str = "user",
    text: str,
    created_at: str,
) -> None:
    client.app.state.messages.setdefault(session_id, []).append(
        Message(
            id=message_id,
            session_id=session_id,
            role=role,
            created_at=created_at,
            updated_at=created_at,
            parts=[Part(id=f"{message_id}_part", type="text", text=text)],
        )
    )
    client.app.state.sessions.update(
        session_id,
        message_count=len(client.app.state.messages[session_id]),
    )


def _complete_turn_with_metadata(
    client: TestClient,
    sid: str,
    text: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    ack = client.post(
        f"/v1/sessions/{sid}/messages",
        json={"parts": [{"type": "text", "text": text}], "metadata": metadata},
    )
    assert ack.status_code == 200, ack.text
    user_id = ack.json()["message_id"]
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        for idx, message in enumerate(messages):
            if message["id"] == user_id:
                if idx > 0 and messages[idx - 1]["role"] == "assistant":
                    return messages[idx - 1]
                break
        time.sleep(0.02)
    raise AssertionError("turn did not settle")


def test_memory_search_defaults_to_session_scope(client_with_arc: TestClient) -> None:
    sid_a = client_with_arc.post(
        "/v1/sessions",
        json={"title": "NDP Monday"},
    ).json()["id"]
    sid_b = client_with_arc.post(
        "/v1/sessions",
        json={"title": "Unrelated Tuesday"},
    ).json()["id"]
    _add_text_message(
        client_with_arc,
        sid_a,
        message_id="msg_a",
        text="We inspected the NDP catalog for climate pressure datasets.",
        created_at="2026-05-25T12:00:00+00:00",
    )
    _add_text_message(
        client_with_arc,
        sid_b,
        message_id="msg_b",
        text="The visualization dashboard discussed climate pressure plots.",
        created_at="2026-05-26T12:00:00+00:00",
    )

    resp = client_with_arc.get(
        "/v1/memory/search",
        params={"query": "climate pressure", "session_id": sid_a},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["include_cross_session"] is False
    assert body["searched_sessions"] == [sid_a]
    assert [hit["session_id"] for hit in body["hits"]] == [sid_a]
    assert body["hits"][0]["metadata"]["cross_session"] is False


def test_memory_search_requires_explicit_cross_session_opt_in(
    client_with_arc: TestClient,
) -> None:
    resp = client_with_arc.get("/v1/memory/search", params={"query": "climate"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["error"] == "invalid_request"
    assert "set_include_cross_session" in body["error"]["details"]["recovery_actions"]


def test_memory_search_cross_session_returns_provenance(
    client_with_arc: TestClient,
) -> None:
    ws_science = client_with_arc.post(
        "/v1/workspaces",
        json={"name": "Science", "root_path": ""},
    ).json()["id"]
    ws_other = client_with_arc.post(
        "/v1/workspaces",
        json={"name": "Other", "root_path": ""},
    ).json()["id"]
    sid_a = client_with_arc.post(
        "/v1/sessions",
        json={"title": "Monday NDP work", "workspace_id": ws_science},
    ).json()["id"]
    sid_b = client_with_arc.post(
        "/v1/sessions",
        json={"title": "Tuesday follow-up", "workspace_id": ws_science},
    ).json()["id"]
    sid_other = client_with_arc.post(
        "/v1/sessions",
        json={"title": "Other workspace", "workspace_id": ws_other},
    ).json()["id"]
    _add_text_message(
        client_with_arc,
        sid_a,
        message_id="msg_monday",
        role="assistant",
        text="NDP catalog result: dataset alpha has pressure and temperature.",
        created_at="2026-05-25T12:00:00+00:00",
    )
    _add_text_message(
        client_with_arc,
        sid_b,
        message_id="msg_tuesday",
        role="assistant",
        text="Follow-up confirmed pressure anomalies in dataset beta.",
        created_at="2026-05-26T12:00:00+00:00",
    )
    _add_text_message(
        client_with_arc,
        sid_other,
        message_id="msg_other",
        role="assistant",
        text="Pressure appears here too but this is a different workspace.",
        created_at="2026-05-27T12:00:00+00:00",
    )

    resp = client_with_arc.get(
        "/v1/memory/search",
        params={
            "query": "pressure dataset",
            "session_id": sid_b,
            "workspace_id": ws_science,
            "include_cross_session": "true",
            "limit": 10,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["metadata"]["scope"] == "cross_session"
    assert set(body["searched_sessions"]) == {sid_a, sid_b}
    assert {hit["session_id"] for hit in body["hits"]} == {sid_a, sid_b}
    by_session = {hit["session_id"]: hit for hit in body["hits"]}
    assert by_session[sid_a]["session_title"] == "Monday NDP work"
    assert by_session[sid_a]["workspace_id"] == ws_science
    assert by_session[sid_a]["metadata"]["source"] == "gact_transcript"
    assert by_session[sid_a]["metadata"]["cross_session"] is True
    assert by_session[sid_b]["metadata"]["cross_session"] is False


def test_memory_search_unknown_session_404s(client_with_arc: TestClient) -> None:
    resp = client_with_arc.get(
        "/v1/memory/search",
        params={"query": "pressure", "session_id": "sess_missing"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["error"] == "not_found"


def test_turn_can_opt_into_cross_session_memory_context(tmp_path: Path) -> None:
    agent = FakeAgent()
    client = TestClient(
        build_app(
            sessions_path=tmp_path / "s.json",
            arc=FakeARC(hits=1, misses=0),
            agent=agent,
        )
    )
    ws_science = client.post(
        "/v1/workspaces",
        json={"name": "Science", "root_path": ""},
    ).json()["id"]
    sid_prior = client.post(
        "/v1/sessions",
        json={"title": "Monday NDP work", "workspace_id": ws_science},
    ).json()["id"]
    sid_current = client.post(
        "/v1/sessions",
        json={"title": "Wednesday planning", "workspace_id": ws_science},
    ).json()["id"]
    _add_text_message(
        client,
        sid_prior,
        message_id="msg_prior",
        role="assistant",
        text="NDP catalog result: dataset alpha has pressure and temperature.",
        created_at="2026-05-25T12:00:00+00:00",
    )

    assistant = _complete_turn_with_metadata(
        client,
        sid_current,
        "Based on recent work, draft next steps.",
        {
            "memory_search": {
                "enabled": True,
                "query": "pressure dataset",
                "include_cross_session": True,
                "workspace_id": ws_science,
                "limit": 5,
                "reason": "answer user request about recent work",
            }
        },
    )

    assert "## Explicit Memory Search Results" in agent.questions[0]
    assert "Monday NDP work" in agent.questions[0]
    assert "dataset alpha has pressure and temperature" in agent.questions[0]
    assert "Based on recent work, draft next steps." in agent.questions[0]
    assert assistant["metadata"]["memory_search"]["include_cross_session"] is True
    assert set(assistant["metadata"]["memory_search"]["searched_sessions"]) == {
        sid_prior,
        sid_current,
    }
    assert assistant["metadata"]["memory_search"]["hits"][0]["session_id"] == sid_prior
    assert assistant["metadata"]["memory_search"]["hits"][0]["cross_session"] is True
    events = client.app.state.bus._history.get(sid_current, [])
    assert "memory.search.completed" in [event.type for event in events]
