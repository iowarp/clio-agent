"""tests for /v1/memory/stats.

Stats-only cases use fixed cache counters. Recall and turn cases use the actual
application lifespan, Core history and trusted session relationships. Covers:

- ARC wired -> cache + global stats reported
- ARC not wired -> zeros (per SPEC §6.19 'zeros are a valid signal')
- ?session_id with known session -> session block populated
- ?session_id with unknown session -> empty session block (NOT a 404)
- Wire shape uses 'global' (not 'global_') as the JSON key
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import CoordinationGrant
from clio_agent.gact import context as ctx
from clio_agent.gact.app import build_app
from clio_agent.gact.native_work import now
from clio_agent.gact.session_store import _replace_session_messages
from clio_agent.gact.summarization_record import summarization_part
from clio_agent.gact.types import Message, Part, Tokens
from clio_agent.gact.workspaces import Workspace
from tests.test_gact.test_streaming import _wait_for_turn_settlement

# #948 S4b: default sessions run the blueprint react ``main``; route it to each
# test's ``build_app(agent=...)`` host fake.
pytestmark = pytest.mark.usefixtures("host_agent_executor")


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
        self._highway_sink: Any = None

    def get_cache_stats(self) -> dict[str, Any]:
        return dict(self._stats)

    # The op-logger the app wires on every ARC (a failure to wire now raises).
    def set_segment_op_logger(self, _logger: Any) -> None:
        return None

    def list_segment_scopes(self, session_id: str, scope_prefix: str = "") -> list[str]:
        # No agent context on this stats-only stub (a rewind rolls back nothing).
        return []

    def set_highway_sink(self, sink: Any) -> None:
        # ARC-as-source: gact wires the highway-derive sink onto the arc via
        # _set_app_arc so a recorded event fans out to the trace/SSE/hooks.
        self._highway_sink = sink

    def record_semantic_event(self, event: Any) -> Any:
        # Mirror ARCMemory.record_semantic_event: persist (a no-op for this
        # stats-only stub) then DERIVE the highway. ARC is the source; the fail-loud
        # _emit_semantic_event requires every event to enter through this method.
        if self._highway_sink is not None:
            return self._highway_sink(event)
        return {}


class _StubProviderConfig:
    """Minimal stand-in exposing the handshake-resolved context window the
    memory-stats budget reads via ``_resolve_expert_context_window``."""

    def __init__(self, chosen_context: int = 4000) -> None:
        self.chosen_context = chosen_context
        self.model = "stub/model"


class _StubAgent:
    def __init__(self, chosen_context: int = 4000) -> None:
        self._provider_config = _StubProviderConfig(chosen_context)

    def forward(self, question: str, session_id: str) -> Any:
        """Keep stats-only fixtures from accidentally executing a model turn."""
        raise AssertionError("Stats-only agent must not execute inference")


class FakeAgent:
    def __init__(self) -> None:
        self.questions: list[str] = []
        self.injections: list[dict[str, str]] = []
        self._provider_config = _StubProviderConfig()

    def forward(self, question: str, session_id: str) -> Any:
        self.questions.append(question)
        self.injections.append(dict(ctx.turn_injections()))
        return type(
            "Pred",
            (),
            {
                "answer": "memory-aware answer",
                "selected_expert": "main",
                "routing_rationale": "fake",
            },
        )()


def _application(client: TestClient) -> FastAPI:
    """Narrow the actual application rather than assuming every ASGI callable has state."""
    app = client.app
    assert isinstance(app, FastAPI)
    return app


@pytest.fixture()
def client_with_arc(
    tmp_path: Path, arc: ARCMemory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    counters = FakeARC(hits=80, misses=20, conv_index_size=12, inv_index_size=42)
    monkeypatch.setattr(arc, "get_cache_stats", counters.get_cache_stats)
    with TestClient(
        build_app(sessions_path=tmp_path / "s.json", arc=arc, agent=_StubAgent())
    ) as client:
        yield client


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
    _application(client_with_arc).state.sessions.update(
        sid,
        message_count=3,
        add_tokens_input=120,
        add_tokens_output=80,
    )
    _application(client_with_arc).state.messages[sid] = [
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
    assert body["metadata"]["tokens_budget_source"] == "handshake_window"
    assert body["metadata"]["session"]["recorded_lifetime_tokens"] == 200


def test_memory_stats_budget_from_handshake_context_window(tmp_path: Path, arc: ARCMemory) -> None:
    """The retained-context budget is the handshake-resolved context window
    (``chosen_context`` on the live provider config), not the 4000 fossil."""

    client = TestClient(
        build_app(
            sessions_path=tmp_path / "s.json",
            arc=arc,
            agent=_StubAgent(chosen_context=32000),
        )
    )
    sid = client.post("/v1/sessions", json={"title": "x"}).json()["id"]
    _application(client).state.messages[sid] = [
        Message(
            id="msg_1",
            session_id=sid,
            role="user",
            created_at="2026-05-27T00:00:00+00:00",
            updated_at="2026-05-27T00:00:00+00:00",
            parts=[Part(id="part_1", type="text", text="hello")],
        ),
    ]
    body = client.get(f"/v1/memory/stats?session_id={sid}").json()
    assert body["session"]["tokens_budget"] == 32000
    assert body["metadata"]["tokens_budget_source"] == "handshake_window"


def test_memory_stats_budget_unknown_without_provider_config(
    tmp_path: Path, arc: ARCMemory
) -> None:
    """No live provider config (agent unwired / window undiscoverable) -> the
    budget is 0 and the source is surfaced as ``unknown`` (pressure stays 0)."""

    client = TestClient(build_app(sessions_path=tmp_path / "s.json", arc=arc))
    sid = client.post("/v1/sessions", json={"title": "x"}).json()["id"]
    _application(client).state.messages[sid] = [
        Message(
            id="msg_1",
            session_id=sid,
            role="user",
            created_at="2026-05-27T00:00:00+00:00",
            updated_at="2026-05-27T00:00:00+00:00",
            parts=[Part(id="part_1", type="text", text="hello")],
        ),
    ]
    body = client.get(f"/v1/memory/stats?session_id={sid}").json()
    assert body["session"]["tokens_budget"] == 0
    assert body["session"]["token_pressure"] == 0.0
    assert body["metadata"]["tokens_budget_source"] == "unknown"


def test_memory_stats_reports_retained_context_files_and_compaction_pressure(
    client_with_arc: TestClient,
) -> None:
    sid = client_with_arc.post("/v1/sessions", json={"title": "x"}).json()["id"]
    _application(client_with_arc).state.messages[sid] = [
        Message(
            id="msg_compact",
            session_id=sid,
            role="assistant",
            created_at="2026-05-27T00:00:00+00:00",
            updated_at="2026-05-27T00:00:00+00:00",
            parts=[
                summarization_part(
                    "x" * 12000,
                    trigger="auto",
                    compaction_id="cmp_1",
                    derived_from=["a1"],
                    compacted_message_ids=[],
                )
            ],
        )
    ]
    _application(client_with_arc).state.context_files[sid] = {
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
    role: Literal["user", "assistant", "system", "tool"] = "user",
    text: str,
    created_at: str,
) -> None:
    rows = list(_application(client).state.messages.get(session_id, []))
    rows.append(
        Message(
            id=message_id,
            session_id=session_id,
            role=role,
            created_at=created_at,
            updated_at=created_at,
            parts=[Part(id=f"{message_id}_part", type="text", text=text)],
        )
    )
    _replace_session_messages(_application(client), session_id, rows)
    _application(client).state.sessions.update(
        session_id,
        message_count=len(_application(client).state.messages[session_id]),
    )


def _grant_read(client: TestClient, recipient: str, target: str) -> CoordinationGrant:
    """Install a trusted human/configuration relationship, never model-supplied intent."""
    app = _application(client)
    grant = CoordinationGrant(
        id=f"fixture-read:{recipient}:{target}",
        granting_human=app.state.human_principal,
        recipient_session=recipient,
        target_session=target,
        target_scope="session",
        rights=("read",),
        created_at=now(),
    )
    app.state.task_history.record_grant(grant, observation_id=grant.id)
    return grant


def _complete_turn_with_metadata(
    client: TestClient,
    sid: str,
    text: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """POST a user message carrying ``metadata`` and wait for its turn to settle.

    ``client`` MUST be an ENTERED ``TestClient`` (``with TestClient(app) as
    client:``). A client that is only constructed gives every request its own
    transient anyio portal; the turn task the POST fires is scheduled on that
    portal's loop (turn_runner's app-lifetime loop is only bound during the
    ASGI lifespan, which an unentered client never runs) and is cancelled the
    instant the portal tears down when the POST response lands -- before the
    recording agent is ever reached. The turn then settles truthfully as
    ``cancelled`` and callers indexing into the agent's recorded calls raise
    IndexError (#1312's settlement-race class).

    30s (not the 5s this used to carry): post-S4b every turn builds and runs a
    real blueprint module, and a tighter bound flakes on slow 2-core CI runners
    under coverage. The wait returns the instant the task settles, so the
    bound only ever fires on a genuine hang.
    """

    ack = client.post(
        f"/v1/sessions/{sid}/messages",
        json={"parts": [{"type": "text", "text": text}], "metadata": metadata},
    )
    assert ack.status_code == 202, ack.text
    accepted = ack.json()
    _wait_for_turn_settlement(
        _application(client), sid, timeout=30.0, handle=accepted["task_handle"]
    )
    history = _application(client).state.task_history
    assignment = history.task_snapshot(accepted["task_handle"])
    assert assignment is not None and assignment.status == "completed", {
        "assignment": assignment,
        "result": history.read_content(assignment.result)
        if assignment is not None and assignment.result is not None
        else None,
    }
    messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
    for idx, message in enumerate(messages):
        if message["id"] == accepted["message_id"]:
            assert idx > 0 and messages[idx - 1]["role"] == "assistant"
            return messages[idx - 1]
    raise AssertionError("Settled turn has no accepted user message in its transcript")


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
        "/v1/session-recall/search",
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
    resp = client_with_arc.get("/v1/session-recall/search", params={"query": "climate"})
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
        "/v1/session-recall/search",
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
    assert by_session[sid_a]["metadata"]["scope"] == "workspace"
    assert by_session[sid_b]["metadata"]["cross_session"] is False
    assert by_session[sid_b]["metadata"]["scope"] == "session"
    assert body["metadata"]["workspace_scope"] == "workspace"


def test_memory_search_denies_other_workspace_active_session(
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
    sid_other = client_with_arc.post(
        "/v1/sessions",
        json={"title": "Other workspace", "workspace_id": ws_other},
    ).json()["id"]

    resp = client_with_arc.get(
        "/v1/session-recall/search",
        params={
            "query": "pressure dataset",
            "session_id": sid_other,
            "workspace_id": ws_science,
            "include_cross_session": "true",
        },
    )

    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["error"] == "permission_error"
    assert body["error"]["details"]["scope"] == "other_workspace"


def test_memory_search_marks_global_scope(client_with_arc: TestClient) -> None:
    _application(client_with_arc).state.workspaces._workspaces["ws_global"] = Workspace(  # type: ignore[attr-defined]
        id="ws_global",
        name="global",
        root_path="",
    )
    sid = client_with_arc.post(
        "/v1/sessions",
        json={"title": "Global insight", "workspace_id": "ws_global"},
    ).json()["id"]
    _add_text_message(
        client_with_arc,
        sid,
        message_id="msg_global",
        role="assistant",
        text="User-level insight about pressure dataset work.",
        created_at="2026-05-25T12:00:00+00:00",
    )

    resp = client_with_arc.get(
        "/v1/session-recall/search",
        params={
            "query": "pressure dataset",
            "workspace_id": "ws_global",
            "include_cross_session": "true",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["metadata"]["workspace_scope"] == "global"
    assert body["hits"][0]["metadata"]["scope"] == "global"


def test_memory_search_unknown_session_404s(client_with_arc: TestClient) -> None:
    resp = client_with_arc.get(
        "/v1/session-recall/search",
        params={"query": "pressure", "session_id": "sess_missing"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["error"] == "not_found"


def test_memory_tool_search_same_workspace_requires_and_records_trusted_grant(
    client_with_arc: TestClient,
) -> None:
    ws_science = client_with_arc.post(
        "/v1/workspaces",
        json={"name": "Science", "root_path": ""},
    ).json()["id"]
    sid_prior = client_with_arc.post(
        "/v1/sessions",
        json={"title": "Monday NDP work", "workspace_id": ws_science},
    ).json()["id"]
    sid_current = client_with_arc.post(
        "/v1/sessions",
        json={"title": "Wednesday follow-up", "workspace_id": ws_science},
    ).json()["id"]
    _add_text_message(
        client_with_arc,
        sid_prior,
        message_id="msg_prior",
        role="assistant",
        text="NDP catalog result: dataset alpha has pressure and temperature.",
        created_at="2026-05-25T12:00:00+00:00",
    )

    grant = _grant_read(client_with_arc, sid_current, sid_prior)
    resp = client_with_arc.post(
        f"/v1/sessions/{sid_current}/session-recall/search",
        json={
            "query": "pressure dataset",
            "scope": "current_workspace",
            "user_intent": "answer the user's request about work from the last few days",
            "caller": {"type": "agent", "agent_id": "orchestrator"},
            "limit": 5,
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tool"] == "session_search"
    assert body["metadata"]["policy_decision"] == "allow_core_relationship"
    assert body["metadata"]["provenance"]["source"] == "gact_memory_tool"
    assert body["hits"][0]["session_id"] == sid_prior
    assert body["hits"][0]["metadata"]["cross_session"] is True
    audit = _application(client_with_arc).state.session_recall_audit[-1]
    assert audit["tool_name"] == "session_search"
    assert audit["status"] == "completed"
    assert audit["policy_decision"] == "allow_core_relationship"
    recorded_agent = _application(client_with_arc).state.sessions.get(sid_current).agent["id"]
    assert body["metadata"]["caller"] == {"agent_id": recorded_agent}
    assert audit["caller"] == {"agent_id": recorded_agent}
    assert recorded_agent != "orchestrator"
    _application(client_with_arc).state.task_history.record_grant(
        grant.model_copy(update={"revoked_at": now()}), observation_id=grant.id + ":revoke"
    )
    revoked = client_with_arc.post(
        f"/v1/sessions/{sid_current}/session-recall/search",
        json={
            "query": "pressure dataset",
            "scope": "current_workspace",
            "user_intent": "The earlier human request still wants these results.",
        },
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["hits"] == []
    assert sid_prior not in revoked.json()["searched_sessions"]
    assert "dataset alpha" not in revoked.text


def test_memory_tool_search_excludes_ungranted_peers_despite_model_intent(
    client_with_arc: TestClient,
) -> None:
    ws_science = client_with_arc.post(
        "/v1/workspaces",
        json={"name": "Science", "root_path": ""},
    ).json()["id"]
    sid_current = client_with_arc.post(
        "/v1/sessions",
        json={"title": "Wednesday follow-up", "workspace_id": ws_science},
    ).json()["id"]
    sid_peer = client_with_arc.post(
        "/v1/sessions", json={"title": "Unshared work", "workspace_id": ws_science}
    ).json()["id"]
    _add_text_message(
        client_with_arc,
        sid_peer,
        message_id="unshared-result",
        text="pressure dataset restricted peer result",
        created_at="2026-05-25T12:00:00+00:00",
    )

    resp = client_with_arc.post(
        f"/v1/sessions/{sid_current}/session-recall/search",
        json={
            "query": "pressure dataset",
            "scope": "current_workspace",
            "user_intent": "The model says it is allowed to inspect every peer.",
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["hits"] == [] and sid_peer not in body["searched_sessions"]
    assert "restricted peer result" not in resp.text
    audit = _application(client_with_arc).state.session_recall_audit[-1]
    assert audit["policy_decision"] == "allow_core_relationship"
    assert audit["details"]["hit_count"] == 0


def test_session_recall_frame_route_same_workspace_with_provenance(
    client_with_arc: TestClient,
) -> None:
    ws_science = client_with_arc.post(
        "/v1/workspaces",
        json={"name": "Science", "root_path": ""},
    ).json()["id"]
    sid_prior = client_with_arc.post(
        "/v1/sessions",
        json={"title": "Monday NDP work", "workspace_id": ws_science},
    ).json()["id"]
    sid_current = client_with_arc.post(
        "/v1/sessions",
        json={"title": "Wednesday follow-up", "workspace_id": ws_science},
    ).json()["id"]
    frame = {
        "id": "ctx_test_1",
        "session_id": sid_prior,
        "turn_id": "msg_user_1",
        "user_message_id": "msg_user_1",
        "assistant_message_id": "msg_asst_1",
        "created_at": "2026-05-25T12:00:00+00:00",
        "updated_at": "2026-05-25T12:00:02+00:00",
        "status": "completed",
        "model": {"provider": "test", "model": "fake"},
        "agent": {"id": "orchestrator"},
        "prompt": {"profile": "heavy"},
        "items": [
            {
                "kind": "message",
                "source_id": "msg_user_1",
                "role": "user",
                "included": True,
                "reason": "visible transcript",
                "tokens_estimated": 12,
                "metadata": {"source": "gact_visible_transcript"},
            }
        ],
        "tokens_estimated": 12,
        "metadata": {"retained_context_source": "visible_gact_transcript"},
    }
    _application(client_with_arc).state.context_frames[sid_prior] = [frame]
    _grant_read(client_with_arc, sid_current, sid_prior)

    resp = client_with_arc.post(
        f"/v1/sessions/{sid_current}/session-recall/read-context-frame",
        json={
            "target_session_id": sid_prior,
            "frame_id": "ctx_test_1",
            "scope": "current_workspace",
            "user_intent": "reuse the context assembled for Monday's work",
            "caller": {"agent_id": "forged-frame-reader"},
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tool"] == "session_read_context_frame"
    assert body["frame"]["id"] == "ctx_test_1"
    assert body["frame"]["metadata"]["source"] == "gact_context_frame"
    assert body["metadata"]["policy_decision"] == "allow_core_relationship"
    assert body["metadata"]["provenance"]["target_session_id"] == sid_prior
    recorded_agent = _application(client_with_arc).state.sessions.get(sid_current).agent["id"]
    assert body["metadata"]["caller"] == {"agent_id": recorded_agent}
    assert _application(client_with_arc).state.session_recall_audit[-1]["caller"] == {
        "agent_id": recorded_agent
    }


def test_memory_tool_read_summary_denies_other_workspace(
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
    sid_current = client_with_arc.post(
        "/v1/sessions",
        json={"title": "Science", "workspace_id": ws_science},
    ).json()["id"]
    sid_other = client_with_arc.post(
        "/v1/sessions",
        json={"title": "Other", "workspace_id": ws_other},
    ).json()["id"]

    resp = client_with_arc.post(
        f"/v1/sessions/{sid_current}/session-recall/read-summary",
        json={
            "target_session_id": sid_other,
            "scope": "current_workspace",
            "user_intent": "look across my recent work",
            "caller": {"agent_id": "forged-summary-reader"},
        },
    )

    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["error"] == "session_recall_policy_denied"
    assert body["error"]["details"]["policy_decision"] == "deny_session_unavailable"
    recorded_agent = _application(client_with_arc).state.sessions.get(sid_current).agent["id"]
    assert _application(client_with_arc).state.session_recall_audit[-1]["caller"] == {
        "agent_id": recorded_agent
    }


def test_memory_tool_global_search_requires_global_scope(client_with_arc: TestClient) -> None:
    _application(client_with_arc).state.workspaces._workspaces["ws_global"] = Workspace(  # type: ignore[attr-defined]
        id="ws_global",
        name="global",
        root_path="",
    )
    sid_current = client_with_arc.post(
        "/v1/sessions",
        json={"title": "Workspace session"},
    ).json()["id"]
    sid_global = client_with_arc.post(
        "/v1/sessions",
        json={"title": "Global insight", "workspace_id": "ws_global"},
    ).json()["id"]
    _add_text_message(
        client_with_arc,
        sid_global,
        message_id="msg_global",
        role="assistant",
        text="User-level memory says pressure dataset decisions were stable.",
        created_at="2026-05-25T12:00:00+00:00",
    )
    _grant_read(client_with_arc, sid_current, sid_global)

    resp = client_with_arc.post(
        f"/v1/sessions/{sid_current}/session-recall/search",
        json={"query": "pressure dataset", "scope": "global", "limit": 5},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["metadata"]["policy_decision"] == "allow_core_relationship"
    assert body["metadata"]["policy_scope"] == "global"
    assert body["hits"][0]["session_id"] == sid_global


def test_memory_tool_search_excludes_rewound_messages(tmp_path: Path, arc: ARCMemory) -> None:
    app = build_app(sessions_path=tmp_path / "s.json", arc=arc)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "rewind memory"}).json()["id"]
        _add_text_message(
            client,
            sid,
            message_id="msg_keep",
            role="assistant",
            text="Keep this durable pressure dataset summary.",
            created_at="2026-05-25T12:00:00+00:00",
        )
        _add_text_message(
            client,
            sid,
            message_id="msg_delete",
            role="assistant",
            text="Remove this tombstone-only zircon clue from normal memory.",
            created_at="2026-05-25T12:01:00+00:00",
        )

        rewind = client.post(f"/v1/sessions/{sid}/rewind", json={"message_id": "msg_keep"})
        assert rewind.status_code == 200, rewind.text

        resp = client.post(
            f"/v1/sessions/{sid}/session-recall/search",
            json={"query": "zircon clue", "scope": "session"},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["hits"] == []
        summary = client.post(
            f"/v1/sessions/{sid}/session-recall/read-summary",
            json={},
        ).json()["summary"]
        assert summary["visible_message_ids"] == ["msg_keep"]
        assert summary["excluded_message_ids"] == ["msg_delete"]


def test_standard_agent_carries_memory_and_session_recall_tools() -> None:
    from clio_agent.gact.agents.native_tool_registry import build_native_tools
    from clio_agent.gact.agents.resolution import resolution_agent_def
    from clio_agent.plugins.service import PluginService

    resolution = PluginService().resolve("clio/standard", pin=False)
    names = set(resolution.tool_names)
    assert {"query_tasks", "observe_tasks", "send_agent_message"} <= names
    recall_tools = {"session_search", "session_read_summary", "session_read_context_frame"}
    memory_tools = {"memory_search", "memory_read", "create_memory", "update_memory"}
    assert recall_tools | memory_tools <= names  # the memory plugin every agent carries
    assert not {name for name in names if name.startswith("memory_") and name not in memory_tools}
    task_tools = {"query_tasks", "observe_tasks", "send_agent_message"}
    built, _gateway, withheld = build_native_tools(
        resolution_agent_def(resolution), sorted(task_tools), {}
    )
    assert set(built) == task_tools and withheld == {}
