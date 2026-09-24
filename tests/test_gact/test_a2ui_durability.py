"""S8 deliverable 4 (docs/design/a2ui-compat-campaign-2026-09.md, issue
#1374): ``a2ui``/``a2ui_action`` parts survive compaction and a full server
restart.

``test_session_rollback.py``/``test_message_delete.py`` already cover the
undo/delete preservation half (``preserve_a2ui``,
``routes/session_a2ui_preservation.py``) for the ``a2ui`` part type. This
module adds:

1. Compaction (``POST /v1/sessions/{sid}/compact``, #1339) appends a
   checkpoint row and keeps every prior row verbatim (the compaction design
   is additive, never destructive -- ``compaction.append_checkpoint``'s own
   docstring), so a live surface AND its action lifecycle record survive a
   real compaction pass unchanged.
2. A full server restart (a genuinely SEPARATE ``build_app()`` process,
   pointed at the same on-disk ``sessions.json``/``messages/`` store) keeps
   the client-capabilities memory (session metadata), the action record, and
   the surface state intact, and the SAME action envelope re-submitted after
   the restart is deduped by its durable idempotency key -- no duplicate
   delivery, no second turn spawned.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.a2ui_capabilities import client_capabilities, remember_client_capabilities
from clio_agent.gact.a2ui_catalogs.builtin import workspace_catalog_id
from clio_agent.gact.app import build_app

# Surface mechanics, not catalog policy: bare sessions resolve the builtin
# catalogs (tests/test_gact/conftest.py::a2ui_builtin_catalogs, v15 S8).
pytestmark = pytest.mark.usefixtures("a2ui_builtin_catalogs")

HEADERS = {"X-GACT-Version": "0.3", "X-A2UI-Version": "0.9.1"}
WORKSPACE_CATALOG_ID = workspace_catalog_id()


class _CapturingAgent:
    """Fake compact agent recording every prompt it summarises (mirrors
    test_compaction.py's own fixture -- this module needs the SAME contract
    since it drives the real ``/compact`` route, not a stub)."""

    def __init__(self, summaries: list[str] | None = None) -> None:
        self.prompts: list[str] = []
        self._summaries = list(summaries or [])
        self._default = "a summary"

    def _run_chat_agent(self, question: str, _session_id: str) -> str:
        self.prompts.append(question)
        if self._summaries:
            return self._summaries.pop(0)
        return self._default

    def _call_with_transient_provider_retries(self, _label: str, call: Callable[[], Any]) -> Any:
        return call()


def _stub_spawn(app: Any, monkeypatch: Any) -> list[Any]:
    spawned: list[Any] = []

    def _spawn(coro: Any, **_kwargs: Any) -> None:
        spawned.append(coro)
        coro.close()

    monkeypatch.setattr(app.state.turn_runner, "spawn", _spawn)
    return spawned


def _create_message(surface_id: str = "surface_1") -> dict[str, Any]:
    return {
        "version": "v0.9.1",
        "createSurface": {"surfaceId": surface_id, "catalogId": WORKSPACE_CATALOG_ID},
    }


def _components(surface_id: str = "surface_1") -> dict[str, Any]:
    return {
        "version": "v0.9.1",
        "updateComponents": {
            "surfaceId": surface_id,
            "components": [{"id": "root", "component": "Text", "text": "durability probe"}],
        },
    }


def _action(name: str, surface_id: str, timestamp: str, context: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": "v0.9.1",
        "action": {
            "name": name,
            "surfaceId": surface_id,
            "sourceComponentId": "root",
            "timestamp": timestamp,
            "context": context,
        },
    }


def test_surface_and_action_record_survive_a_real_compaction_pass(
    tmp_path: Path, monkeypatch: Any
) -> None:
    agent = _CapturingAgent(["the summary"])
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as client:
        session = app.state.sessions.create(workspace_id="ws_default", title="durability")
        sid = session.id
        _stub_spawn(app, monkeypatch)

        created = client.post(
            f"/v1/sessions/{sid}/a2ui/messages",
            headers=HEADERS,
            json={"messages": [_create_message(), _components()]},
        )
        assert created.status_code == 200, created.text
        action_response = client.post(
            f"/v1/sessions/{sid}/a2ui/actions",
            headers=HEADERS,
            json={"message": _action("form.submit", "surface_1", "2026-09-17T00:00:00Z", {"x": 1})},
        )
        assert action_response.status_code == 200, action_response.text
        action_id = action_response.json()["action_id"]

        before_count = len(app.state.messages.get(sid, []))

        compact_response = client.post(f"/v1/sessions/{sid}/compact", json={})
        assert compact_response.status_code == 200, compact_response.text
        assert compact_response.json()["compacted"] is True

        after_count = len(app.state.messages.get(sid, []))
        assert after_count == before_count + 1, "compaction appends a checkpoint, drops nothing"

        surface = app.state.a2ui_store.get(sid, "surface_1")
        assert surface is not None
        assert surface.state == "ready"
        [record] = surface.actions
        assert record["id"] == action_id
        assert record["state"] == "delivered"


def test_capabilities_action_and_surface_survive_a_full_server_restart(
    tmp_path: Path, monkeypatch: Any
) -> None:
    sessions_path = tmp_path / "sessions.json"

    app1 = build_app(sessions_path=sessions_path)
    with TestClient(app1) as client1:
        session = app1.state.sessions.create(workspace_id="ws_default", title="restart durability")
        sid = session.id

        from clio_schemas.a2ui.v0_9_1.capabilities import A2UIClientCapabilities

        caps_before = A2UIClientCapabilities.model_validate(
            {"v0.9": {"supportedCatalogIds": [WORKSPACE_CATALOG_ID]}}
        )
        remember_client_capabilities(app1, sid, caps_before)

        created = client1.post(
            f"/v1/sessions/{sid}/a2ui/messages",
            headers=HEADERS,
            json={"messages": [_create_message(), _components()]},
        )
        assert created.status_code == 200, created.text

        _stub_spawn(app1, monkeypatch)
        timestamp = "2026-09-17T01:00:00Z"
        context = {"selection": "SGPS"}
        first_action = client1.post(
            f"/v1/sessions/{sid}/a2ui/actions",
            headers=HEADERS,
            json={"message": _action("form.submit", "surface_1", timestamp, context)},
        )
        assert first_action.status_code == 200, first_action.text
        action_id_before = first_action.json()["action_id"]

    # --- restart: a genuinely SEPARATE app process on the same on-disk store ---
    app2 = build_app(sessions_path=sessions_path)
    with TestClient(app2) as client2:
        assert app2.state.sessions.get(sid) is not None, "session metadata did not survive restart"

        caps_after = client_capabilities(app2, sid)
        assert caps_after is not None
        assert list(caps_after.v0_9.supportedCatalogIds) == [WORKSPACE_CATALOG_ID]

        surface_after = app2.state.a2ui_store.get(sid, "surface_1")
        assert surface_after is not None
        assert surface_after.state == "ready"
        [record_after] = surface_after.actions
        assert record_after["id"] == action_id_before
        assert record_after["state"] == "delivered"

        spawned_after = _stub_spawn(app2, monkeypatch)
        duplicate_action = client2.post(
            f"/v1/sessions/{sid}/a2ui/actions",
            headers=HEADERS,
            json={"message": _action("form.submit", "surface_1", timestamp, context)},
        )

        assert duplicate_action.status_code == 200, duplicate_action.text
        assert duplicate_action.json()["action_id"] == action_id_before, (
            "the SAME action re-submitted after a restart must be deduped by its "
            "durable idempotency key, not delivered a second time"
        )
        assert spawned_after == [], "a duplicate submission must not spawn a second turn"

        surface_final = app2.state.a2ui_store.get(sid, "surface_1")
        assert surface_final is not None
        assert len(surface_final.actions) == 1, "no second action record was minted"
