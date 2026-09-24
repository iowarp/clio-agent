"""S8 deliverable 3 (docs/design/a2ui-compat-campaign-2026-09.md, issue
#1374): two sessions, two Agent Blueprints, one workspace.

Proves three distinct isolation properties in one scenario:

1. **Producible sets differ per session** — session A activates
   ``a2ui-minimal-pack`` (catalog ``minimal``), session B activates
   ``a2ui-second-pack`` (catalog ``second``); each session's own producible
   set carries only ITS blueprint's pack catalog (plus the two builtins),
   never the other session's.
2. **The installed set (client-advertised) is identical** — a client's
   ``a2uiClientCapabilities`` advertisement is a transport/client property,
   not session-scoped; the SAME advertisement on both sessions reports the
   SAME ``client_supported_catalog_ids``.
3. **Cross-session surface access is refused, typed** — a surface created in
   session A cannot be acted on (producer tool OR the action HTTP door) from
   session B, even by exact surface id.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from clio_agent.gact import context as gact_context
from clio_agent.gact.a2ui_capability_selection import select_catalog
from clio_agent.gact.a2ui_catalogs.activation import session_producible_catalog_ids
from clio_agent.gact.a2ui_catalogs.builtin import basic_catalog_id, workspace_catalog_id
from clio_agent.gact.a2ui_producer import (
    build_create_a2ui_surface_tool,
    build_update_a2ui_components_tool,
)
from clio_agent.gact.app import build_app

MINIMAL_PACK_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "a2ui_packs" / "minimal"
SECOND_PACK_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "a2ui_packs" / "second"
MINIMAL_CATALOG_ID = "https://example.test/a2ui/catalogs/minimal"
SECOND_CATALOG_ID = "https://example.test/a2ui/catalogs/second"
BASIC_ID = basic_catalog_id()
WORKSPACE_ID_CATALOG = workspace_catalog_id()
HEADERS = {"X-GACT-Version": "0.3", "X-A2UI-Version": "0.9.1"}


def _advertise(app: Any, session_id: str, catalog_ids: list[str]) -> None:
    from clio_schemas.a2ui.v0_9_1.capabilities import A2UIClientCapabilities

    from clio_agent.gact.a2ui_capabilities import remember_client_capabilities

    caps = A2UIClientCapabilities.model_validate({"v0.9": {"supportedCatalogIds": catalog_ids}})
    remember_client_capabilities(app, session_id, caps)


def _two_sessions_one_workspace(tmp_path: Path) -> tuple[TestClient, str, str, str]:
    """Build one app, one workspace, two sessions, each on its own pack."""

    app = build_app(sessions_path=tmp_path / "sessions.json")
    client = TestClient(app)
    with client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "shared",
                "root_path": str(tmp_path / "workspace"),
                "storage_root": str(tmp_path / "workspace" / ".clio"),
            },
        ).json()["id"]

        for pack_root in (MINIMAL_PACK_ROOT, SECOND_PACK_ROOT):
            installed = client.post(
                "/v1/agent-blueprints/install",
                json={"source": str(pack_root), "scope": "global"},
            )
            assert installed.status_code == 201, installed.text

        sid_a = client.post(
            "/v1/sessions", json={"title": "session-a", "workspace_id": wid}
        ).json()["id"]
        sid_b = client.post(
            "/v1/sessions", json={"title": "session-b", "workspace_id": wid}
        ).json()["id"]

        activated_a = client.post(
            f"/v1/sessions/{sid_a}/agent-blueprint", json={"blueprint_id": "a2ui-minimal-pack"}
        )
        assert activated_a.status_code == 200, activated_a.text
        activated_b = client.post(
            f"/v1/sessions/{sid_b}/agent-blueprint", json={"blueprint_id": "a2ui-second-pack"}
        )
        assert activated_b.status_code == 200, activated_b.text

    return client, wid, sid_a, sid_b


def test_producible_sets_differ_per_session_same_workspace(tmp_path: Path) -> None:
    client, _wid, sid_a, sid_b = _two_sessions_one_workspace(tmp_path)
    app = client.app

    producible_a = set(session_producible_catalog_ids(app, sid_a))
    producible_b = set(session_producible_catalog_ids(app, sid_b))

    # v15 S8: each fixture pack declares only its own catalog, and nothing is
    # implicit -- the builtins are producible only for an agent that lists them.
    assert producible_a == {MINIMAL_CATALOG_ID}
    assert producible_b == {SECOND_CATALOG_ID}
    assert not {BASIC_ID, WORKSPACE_ID_CATALOG} & (producible_a | producible_b)
    assert MINIMAL_CATALOG_ID not in producible_b
    assert SECOND_CATALOG_ID not in producible_a


def test_installed_client_advertised_set_is_identical_across_sessions(tmp_path: Path) -> None:
    """The client's OWN advertisement is a transport property; the SAME
    advertisement reports the SAME supportedCatalogIds regardless of which
    session's blueprint is active -- only PRODUCIBLE differs."""

    client, _wid, sid_a, sid_b = _two_sessions_one_workspace(tmp_path)
    app = client.app
    # Pack catalogs first in the client's own preference order, so selection
    # picks each session's OWN pack catalog rather than falling through to a
    # shared builtin -- makes the "selection differs too" assertion below
    # deterministic.
    advertised = [MINIMAL_CATALOG_ID, SECOND_CATALOG_ID, BASIC_ID, WORKSPACE_ID_CATALOG]
    _advertise(app, sid_a, advertised)
    _advertise(app, sid_b, advertised)

    selection_a = select_catalog(app, sid_a, record=False)
    selection_b = select_catalog(app, sid_b, record=False)

    assert selection_a.client_supported_catalog_ids == selection_b.client_supported_catalog_ids
    assert set(selection_a.client_supported_catalog_ids) == set(advertised)
    # But the PRODUCIBLE side of the same selection still isolates by session.
    assert selection_a.producible_catalog_ids != selection_b.producible_catalog_ids
    assert selection_a.catalog_id == MINIMAL_CATALOG_ID
    assert selection_b.catalog_id == SECOND_CATALOG_ID


