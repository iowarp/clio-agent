"""Tests for the #232 query params on the message-list and session-list routes.

Two protocol-convergence items the Go TUI already SENDS but the server used to
silently ignore:

* ``GET /v1/sessions/{sid}/messages`` — ``include_system`` / ``limit`` / ``before``
  paging plus a real ``next_cursor`` (was always ``null``). Omitting every param
  must reproduce the historical full-ledger, newest-first, ``next_cursor: null``
  behaviour, including the live in-flight assistant projection on the newest page.
* ``GET /v1/sessions`` — ``parent_session_id`` fork-lineage filter.

The tests drive the FastAPI app with a ``TestClient`` and inject messages
straight into ``app.state.messages`` (a plain dict keyed by session id) so the
paging/ordering contract is exercised without a real LM turn.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.types import Message, Part


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "sessions.json", agent=None))


def _create_session(client: TestClient, title: str = "t") -> str:
    return client.post("/v1/sessions", json={"title": title}).json()["id"]


def _msg(sid: str, mid: str, role: str, ordinal: int) -> Message:
    """Build a stored message. ``ordinal`` drives a sortable ISO timestamp so the
    chronological order in ``app.state.messages`` is unambiguous."""

    ts = f"2026-07-09T00:00:{ordinal:02d}+00:00"
    return Message(
        id=mid,
        session_id=sid,
        turn_id=mid,
        role=role,  # type: ignore[arg-type]
        created_at=ts,
        updated_at=ts,
        parts=[Part(id=f"{mid}_p0", type="text", text=f"{role} {ordinal}")],
    )


def _seed(client: TestClient, sid: str, spec: list[tuple[str, str]]) -> None:
    """Populate the session ledger chronologically. ``spec`` is [(id, role), ...]
    in chronological (oldest-first) order."""

    rows = [_msg(sid, mid, role, i) for i, (mid, role) in enumerate(spec)]
    client.app.state.messages[sid] = rows  # type: ignore[attr-defined]


# ---- messages: defaults unchanged ------------------------------------------


def test_no_params_reproduces_full_newest_first_ledger(client: TestClient) -> None:
    sid = _create_session(client)
    _seed(
        client,
        sid,
        [("m0", "user"), ("m1", "assistant"), ("m2", "user"), ("m3", "assistant")],
    )

    body = client.get(f"/v1/sessions/{sid}/messages").json()
    assert [m["id"] for m in body["messages"]] == ["m3", "m2", "m1", "m0"]
    assert body["next_cursor"] is None


def test_empty_session_unchanged(client: TestClient) -> None:
    sid = _create_session(client)
    body = client.get(f"/v1/sessions/{sid}/messages").json()
    assert body["messages"] == []
    assert body["next_cursor"] is None


def test_empty_session_does_not_fetch_a_missing_persistent_blob(client: TestClient) -> None:
    sid = _create_session(client)

    class MissingBlobStore:
        def get(self, _key: str, _default: object = None) -> object:
            raise RuntimeError("GetBlob operation failed")

    client.app.state.messages = MissingBlobStore()  # type: ignore[attr-defined]

    response = client.get(
        f"/v1/sessions/{sid}/messages",
        headers={"X-GACT-Version": "0.3"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "messages": [],
        "tools": [],
        "tasks": [],
        "subagents": [],
        "artifacts": [],
        "surfaces": [],
    }


# ---- messages: include_system ----------------------------------------------


def test_include_system_default_keeps_system_messages(client: TestClient) -> None:
    sid = _create_session(client)
    _seed(client, sid, [("m0", "system"), ("m1", "user"), ("m2", "assistant")])

    body = client.get(f"/v1/sessions/{sid}/messages").json()
    assert [m["id"] for m in body["messages"]] == ["m2", "m1", "m0"]


def test_include_system_false_drops_system_messages(client: TestClient) -> None:
    sid = _create_session(client)
    _seed(client, sid, [("m0", "system"), ("m1", "user"), ("m2", "assistant")])

    body = client.get(
        f"/v1/sessions/{sid}/messages", params={"include_system": "false"}
    ).json()
    assert [m["id"] for m in body["messages"]] == ["m2", "m1"]
    assert all(m["role"] != "system" for m in body["messages"])
    assert body["next_cursor"] is None


# ---- messages: limit + next_cursor -----------------------------------------


def test_limit_caps_to_newest_and_sets_next_cursor_when_truncated(
    client: TestClient,
) -> None:
    sid = _create_session(client)
    _seed(
        client,
        sid,
        [("m0", "user"), ("m1", "assistant"), ("m2", "user"), ("m3", "assistant")],
    )

    body = client.get(f"/v1/sessions/{sid}/messages", params={"limit": 2}).json()
    # newest 2, newest-first
    assert [m["id"] for m in body["messages"]] == ["m3", "m2"]
    # oldest in the returned page, because older rows (m1, m0) remain
    assert body["next_cursor"] == "m2"


def test_limit_not_truncating_returns_null_next_cursor(client: TestClient) -> None:
    sid = _create_session(client)
    _seed(client, sid, [("m0", "user"), ("m1", "assistant")])

    body = client.get(f"/v1/sessions/{sid}/messages", params={"limit": 5}).json()
    assert [m["id"] for m in body["messages"]] == ["m1", "m0"]
    assert body["next_cursor"] is None


def test_limit_exactly_page_size_returns_null_next_cursor(client: TestClient) -> None:
    sid = _create_session(client)
    _seed(client, sid, [("m0", "user"), ("m1", "assistant")])

    body = client.get(f"/v1/sessions/{sid}/messages", params={"limit": 2}).json()
    assert [m["id"] for m in body["messages"]] == ["m1", "m0"]
    assert body["next_cursor"] is None


def test_limit_zero_is_422(client: TestClient) -> None:
    sid = _create_session(client)
    _seed(client, sid, [("m0", "user")])

    resp = client.get(f"/v1/sessions/{sid}/messages", params={"limit": 0})
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["error"] == "validation_error"
    assert err["details"]["session_id"] == sid


def test_limit_negative_is_422(client: TestClient) -> None:
    sid = _create_session(client)
    resp = client.get(f"/v1/sessions/{sid}/messages", params={"limit": -3})
    assert resp.status_code == 422
    assert resp.json()["error"]["error"] == "validation_error"


# ---- messages: before ------------------------------------------------------


def test_before_returns_only_strictly_older_messages(client: TestClient) -> None:
    sid = _create_session(client)
    _seed(
        client,
        sid,
        [("m0", "user"), ("m1", "assistant"), ("m2", "user"), ("m3", "assistant")],
    )

    # strictly older than m2 chronologically -> m1, m0, newest-first
    body = client.get(f"/v1/sessions/{sid}/messages", params={"before": "m2"}).json()
    assert [m["id"] for m in body["messages"]] == ["m1", "m0"]
    assert body["next_cursor"] is None


def test_before_oldest_message_returns_empty(client: TestClient) -> None:
    sid = _create_session(client)
    _seed(client, sid, [("m0", "user"), ("m1", "assistant")])

    body = client.get(f"/v1/sessions/{sid}/messages", params={"before": "m0"}).json()
    assert body["messages"] == []
    assert body["next_cursor"] is None


def test_before_unknown_id_is_404(client: TestClient) -> None:
    sid = _create_session(client)
    _seed(client, sid, [("m0", "user"), ("m1", "assistant")])

    resp = client.get(
        f"/v1/sessions/{sid}/messages", params={"before": "msg_missing"}
    )
    assert resp.status_code == 404
    err = resp.json()["error"]
    assert err["error"] == "not_found"
    assert err["details"] == {"session_id": sid, "message_id": "msg_missing"}


def test_before_and_limit_and_include_system_combined(client: TestClient) -> None:
    sid = _create_session(client)
    _seed(
        client,
        sid,
        [
            ("m0", "system"),
            ("m1", "user"),
            ("m2", "assistant"),
            ("m3", "system"),
            ("m4", "user"),
            ("m5", "assistant"),
        ],
    )

    # before m5 -> chronological m0..m4; drop system (m0, m3) -> m1,m2,m4;
    # newest-first -> m4,m2,m1; limit 2 -> m4,m2 with older rows remaining.
    body = client.get(
        f"/v1/sessions/{sid}/messages",
        params={"before": "m5", "limit": 2, "include_system": "false"},
    ).json()
    assert [m["id"] for m in body["messages"]] == ["m4", "m2"]
    assert body["next_cursor"] == "m2"


# ---- messages: live in-flight assistant ------------------------------------


def _install_live_assistant(client: TestClient, sid: str, mid: str) -> None:
    app = client.app  # type: ignore[attr-defined]
    live_ids = getattr(app.state, "live_assistant_message_ids", None)
    if live_ids is None:
        live_ids = {}
        app.state.live_assistant_message_ids = live_ids
    live_parts = getattr(app.state, "live_assistant_parts", None)
    if live_parts is None:
        live_parts = {}
        app.state.live_assistant_parts = live_parts
    live_ids[sid] = mid
    live_parts[sid] = [Part(id=f"{mid}_p0", type="text", text="streaming…")]


def test_live_message_appears_only_on_newest_page(client: TestClient) -> None:
    sid = _create_session(client)
    _seed(client, sid, [("m0", "user"), ("m1", "assistant"), ("m2", "user")])
    _install_live_assistant(client, sid, "msg_live")

    # Newest page (no before): the live in-flight assistant is the newest row.
    newest = client.get(f"/v1/sessions/{sid}/messages").json()
    assert [m["id"] for m in newest["messages"]] == ["msg_live", "m2", "m1", "m0"]
    assert newest["messages"][0]["metadata"]["live"] is True

    # Paginating into the past (before set) must NOT append the live message.
    past = client.get(f"/v1/sessions/{sid}/messages", params={"before": "m2"}).json()
    assert [m["id"] for m in past["messages"]] == ["m1", "m0"]
    assert all("msg_live" != m["id"] for m in past["messages"])


def test_live_message_before_cursor_unknown_is_404(client: TestClient) -> None:
    sid = _create_session(client)
    _seed(client, sid, [("m0", "user")])
    _install_live_assistant(client, sid, "msg_live")

    # The live projection is not a stored message, so it is not a valid cursor.
    resp = client.get(
        f"/v1/sessions/{sid}/messages", params={"before": "msg_live"}
    )
    assert resp.status_code == 404


def test_limit_on_newest_page_counts_live_message(client: TestClient) -> None:
    sid = _create_session(client)
    _seed(client, sid, [("m0", "user"), ("m1", "assistant"), ("m2", "user")])
    _install_live_assistant(client, sid, "msg_live")

    body = client.get(f"/v1/sessions/{sid}/messages", params={"limit": 2}).json()
    # newest two are the live message + m2; older rows remain -> next_cursor = m2
    assert [m["id"] for m in body["messages"]] == ["msg_live", "m2"]
    assert body["next_cursor"] == "m2"


# ---- messages: unknown session ---------------------------------------------


def test_messages_unknown_session_is_404(client: TestClient) -> None:
    resp = client.get("/v1/sessions/sess_nope/messages", params={"limit": 2})
    assert resp.status_code == 404
    assert resp.json()["error"]["error"] == "not_found"


# ---- sessions: parent_session_id -------------------------------------------


def test_parent_session_id_filters_to_subsessions(client: TestClient) -> None:
    parent = _create_session(client, title="parent")
    # Fork twice off the parent so the children carry parent_session_id=parent.
    child_a = client.post(f"/v1/sessions/{parent}/fork", json={}).json()["id"]
    child_b = client.post(f"/v1/sessions/{parent}/fork", json={}).json()["id"]
    # An unrelated top-level session (no parent).
    _create_session(client, title="unrelated")

    rows = client.get(
        "/v1/sessions", params={"parent_session_id": parent}
    ).json()["sessions"]
    assert {r["id"] for r in rows} == {child_a, child_b}
    assert all(r["parent_session_id"] == parent for r in rows)


def test_parent_session_id_non_matching_returns_empty(client: TestClient) -> None:
    _create_session(client, title="a")
    _create_session(client, title="b")

    rows = client.get(
        "/v1/sessions", params={"parent_session_id": "sess_nobody"}
    ).json()["sessions"]
    assert rows == []


def test_parent_session_id_omitted_is_unchanged(client: TestClient) -> None:
    parent = _create_session(client, title="parent")
    child = client.post(f"/v1/sessions/{parent}/fork", json={}).json()["id"]

    rows = client.get("/v1/sessions").json()["sessions"]
    ids = {r["id"] for r in rows}
    assert parent in ids
    assert child in ids


def test_parent_session_id_empty_string_is_unchanged(client: TestClient) -> None:
    parent = _create_session(client, title="parent")
    child = client.post(f"/v1/sessions/{parent}/fork", json={}).json()["id"]

    rows = client.get(
        "/v1/sessions", params={"parent_session_id": ""}
    ).json()["sessions"]
    ids = {r["id"] for r in rows}
    assert parent in ids
    assert child in ids


def test_parent_session_id_combines_with_workspace_scope(client: TestClient) -> None:
    # A workspace distinct from ws_default.
    wid = client.post("/v1/workspaces", json={"name": "other"}).json()["id"]
    parent_default = _create_session(client, title="parent-default")
    client.post(f"/v1/sessions/{parent_default}/fork", json={})

    # A parent + child living in the OTHER workspace.
    parent_other = client.post(
        "/v1/sessions", json={"title": "parent-other", "workspace_id": wid}
    ).json()["id"]
    # Forks inherit the source workspace, so this child is in `wid` too.
    child_other = client.post(f"/v1/sessions/{parent_other}/fork", json={}).json()["id"]

    # Scope to the other workspace + that parent: only its child, not the
    # default-workspace fork, comes back.
    rows = client.get(
        "/v1/sessions",
        params={"workspace_id": wid, "parent_session_id": parent_other},
    ).json()["sessions"]
    assert {r["id"] for r in rows} == {child_other}
