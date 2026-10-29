"""DELETE of a trusted (spawned) session confirms its lifecycle off the loop.

A session carrying a ``creation_digest`` (every spawned child) refuses lifecycle
confirmation on the event loop. The route must hand the store delete to an off-loop
owner, or the client sees a 500 for a delete that already happened and the
``session.deleted`` event is never confirmed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import clio_agent.gact.provenance_wiring as provenance_wiring
from clio_agent.gact.app import build_app


def test_delete_trusted_child_session_returns_204_and_emits_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    emitted: list[tuple[str, str]] = []
    emit = provenance_wiring._emit_semantic_event

    def _recording_emit(app: Any, sid: str, event_type: str, **kwargs: Any) -> dict[str, Any]:
        result = emit(app, sid, event_type, **kwargs)
        emitted.append((event_type, sid))
        return result

    monkeypatch.setattr(provenance_wiring, "_emit_semantic_event", _recording_emit)
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "parent"}).json()
        # Prepared and initialized off the loop, the way spawn creates a child.
        store = app.state.sessions
        child = store.create(
            workspace_id=parent["workspace_id"],
            parent_session_id=parent["id"],
            agent={"id": "main"},
            creation_id="spawn:trusted-delete",
            creation_digest="a" * 64,
            defer_initialization=True,
        )
        store.initialize(child.id)
        assert store.get(child.id).creation_digest
        assert ("session.created", child.id) in emitted

        resp = client.delete(f"/v1/sessions/{child.id}")

        assert resp.status_code == 204, resp.text
        assert client.get(f"/v1/sessions/{child.id}").status_code == 404
        assert ("session.deleted", child.id) in emitted
