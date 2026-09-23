"""S8 deliverable 1 (docs/design/a2ui-compat-campaign-2026-09.md, issue #1374):
replay of a pre-campaign (v0.9.2-era) transcript.

No real owner session file existed to vendor+scrub (no
``~/.config/clio-agent/messages/sess_*.json`` from that era was available), so
``tests/fixtures/a2ui_transcripts/v0_9_2_workspace_surface.json`` is
constructed from the REAL pre-campaign wire shapes recorded in
``tests/test_gact/test_a2ui_v3.py``'s git history at commit ``1e5d0341``
(``git show 1e5d0341:tests/test_gact/test_a2ui_v3.py``): the old
``agent.submit``/``form.submit`` button action shape, the single hardcoded
``CLIO_A2UI_CATALOG_ID`` (``https://iowarp.ai/a2ui/catalogs/clio-workspace/v1``
-- unchanged, still today's builtin ``clio-workspace`` catalog id), and the
``/lastAction`` ``updateDataModel`` ack the pre-S5 server appended after an
action (deleted in S5, docs/design/a2ui-compat-campaign-2026-09.md deletion
inventory). The fixture's own ``metadata.fixture_provenance`` on its first
message states this too. The ``Message``/``Part`` transcript-part base schema
is unchanged since that commit (verified against
``git show 1e5d0341:src/clio_agent/gact/parts.py``), so the fixture round-
trips through today's ``MessageStore`` exactly like a genuine persisted
ledger would.

This proves two distinct things:

1. REPLAY: the stored transcript folds unchanged on a fresh process touch —
   same surface, same state, the builtin catalog id resolved (not a stale/
   unknown id) — and the interactions projection shows the surface.
2. LEGACY DISPATCH: the OLD action-envelope event names still dispatch
   through TODAY's destination-routed dispatcher — ``agent.submit``/
   ``form.submit`` (undeclared -> agent lane), ``approval.respond``
   (sidecar-declared -> permission lane), ``run.cancel``/``run.retry``
   (sidecar-declared -> run lane) — per the clio-workspace catalog's own
   ``catalog.clio.json`` events map.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from clio_agent.gact.a2ui_catalogs.builtin import workspace_catalog_id
from clio_agent.gact.app import build_app

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "a2ui_transcripts"
    / "v0_9_2_workspace_surface.json"
)
HEADERS = {"X-GACT-Version": "0.3", "X-A2UI-Version": "0.9.1"}
#: The pre-campaign hardcoded catalog id -- identical to today's builtin id.
OLD_CATALOG_ID = "https://iowarp.ai/a2ui/catalogs/clio-workspace/v1"


def _replayed_session(tmp_path: Path) -> tuple[TestClient, Any, str]:
    """Build an app, mint a session, and seed its ledger from the v0.9.2 fixture.

    The fixture's own ``session_id``/``turn_id`` fields are rewritten to the
    freshly-minted session id (the fixture is a realistic STAND-IN, not a
    literal recording of a live session, so it is bound to whatever session
    this test creates) before being written to the app's ``messages/``
    directory -- exactly where a real restart would find a prior session's
    ledger on disk, never touched via any producer tool or HTTP door.
    """

    sessions_path = tmp_path / "sessions.json"
    app = build_app(sessions_path=sessions_path)
    session = app.state.sessions.create(workspace_id="ws_default", title="v0.9.2 replay")
    sid = session.id

    rows = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert len(rows) == 4, "fixture must not have silently lost rows before this test even runs"
    old_sid = rows[0]["session_id"]
    for row in rows:
        assert row["session_id"] == old_sid
        row["session_id"] = sid
        if row.get("turn_id") == old_sid:
            row["turn_id"] = sid

    messages_dir = sessions_path.parent / "messages"
    messages_dir.mkdir(parents=True, exist_ok=True)
    (messages_dir / f"{sid}.json").write_text(json.dumps(rows), encoding="utf-8")

    return TestClient(app), app, sid


def test_fixture_file_is_untouched_in_the_repo() -> None:
    """Sanity: the vendored fixture exists and is exactly the shape the other
    tests in this module assume (4 rows, old catalog id, /lastAction ack)."""

    assert FIXTURE_PATH.exists(), FIXTURE_PATH
    rows = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert len(rows) == 4
    assert "fixture_provenance" in rows[0]["metadata"]
    a2ui_parts = [p for row in rows for p in row["parts"] if p.get("type") == "a2ui"]
    assert len(a2ui_parts) == 2
    create_batch, last_action_batch = (p["a2ui_messages"] for p in a2ui_parts)
    assert create_batch[0]["createSurface"]["catalogId"] == OLD_CATALOG_ID
    assert last_action_batch[0]["updateDataModel"]["path"] == "/lastAction"
    button_events = {
        c["action"]["event"]["name"]
        for c in create_batch[1]["updateComponents"]["components"]
        if "action" in c
    }
    assert button_events == {"agent.submit", "form.submit"}


def test_replay_folds_the_same_surface_with_the_builtin_catalog_resolved(
    tmp_path: Path,
) -> None:
    client, app, sid = _replayed_session(tmp_path)
    with client:
        response = client.get(f"/v1/sessions/{sid}/messages")
        assert response.status_code == 200, response.text
        loaded = response.json()["messages"]
        assert len(loaded) == 4, "replay must not silently drop a row from the old ledger"

        surface = app.state.a2ui_store.get(sid, "surface_1")
        assert surface is not None
        assert surface.state == "ready"
        # The builtin catalog id is resolved to a REAL, currently-installed
        # catalog -- not merely echoed back as an opaque string.
        assert surface.catalog_id == OLD_CATALOG_ID == workspace_catalog_id()
        registry = app.state.a2ui_catalogs
        assert registry.get(surface.catalog_id, surface.protocol_version) is not None


def test_replay_interactions_projection_shows_the_surface(tmp_path: Path) -> None:
    client, _app, sid = _replayed_session(tmp_path)
    with client:
        rows = client.get(f"/v1/sessions/{sid}/interactions").json()["interactions"]
        a2ui_rows = [row for row in rows if row["kind"] == "a2ui"]
        assert len(a2ui_rows) == 1
        assert a2ui_rows[0]["source"]["surface_id"] == "surface_1"


def _legacy_action(name: str, context: dict[str, Any]) -> dict[str, Any]:
    """The OLD (pre-S5) action-envelope wire shape -- unchanged today."""

    return {
        "version": "v0.9.1",
        "action": {
            "name": name,
            "surfaceId": "surface_1",
            "sourceComponentId": "submit_btn",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "context": context,
        },
    }


def test_legacy_form_submit_and_agent_submit_dispatch_to_the_agent_lane(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """``agent.submit``/``form.submit`` are undeclared in the clio-workspace
    catalog's sidecar events map, so they fall to the default agent lane
    (docs/gact/a2ui-binding.md rule 6). ``delivery="start"`` is the agent
    lane's idle-session sub-kind (a new turn starts) -- distinct from
    ``"steer"``/``"resolve_question"`` for a running/waiting_user session."""

    client, app, sid = _replayed_session(tmp_path)
    spawned: list[Any] = []

    def _spawn(coro: Any, **_kwargs: Any) -> None:
        spawned.append(coro)
        coro.close()

    with client:
        monkeypatch.setattr(app.state.turn_runner, "spawn", _spawn)

        for name in ("agent.submit", "form.submit"):
            response = client.post(
                f"/v1/sessions/{sid}/a2ui/actions",
                headers=HEADERS,
                json={"message": _legacy_action(name, {"selection": "SGPS"})},
            )
            assert response.status_code == 200, response.text
            assert response.json()["delivery"] == "start"

        assert len(spawned) == 2, "both legacy names must actually reach the agent lane"


def test_legacy_approval_respond_dispatches_to_the_permission_lane(tmp_path: Path) -> None:
    client, app, sid = _replayed_session(tmp_path)
    with client:
        now = datetime.now(timezone.utc).isoformat()
        app.state.permissions["perm_legacy"] = {
            "id": "perm_legacy",
            "session_id": sid,
            "status": "pending",
            "summary": "Allow write",
            "created_at": now,
            "tool_call": {"tool_name": "fs_apply_edit_write", "input": {"path": "x"}},
        }

        response = client.post(
            f"/v1/sessions/{sid}/a2ui/actions",
            headers=HEADERS,
            json={
                "message": _legacy_action(
                    "approval.respond", {"permission_id": "perm_legacy", "action": "allow"}
                )
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["delivery"] == "permission"
        assert app.state.permissions["perm_legacy"]["status"] == "resolved"


def test_legacy_run_cancel_dispatches_to_the_run_lane(tmp_path: Path) -> None:
    client, app, sid = _replayed_session(tmp_path)
    with client:
        response = client.post(
            f"/v1/sessions/{sid}/a2ui/actions",
            headers=HEADERS,
            json={"message": _legacy_action("run.cancel", {})},
        )

        assert response.status_code == 200, response.text
        assert response.json()["delivery"] == "run_cancel"
        assert app.state.sessions.get(sid).status == "cancelled"
