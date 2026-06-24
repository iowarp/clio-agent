"""Thread A: REST exposure of the ARC live context plane.

GET /v1/sessions/{sid}/context/state  -> %used, block counts, token categories, render
POST /v1/sessions/{sid}/context/ops   -> apply append/insert/delete/summarize
plus the redacted arc.op SSE-bus frame for the TUI.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from clio_agent.arc.memory import ARCMemory
from clio_agent.gact.app import build_app

SCOPE = "agentA"


def _client(tmp_path: Path, arc) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "sessions.json", arc=arc))


def _session(client: TestClient) -> str:
    return client.post("/v1/sessions", json={"title": "t"}).json()["id"]


def test_get_context_state(tmp_path):
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    client = _client(tmp_path, arc)
    sid = _session(client)
    arc.append_segment(sid, SCOPE, "thought", {"text": "T0"}, step=0, token_count=5)
    arc.append_segment(sid, SCOPE, "tool_call", {"name": "a", "args": {}}, step=0)
    arc.append_segment(sid, SCOPE, "observation", {"text": "O0"}, step=0, token_count=10)

    r = client.get(f"/v1/sessions/{sid}/context/state", params={"scope": SCOPE})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["live_block_count"] == 3
    assert body["tokens_by_kind"] == {"thought": 5, "tool_call": 0, "observation": 10}
    assert body["live_tokens"] == 15
    assert body["pct_used"] is None  # no agent/_provider_config -> window unknown
    assert list(body["render_keys"].keys()) == [
        "thought_0",
        "tool_name_0",
        "tool_args_0",
        "observation_0",
    ]
    assert "O0" in body["render_text"]


def test_context_state_categories_and_autocompact(tmp_path):
    """The /context view buckets per-kind tokens into Claude-Code-/context-style
    categories and surfaces the auto-compaction threshold for the UI."""
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    client = _client(tmp_path, arc)
    sid = _session(client)
    arc.append_segment(sid, SCOPE, "thought", {"text": "T0"}, step=0, token_count=5)
    arc.append_segment(sid, SCOPE, "observation", {"text": "O0"}, step=0, token_count=10)
    arc.append_segment(sid, SCOPE, "tool_def", {"name": "fs"}, step=0, token_count=3)

    body = client.get(f"/v1/sessions/{sid}/context/state", params={"scope": SCOPE}).json()
    assert body["categories"] == {"reasoning": 5, "observations": 10, "tools": 3}
    assert body["autocompact_pct"] == 0.85  # default trigger fraction
    # No LM call in-test -> model-grounded reading is unavailable (no framing entry).
    assert body["used_tokens"] is None
    assert "framing" not in body["categories"]


def test_post_context_compact_nothing_409(tmp_path):
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    client = _client(tmp_path, arc)
    sid = _session(client)
    r = client.post(f"/v1/sessions/{sid}/context/compact", params={"scope": SCOPE})
    assert r.status_code == 409


def test_post_context_compact_no_summary_503(tmp_path, monkeypatch):
    import clio_agent.gact.agents.runtime as runtime_mod

    monkeypatch.setattr(runtime_mod, "_summarize_segments_llm", lambda segs: "")
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    client = _client(tmp_path, arc)
    sid = _session(client)
    arc.append_segment(sid, SCOPE, "thought", {"text": "T0"}, step=0, token_count=5)
    r = client.post(f"/v1/sessions/{sid}/context/compact", params={"scope": SCOPE})
    assert r.status_code == 503


def test_post_context_compact_summarizes_working_set(tmp_path, monkeypatch):
    """Manual compaction collapses the live working-set into one summary segment via the
    sanctioned summarize op (the same summarizer the auto-compactor uses)."""
    import clio_agent.gact.agents.runtime as runtime_mod

    monkeypatch.setattr(runtime_mod, "_summarize_segments_llm", lambda segs: "COMPACT_SUMMARY")
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    client = _client(tmp_path, arc)
    sid = _session(client)
    arc.append_segment(sid, SCOPE, "thought", {"text": "T0"}, step=0, token_count=5)
    arc.append_segment(sid, SCOPE, "tool_call", {"name": "a", "args": {}}, step=0)
    arc.append_segment(sid, SCOPE, "observation", {"text": "O0"}, step=0, token_count=10)

    r = client.post(f"/v1/sessions/{sid}/context/compact", params={"scope": SCOPE})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["live_block_count"] == 1  # three segments -> one summary
    assert "COMPACT_SUMMARY" in body["render_text"]


def test_get_context_state_unknown_session_404(tmp_path):
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    client = _client(tmp_path, arc)
    r = client.get("/v1/sessions/nope/context/state", params={"scope": SCOPE})
    assert r.status_code == 404


def test_get_context_state_arc_disabled_503(tmp_path):
    client = TestClient(build_app(sessions_path=tmp_path / "sessions.json", arc=None))
    sid = _session(client)
    r = client.get(f"/v1/sessions/{sid}/context/state", params={"scope": SCOPE})
    assert r.status_code == 503


def test_post_context_op_append_then_delete(tmp_path):
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    client = _client(tmp_path, arc)
    sid = _session(client)

    r = client.post(
        f"/v1/sessions/{sid}/context/ops",
        json={
            "op": "append",
            "scope": SCOPE,
            "kind": "observation",
            "content": {"text": "NEEDLE"},
            "token_count": 7,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["live_block_count"] == 1
    assert r.json()["tokens_by_kind"] == {"observation": 7}

    state = client.get(f"/v1/sessions/{sid}/context/state", params={"scope": SCOPE}).json()
    assert "NEEDLE" in str(state["render_keys"])

    seg_id = arc.render_segments(sid, SCOPE)[0].id
    r2 = client.post(
        f"/v1/sessions/{sid}/context/ops",
        json={"op": "delete", "scope": SCOPE, "ids": [seg_id]},
    )
    assert r2.status_code == 200
    assert r2.json()["tombstoned_count"] == 1
    state2 = client.get(f"/v1/sessions/{sid}/context/state", params={"scope": SCOPE}).json()
    assert "NEEDLE" not in str(state2["render_keys"])


def test_post_context_op_invalid_op_rejected(tmp_path):
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    client = _client(tmp_path, arc)
    sid = _session(client)
    # Literal["append","insert","delete","summarize"] -> pydantic 422 at the request model
    r = client.post(f"/v1/sessions/{sid}/context/ops", json={"op": "frobnicate", "scope": SCOPE})
    assert r.status_code == 422


def test_post_context_op_insert_without_position_400(tmp_path):
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    client = _client(tmp_path, arc)
    sid = _session(client)
    # insert needs a position; missing -> TypeError in the op -> wrapped 400
    r = client.post(
        f"/v1/sessions/{sid}/context/ops",
        json={"op": "insert", "scope": SCOPE, "kind": "thought", "content": {"text": "x"}},
    )
    assert r.status_code == 400


def test_search_context_endpoint(tmp_path):
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    client = _client(tmp_path, arc)
    sid = _session(client)
    arc.append_segment(
        sid,
        "agentA/hdf5",
        "observation",
        {"text": "HDF5 dataset chunk compression filters"},
        step=0,
    )
    arc.append_segment(
        sid,
        "agentA/seismic",
        "observation",
        {"text": "earthquake waveform magnitude epicenter"},
        step=0,
    )
    r = client.get(f"/v1/sessions/{sid}/context/search", params={"q": "HDF5 compression"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["semantic"] is False  # LocalFS in tests
    assert body["hits"] and body["hits"][0]["scope"] == "agentA/hdf5"


def test_search_context_404_503(tmp_path):
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    client = _client(tmp_path, arc)
    assert client.get("/v1/sessions/nope/context/search", params={"q": "x"}).status_code == 404
    client2 = TestClient(build_app(sessions_path=tmp_path / "s2.json", arc=None))
    sid = _session(client2)
    assert client2.get(f"/v1/sessions/{sid}/context/search", params={"q": "x"}).status_code == 503


def test_context_op_publishes_redacted_arc_op_frame(tmp_path, monkeypatch):
    """An applied op publishes an arc.op SSE frame to the bus, redacted to
    ids/kinds/token_count (never content)."""
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    app = build_app(sessions_path=tmp_path / "sessions.json", arc=arc)
    client = TestClient(app)
    sid = _session(client)

    published: list = []
    original = app.state.bus.publish
    monkeypatch.setattr(
        app.state.bus, "publish", lambda ev: (published.append(ev), original(ev))[1]
    )

    client.post(
        f"/v1/sessions/{sid}/context/ops",
        json={
            "op": "append",
            "scope": SCOPE,
            "kind": "observation",
            "content": {"text": "SECRET_CONTENT"},
            "token_count": 3,
        },
    )
    arc_ops = [e for e in published if getattr(e, "type", None) == "arc.op"]
    assert arc_ops, "expected an arc.op frame on the bus"
    payload = arc_ops[-1].payload
    assert payload["op"] == "append" and payload["scope"] == SCOPE
    assert "SECRET_CONTENT" not in str(payload)  # content redacted
    for s in payload["segments_written"]:
        assert set(s.keys()) <= {"id", "kind", "token_count"}
