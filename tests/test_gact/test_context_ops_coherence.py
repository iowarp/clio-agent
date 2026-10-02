"""A context edit that would leave the agent's context unfoldable is refused up front.

The fold fails typed on an incoherent plane (a call without its result, a lone
observation, a kind it does not model). An edit through ``POST /context/ops`` that would
create one is refused with nothing applied -- never accepted and left to fail the next
turn. A plane that cannot be folded reaches clients as a typed ``context_fold_failed``,
never an untyped 500.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.arc.memory import ARCMemory
from clio_agent.gact.app import build_app

SCOPE = "agentA"


@pytest.fixture
def client_sid(tmp_path: Path) -> Any:
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    app = build_app(sessions_path=tmp_path / "s.json", arc=arc)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        arc.append_segment(sid, SCOPE, "user", {"text": "q"}, step=0)
        arc.append_segment(sid, SCOPE, "thought", {"text": "t"}, step=0)
        call = arc.append_segment(sid, SCOPE, "tool_call", {"id": "c0", "name": "s", "args": {}})
        obs = arc.append_segment(
            sid, SCOPE, "observation", {"call_id": "c0", "text": "hits", "is_error": False}
        )
        yield client, sid, arc, call.id, obs.id


def _error(response: Any) -> str:
    body = response.json()
    return (body.get("detail") or body)["error"]["error"]


def _op(client: TestClient, sid: str, **body: Any) -> Any:
    return client.post(f"/v1/sessions/{sid}/context/ops", json={"scope": SCOPE, **body})


def test_deleting_only_a_result_is_refused_and_applies_nothing(client_sid: Any) -> None:
    client, sid, arc, _call, obs = client_sid

    response = _op(client, sid, op="delete", ids=[obs])

    assert response.status_code == 409, response.json()
    assert _error(response) == "context_fold_failed"
    assert obs in {s.id for s in arc.render_working_set(sid, SCOPE)}


def test_deleting_the_call_with_its_result_applies(client_sid: Any) -> None:
    client, sid, _arc, call, obs = client_sid

    assert _op(client, sid, op="delete", ids=[call, obs]).status_code == 200


def test_appending_a_lone_observation_is_refused(client_sid: Any) -> None:
    client, sid, _arc, _call, _obs = client_sid

    response = _op(client, sid, op="append", kind="observation", content={"text": "x"})

    assert response.status_code == 409, response.json()
    assert _error(response) == "context_fold_failed"


def test_appending_a_kind_the_agent_context_does_not_model_is_refused(client_sid: Any) -> None:
    client, sid, _arc, _call, _obs = client_sid

    response = _op(client, sid, op="append", kind="system", content={"text": "be terse"})

    assert response.status_code == 409, response.json()
    assert _error(response) == "context_fold_failed"


def test_appending_a_user_note_applies(client_sid: Any) -> None:
    client, sid, _arc, _call, _obs = client_sid

    assert _op(client, sid, op="append", kind="user", content={"text": "note"}).status_code == 200


def test_an_unfoldable_plane_is_a_typed_error_on_context_state(client_sid: Any) -> None:
    client, sid, arc, _call, obs = client_sid
    arc.delete_segments(sid, SCOPE, [obs])  # an internal writer broke the plane

    response = client.get(f"/v1/sessions/{sid}/context/state", params={"scope": SCOPE})

    assert response.status_code == 409, response.json()
    assert _error(response) == "context_fold_failed"
