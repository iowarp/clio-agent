"""Thread A: REST exposure of the ARC live context plane.

GET /v1/sessions/{sid}/context/state  -> %used, block counts, token categories, render
POST /v1/sessions/{sid}/context/ops   -> apply append/insert/delete/summarize
plus the redacted arc.op SSE-bus frame for the TUI.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from clio_agent.arc.memory import ARCMemory
from clio_agent.gact.app import build_app

SCOPE = "agentA"


def _client(tmp_path: Path, arc) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "sessions.json", arc=arc))


def _session(client: TestClient) -> str:
    return client.post("/v1/sessions", json={"title": "t"}).json()["id"]


def test_get_context_state(tmp_path, monkeypatch):
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    client = _client(tmp_path, arc)
    sid = _session(client)
    arc.append_segment(sid, SCOPE, "thought", {"text": "T0"}, step=0, token_count=5)
    arc.append_segment(sid, SCOPE, "tool_call", {"id": "call_0", "name": "a", "args": {}}, step=0)
    arc.append_segment(
        sid,
        SCOPE,
        "observation",
        {"call_id": "call_0", "text": "O0", "is_error": False},
        step=0,
        token_count=10,
    )
    render_segments = arc.render_segments
    rendered_off_loop: list[bool] = []

    def audited_render_segments(*args: Any, **kwargs: Any) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            rendered_off_loop.append(True)
        else:
            rendered_off_loop.append(False)
        return render_segments(*args, **kwargs)

    monkeypatch.setattr(arc, "render_segments", audited_render_segments)

    r = client.get(f"/v1/sessions/{sid}/context/state", params={"scope": SCOPE})
    assert r.status_code == 200, r.text
    assert rendered_off_loop and all(rendered_off_loop)
    body = r.json()
    assert body["live_block_count"] == 3
    assert body["tokens_by_kind"] == {"thought": 5, "tool_call": 0, "observation": 10}
    assert body["live_tokens"] == 15
    assert body["pct_used"] is None  # no agent/_provider_config -> window unknown
    # The agent's context exactly as the model receives it: one step, its call, its result.
    assert body["messages"] == [
        {
            "role": "assistant",
            "parts": [
                {"type": "text", "text": "T0"},
                {"type": "tool_call", "id": "call_0", "name": "a", "input": {}},
            ],
        },
        {
            "role": "tool",
            "parts": [
                {
                    "type": "tool_result",
                    "id": "call_0",
                    "name": "a",
                    "is_error": False,
                    "content": [{"type": "text", "text": "O0"}],
                }
            ],
        },
    ]
    assert "O0" in body["render_text"]


def test_context_state_categories_and_autocompact(tmp_path):
    """The /context view buckets per-kind tokens into Claude-Code-/context-style
    categories and surfaces the auto-compaction threshold for the UI."""
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    client = _client(tmp_path, arc)
    sid = _session(client)
    arc.append_segment(sid, SCOPE, "thought", {"text": "T0"}, step=0, token_count=5)
    call = {"id": "call_0", "name": "fs", "args": {}}
    arc.append_segment(sid, SCOPE, "tool_call", call, step=0, token_count=3)
    obs = {"call_id": "call_0", "text": "O0", "is_error": False}
    arc.append_segment(sid, SCOPE, "observation", obs, step=0, token_count=10)

    body = client.get(f"/v1/sessions/{sid}/context/state", params={"scope": SCOPE}).json()
    assert body["categories"] == {"reasoning": 5, "observations": 10, "tool_calls": 3}
    assert body["autocompact_pct"] == 0.85  # default trigger fraction
    assert body["autocompact_enabled"] is True
    # No LM call in-test -> model-grounded reading is unavailable (no framing entry).
    assert body["used_tokens"] is None
    assert "framing" not in body["categories"]


def test_context_state_uses_durable_scope_owned_provider_usage(tmp_path):
    """Provider usage survives route calls and remains isolated by agent scope."""

    sessions_path = tmp_path / "sessions.json"
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    client = TestClient(build_app(sessions_path=sessions_path, arc=arc))
    sid = _session(client)
    client.app.state.sessions.update(
        sid,
        metadata_patch={
            "context_usage_by_scope": {
                SCOPE: {
                    "used_tokens": 1200,
                    "source": "provider",
                    "model": "anthropic/claude-sonnet-4-6",
                    "cache_read_tokens": 900,
                    "cache_write_tokens": 120,
                    "cache_tokens_measured": True,
                }
            }
        },
    )

    first = client.get(f"/v1/sessions/{sid}/context/state", params={"scope": SCOPE}).json()
    restarted = TestClient(build_app(sessions_path=sessions_path, arc=arc))
    after_restart = restarted.get(
        f"/v1/sessions/{sid}/context/state", params={"scope": SCOPE}
    ).json()
    unrelated = restarted.get(
        f"/v1/sessions/{sid}/context/state", params={"scope": "other-agent"}
    ).json()

    for state in (first, after_restart):
        assert state["used_tokens"] == 1200
        assert state["used_tokens_source"] == "provider"
        assert state["usage_model"] == "anthropic/claude-sonnet-4-6"
        assert state["cache_read_tokens"] == 900
        assert state["cache_write_tokens"] == 120
        assert state["cache_tokens_measured"] is True
    assert unrelated["used_tokens"] is None
    assert unrelated["cache_tokens_measured"] is False


def test_context_preferences_are_session_owned_and_reflected_in_state(tmp_path):
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    client = _client(tmp_path, arc)
    sid = _session(client)

    response = client.patch(
        f"/v1/sessions/{sid}/context/preferences",
        json={"automatic_compaction": False, "autocompact_pct": 0.72},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "session_id": sid,
        "automatic_compaction": False,
        "autocompact_pct": 0.72,
    }
    persisted = client.get(f"/v1/sessions/{sid}/context/preferences").json()
    assert persisted == response.json()
    state = client.get(f"/v1/sessions/{sid}/context/state", params={"scope": SCOPE}).json()
    assert state["autocompact_enabled"] is False
    assert state["autocompact_pct"] == 0.72


def test_context_preferences_reject_invalid_threshold(tmp_path):
    client = _client(tmp_path, ARCMemory(data_dir=str(tmp_path / "arc")))
    sid = _session(client)

    response = client.patch(
        f"/v1/sessions/{sid}/context/preferences",
        json={"autocompact_pct": 1.25},
    )

    assert response.status_code == 422


def test_the_scope_compact_route_is_gone(tmp_path):
    """Compaction is ONE operation (``POST /v1/sessions/{sid}/compact?scope=``)."""
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    client = _client(tmp_path, arc)
    sid = _session(client)
    arc.append_segment(sid, SCOPE, "thought", {"text": "T0"}, step=0, token_count=5)
    r = client.post(f"/v1/sessions/{sid}/context/compact", params={"scope": SCOPE})
    assert r.status_code in {404, 405}


def test_get_context_state_unknown_session_404(tmp_path):
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    client = _client(tmp_path, arc)
    r = client.get("/v1/sessions/nope/context/state", params={"scope": SCOPE})
    assert r.status_code == 404


def test_get_context_state_arc_disabled_503(tmp_path):
    """No ARC bound for the request (clio-core not attached yet) is a typed 503.

    Since b80479ea the first semantic event attaches the process ARC, so an
    ``arc=None`` build gains one as soon as a session is created; the session is made
    with an in-memory ARC and the app is then detached to reach the unattached state.
    """
    from clio_agent.arc.live import _MemoryStore
    from clio_agent.gact.runtime.globals import _set_app_arc

    app = build_app(
        sessions_path=tmp_path / "sessions.json",
        arc=ARCMemory(data_dir=str(tmp_path / "arc"), store=_MemoryStore()),
    )
    client = TestClient(app)
    sid = _session(client)
    _set_app_arc(app, None)
    r = client.get(f"/v1/sessions/{sid}/context/state", params={"scope": SCOPE})
    assert r.status_code == 503
    assert r.json()["error"]["error"] == "arc_unavailable"


def test_post_context_op_append_then_delete(tmp_path):
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    client = _client(tmp_path, arc)
    sid = _session(client)

    r = client.post(
        f"/v1/sessions/{sid}/context/ops",
        json={
            "op": "append",
            "scope": SCOPE,
            "kind": "user",
            "content": {"text": "NEEDLE"},
            "token_count": 7,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["live_block_count"] == 1
    assert r.json()["tokens_by_kind"] == {"user": 7}

    state = client.get(f"/v1/sessions/{sid}/context/state", params={"scope": SCOPE}).json()
    assert "NEEDLE" in str(state["messages"])

    seg_id = arc.render_segments(sid, SCOPE)[0].id
    r2 = client.post(
        f"/v1/sessions/{sid}/context/ops",
        json={"op": "delete", "scope": SCOPE, "ids": [seg_id]},
    )
    assert r2.status_code == 200
    assert r2.json()["tombstoned_count"] == 1
    state2 = client.get(f"/v1/sessions/{sid}/context/state", params={"scope": SCOPE}).json()
    assert "NEEDLE" not in str(state2["messages"])


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


def test_search_context_is_a_typed_503_when_clio_core_cannot_search(tmp_path):
    """#905: the iowarp-core wheels ship no indexer chimod, so clio-core cannot search;
    the route says so, typed, instead of answering with empty hits."""
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    client = _client(tmp_path, arc)
    sid = _session(client)
    arc.append_segment(sid, "agentA/hdf5", "observation", {"text": "HDF5 compression"}, step=0)

    r = client.get(f"/v1/sessions/{sid}/context/search", params={"q": "HDF5 compression"})

    assert r.status_code == 503, r.text
    error = r.json()["error"]
    assert error["error"] == "search_unavailable"
    assert error["details"]["reason"] == "clio_core_search_indexer_absent"


def test_search_context_404_503(tmp_path):
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    client = _client(tmp_path, arc)
    assert client.get("/v1/sessions/nope/context/search", params={"q": "x"}).status_code == 404
    client2 = TestClient(build_app(sessions_path=tmp_path / "s2.json", arc=None))
    sid = _session(client2)
    assert client2.get(f"/v1/sessions/{sid}/context/search", params={"q": "x"}).status_code == 503


class _CteShapedStore:
    """A minimal ``ARCStore`` double shaped like ``ClioCoreStore`` (#905): a backend
    that WOULD be semantic but currently reports degraded (indexer chimod absent) --
    no live clio-core daemon needed to exercise the route's reporting contract."""

    def __init__(self, degraded_reason: str) -> None:
        self._blobs: dict[tuple[str, str], bytes] = {}
        self._degraded_reason = degraded_reason

    def put(self, kind, name, data, *, tier="warm", search_text=None):
        self._blobs[(kind, name)] = data

    def get(self, kind, name):
        return self._blobs.get((kind, name))

    def exists(self, kind, name) -> bool:
        return (kind, name) in self._blobs

    def scan(self, kind, prefix=""):
        for (k, name), data in self._blobs.items():
            if k == kind and name.startswith(prefix):
                yield name, data

    def delete(self, kind, name) -> None:
        self._blobs.pop((kind, name), None)

    def clear(self) -> None:
        self._blobs.clear()

    def supports_search(self) -> bool:
        return not self._degraded_reason

    def search_degradation_reason(self) -> str:
        return self._degraded_reason

    def search(self, kind, query_text, *, name_prefix="", k=10):
        return []  # #905: a degraded clio-core-shaped backend reaches the bare core


def test_search_context_unavailable_is_typed_not_an_empty_result(tmp_path):
    """#905: a clio-core-shaped backend whose indexer chimod is absent is a typed 503
    with the reason, never a 200 with an empty list a caller could misread."""
    arc = ARCMemory(
        data_dir=str(tmp_path / "arc"), store=_CteShapedStore("clio_core_search_indexer_absent")
    )
    client = _client(tmp_path, arc)
    sid = _session(client)
    r = client.get(f"/v1/sessions/{sid}/context/search", params={"q": "HDF5"})
    assert r.status_code == 503, r.text
    assert r.json()["error"]["details"]["reason"] == "clio_core_search_indexer_absent"


def test_search_context_semantic_true_carries_no_reason(tmp_path):
    """The inverse: once a backend genuinely supports real search, the route never
    carries a stale/leftover degradation reason alongside ``semantic=True``."""
    arc = ARCMemory(data_dir=str(tmp_path / "arc"), store=_CteShapedStore(""))
    client = _client(tmp_path, arc)
    sid = _session(client)
    r = client.get(f"/v1/sessions/{sid}/context/search", params={"q": "x"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["semantic"] is True
    assert body["semantic_unavailable_reason"] == ""


def test_context_op_append_does_not_publish_arc_op_frame(tmp_path, monkeypatch):
    """WS1: ``arc.op`` segment bookkeeping is substrate, not served UI -- a plain
    ``append`` does NOT ride the SSE bus (the TUI reads context via GET
    /context/state), and no segment content can leak there. The durable trace + ARC
    still capture the op in full."""
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    app = build_app(sessions_path=tmp_path / "sessions.json", arc=arc)
    client = TestClient(app)
    sid = _session(client)

    published: list = []
    original = app.state.bus.publish
    monkeypatch.setattr(
        app.state.bus, "publish", lambda ev: (published.append(ev), original(ev))[1]
    )

    resp = client.post(
        f"/v1/sessions/{sid}/context/ops",
        json={
            "op": "append",
            "scope": SCOPE,
            "kind": "user",
            "content": {"text": "SECRET_CONTENT"},
            "token_count": 3,
        },
    )
    assert resp.status_code == 200, resp.text
    # The append is NOT published as an arc.op frame on the served bus...
    arc_ops = [e for e in published if getattr(e, "type", None) == "arc.op"]
    assert arc_ops == []
    # ...and no segment content leaks onto the bus at all.
    assert all("SECRET_CONTENT" not in str(getattr(e, "payload", "")) for e in published)
    # The op still actually landed (visible via the on-demand context state).
    state = client.get(f"/v1/sessions/{sid}/context/state", params={"scope": SCOPE}).json()
    assert any(seg.get("kind") == "user" for seg in state["segments"])