def test_surface_created_in_session_a_cannot_be_acted_on_from_session_b(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client, _wid, sid_a, sid_b = _two_sessions_one_workspace(tmp_path)
    app = client.app
    _advertise(app, sid_a, [MINIMAL_CATALOG_ID])
    _advertise(app, sid_b, [SECOND_CATALOG_ID])

    monkeypatch.setattr(gact_context, "active_app", lambda: app)
    monkeypatch.setattr(gact_context, "active_session_id", lambda: sid_a)
    created = build_create_a2ui_surface_tool()(
        surface_id="iso-surface",
        components=[{"id": "root", "component": "MinimalText", "text": "A's surface"}],
    )
    assert created.get("ok") is not False, created
    assert created["catalog_id"] == MINIMAL_CATALOG_ID

    # Producer tool door: session B addressing A's exact surface id.
    monkeypatch.setattr(gact_context, "active_session_id", lambda: sid_b)
    refused = build_update_a2ui_components_tool()(
        surface_id="iso-surface",
        components=[{"id": "root", "component": "MinimalText", "text": "hijack attempt"}],
    )
    assert refused == {
        "ok": False,
        "reason": "a2ui_surface_not_found",
        "detail": "A2UI surface not found: iso-surface",
        "hint": (
            "reuse a live id from a prior result's session_surface_ids, or "
            "call create_a2ui_surface to make a new surface"
        ),
    }
    # A's own store is untouched by the refused cross-session attempt.
    surface_a = app.state.a2ui_store.get(sid_a, "iso-surface")
    assert surface_a is not None
    assert surface_a.revision == created["revision"]

    # Action HTTP door: same cross-session addressing, same refusal shape.
    with client:
        action_response = client.post(
            f"/v1/sessions/{sid_b}/a2ui/actions",
            headers=HEADERS,
            json={
                "message": {
                    "version": "v0.9.1",
                    "action": {
                        "name": "form.submit",
                        "surfaceId": "iso-surface",
                        "sourceComponentId": "root",
                        "timestamp": "2026-09-17T00:00:00Z",
                        "context": {},
                    },
                }
            },
        )
    assert action_response.status_code == 404, action_response.text
    assert "A2UI surface not found" in action_response.json()["error"]["message"]
