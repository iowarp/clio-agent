"""S8 deliverable 2 (docs/design/a2ui-compat-campaign-2026-09.md, issue #1374):
catalog-uninstalled-at-replay, then reinstalled without a restart.

``tests/test_gact/test_a2ui_catalog_registry.py`` already proves the "folds
as ``state=unknown`` with ``a2ui_catalog_unavailable`` recorded and
retrievable" half (S2's
``test_replay_catalog_unavailable_is_recorded_in_the_retrievable_session_ledger``).
This module covers the other half the S8 deliverable adds: reinstalling the
pack that declares the missing catalog (the real ``POST /v1/agent-blueprints/
install`` route, not a direct function call) makes the SAME already-folded
transcript resolve normally on the NEXT read, in the SAME process — no
restart, no new ``CatalogRegistry`` instance — proving the install route's
``registry.invalidate()`` call actually reaches the live fold path.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.parts import Part
from clio_agent.gact.types import Message

PACK_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "a2ui_packs" / "minimal"
MINIMAL_CATALOG_ID = "https://example.test/a2ui/catalogs/minimal"


def _seed_surface_on_minimal_catalog(app, session_id: str) -> None:
    """Directly seed a persisted ``a2ui`` part naming the not-yet-installed
    ``minimal`` pack catalog -- a replay scenario, not a live production
    (the minimal pack is intentionally NOT installed when this runs)."""

    part = Part(
        id="part_minimal",
        type="a2ui",
        surface_id="surface_minimal",
        a2ui_protocol_version="0.9.1",
        a2ui_messages=[
            {
                "version": "v0.9.1",
                "createSurface": {"surfaceId": "surface_minimal", "catalogId": MINIMAL_CATALOG_ID},
            },
            {
                "version": "v0.9.1",
                "updateComponents": {
                    "surfaceId": "surface_minimal",
                    "components": [{"id": "root", "component": "MinimalText", "text": "hi"}],
                },
            },
        ],
    )
    app.state.messages[session_id] = [
        Message(
            id="msg_minimal",
            session_id=session_id,
            role="assistant",
            created_at="2026-09-17T00:00:00Z",
            updated_at="2026-09-17T00:00:00Z",
            parts=[part],
        )
    ]


def test_reinstalling_the_pack_resolves_the_replay_without_a_restart(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="reinstall replay")
    sid = session.id
    _seed_surface_on_minimal_catalog(app, sid)

    # BEFORE install: the persisted surface folds as unknown, degradation
    # recorded and retrievable (S2 baseline, re-asserted here as the "before"
    # half of this test's before/after).
    before = app.state.a2ui_store.get(sid, "surface_minimal")
    assert before is not None
    assert before.state == "unknown"
    before_reasons = app.state.a2ui_catalogs.session_reasons(sid)
    assert any(row["reason"] == "a2ui_catalog_unavailable" for row in before_reasons)

    # Install the pack declaring MINIMAL_CATALOG_ID through the REAL HTTP
    # install route -- same running app, same CatalogRegistry instance, no
    # restart -- proving the route's registry.invalidate() call is what
    # makes this work, not a fresh process picking up the pack on next boot.
    with TestClient(app) as client:
        installed = client.post(
            "/v1/agent-blueprints/install",
            json={"source": str(PACK_ROOT), "scope": "global"},
        )
        assert installed.status_code == 201, installed.text
        assert any(
            row["id"] == "a2ui-minimal-pack" for row in installed.json().get("installed", [])
        )

    # AFTER install, in the SAME process: the SAME transcript now folds
    # normally -- no restart, no rebuild of the app.
    after = app.state.a2ui_store.get(sid, "surface_minimal")
    assert after is not None
    assert after.state == "ready"
    assert after.catalog_id == MINIMAL_CATALOG_ID
    registry_entry = app.state.a2ui_catalogs.get(MINIMAL_CATALOG_ID)
    assert registry_entry is not None
    assert registry_entry.source == "blueprint"


def test_uninstalling_again_makes_the_same_surface_unknown_again(tmp_path: Path) -> None:
    """The registry invalidation cuts both ways: uninstalling the pack after
    a successful replay makes the SAME transcript degrade again, never
    keeping a stale "it used to resolve" cache."""

    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="uninstall replay")
    sid = session.id
    _seed_surface_on_minimal_catalog(app, sid)

    with TestClient(app) as client:
        installed = client.post(
            "/v1/agent-blueprints/install",
            json={"source": str(PACK_ROOT), "scope": "global"},
        )
        assert installed.status_code == 201, installed.text
        assert app.state.a2ui_store.get(sid, "surface_minimal").state == "ready"

        deleted = client.delete(
            "/v1/agent-blueprints/a2ui-minimal-pack", params={"scope": "global"}
        )
        assert deleted.status_code == 200, deleted.text

    after_uninstall = app.state.a2ui_store.get(sid, "surface_minimal")
    assert after_uninstall is not None
    assert after_uninstall.state == "unknown"
