"""A person's call before a session's first turn is owned by a settled anchor assignment."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.assignment_anchor import ANCHOR_INVOCATION, owning_assignment
from clio_agent.tools import call_origin


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=None)
    with TestClient(app) as test_client:
        yield test_client


def _session(client: TestClient) -> str:
    return client.post("/v1/sessions", json={"title": "fresh"}).json()["id"]


def _owner(client: TestClient, sid: str, *, view: bool) -> Any:
    app = cast(FastAPI, client.app)

    async def resolve() -> Any:
        if not view:
            return await owning_assignment(app, sid, "Add a site")
        with call_origin.scope("view"):
            return await owning_assignment(app, sid, "Add a site")

    return client.portal.call(resolve)


def test_the_agents_own_call_with_no_turn_has_no_owner(client: TestClient) -> None:
    sid = _session(client)

    assert _owner(client, sid, view=False) is None
    assert cast(FastAPI, client.app).state.task_history.latest_assignment(sid) is None


def test_a_view_call_before_any_turn_records_one_settled_anchor(client: TestClient) -> None:
    app = cast(FastAPI, client.app)
    sid = _session(client)

    anchor = _owner(client, sid, view=True)

    assert anchor.kind == "Session"
    assert anchor.owner.session_id == sid and anchor.owner.assignment_id == anchor.handle
    assert anchor.invocation_id.startswith(ANCHOR_INVOCATION)
    assert anchor.description == "Add a site"
    assert (anchor.status, anchor.settlement) == ("completed", "confirmed")
    assert anchor.terminal_at is not None
    assert app.state.task_history.latest_assignment(sid).handle == anchor.handle
    # No turn runs: no message, and the session stays idle.
    assert client.get(f"/v1/sessions/{sid}/messages").json()["messages"] == []
    assert not app.state.turn_runner.busy(sid)
    assert app.state.sessions.get(sid).status == "idle"
    # A later call is owned by the same anchor, not a new one.
    assert _owner(client, sid, view=True).handle == anchor.handle
    assert _owner(client, sid, view=False).handle == anchor.handle


def test_the_first_turn_after_an_anchor_starts_its_successor(client: TestClient) -> None:
    app = cast(FastAPI, client.app)
    sid = _session(client)
    anchor = _owner(client, sid, view=True)
    conversations, history = app.state.conversations, app.state.task_history

    with history.serialized():
        successor = conversations._new_assignment(
            sid,
            "hi",
            conversations.caller(sid).human_id,
            predecessor=anchor,
            invocation_id="test:" + sid,
        )

    assert successor.handle != anchor.handle
    assert history.latest_assignment(sid).handle == successor.handle
