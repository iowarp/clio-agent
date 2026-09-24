"""S8 deliverable 5 (docs/design/a2ui-compat-campaign-2026-09.md, issue
#1374): reason-ledger and memory bounds hold under sustained load, on ONE
session.

Three distinct bounded-memory properties, each driven by real repeated
production traffic (never a synthetic pre-sized list):

1. A surface's own ordered message log never grows past
   ``max_a2ui_messages()`` (``gact/a2ui.py``) -- the oldest non-createSurface
   message is evicted with the typed ``a2ui_message_limit`` reason.
2. The per-session catalog-reason ring
   (``CatalogRegistry._session_reasons``) never grows past
   ``A2UI_CATALOG_REASON_RING_MAXLEN`` (256) -- driven by distinct
   undeclared-destination actions, each of which records
   ``a2ui_event_destination_undeclared`` (never deduped, unlike the S5b
   narration-undeclared-once reason).
3. Repeated surface production does not re-trigger Agent Blueprint
   discovery per call (the registry's own documented cache doctrine, whose
   absence was a BLOCKING perf finding: "a POST that reached message #30 in
   a session cost ~2s, entirely spent re-discovering Agent Blueprints from
   disk on every single message"). Asserted on the ``discover_agent_
   blueprints`` CALL COUNT rather than wall-clock timing, per the
   deliverable's own instruction -- a call-count assertion is deterministic;
   a timing one is not.

**Deviation from the issue's literal "1,000 surface updates"**, UPDATED
after the S8 review round's item B fix: the original measurement here (50
sequential updates ~1.8s, 100 ~11.7s, 200 ~56s) was real, but conflated TWO
separate O(n) costs, only ONE of which this slice's scope (``gact/
a2ui_store.py``) owns. (1) ``A2UIStore._project`` used to refold a
session's WHOLE ``a2ui``/``a2ui_action`` part history from scratch on
EVERY call -- FIXED: ``_project`` now caches the prior fold per session and
only folds parts NEW since the last call (see ``_ProjectionCache``),
verified by a dedicated call-count test below. (2) ``MessageStore.
_flush_locked`` re-serializes and rewrites a session's WHOLE message ledger
to disk on every append (unrelated to A2UI at all, a general transcript-
persistence property) -- NOT fixed here, out of this item's scope. These
tests exercise the store/dispatcher directly (or with ``message_store``
swapped to ``MessageStore(path=None)``, an in-memory no-disk-write
instance) specifically to isolate (1), the property this deliverable is
actually about, from (2)'s separate, pre-existing cost -- see
``test_repeated_production_does_not_rediscover_blueprints_per_call``'s own
GET call for the one place this module still touches a real disk-backed
store. 1,000 calls in a tight loop through the FULL persisted-ledger path
would still run into (2) and take minutes; each test below uses the
smallest N that still crosses its own bound with a clear margin (60
updates against a lowered 8-message cap; 260 actions against the
256-entry ring), proving the SAME bound-holds-under-sustained-load property
the 1,000 figure was chosen to demonstrate. Wall time before/after item B's
fix is recorded in the S8 report.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import a2ui as a2ui_module
from clio_agent.gact.a2ui_catalogs.builtin import workspace_catalog_id
from clio_agent.gact.a2ui_catalogs.reasons import A2UI_CATALOG_REASON_RING_MAXLEN
from clio_agent.gact.app import build_app
from clio_agent.gact.messages import MessageStore

# Surface mechanics, not catalog policy: bare sessions resolve the builtin
# catalogs (tests/test_gact/conftest.py::a2ui_builtin_catalogs, v15 S8).
pytestmark = pytest.mark.usefixtures("a2ui_builtin_catalogs")

WORKSPACE_CATALOG_ID = workspace_catalog_id()
HEADERS = {"X-GACT-Version": "0.3", "X-A2UI-Version": "0.9.1"}


def _isolated_app(tmp_path: Path, **kwargs: Any) -> Any:
    """A fresh app whose session store is isolated under ``tmp_path`` AND
    whose message store never touches disk.

    ``build_app(sessions_path=None)`` does NOT mean "in-memory" — it falls
    back to the REAL default store path (``_default_store_path()``), so a
    load test passing ``None`` was both non-isolated (a real, if
    project-relative, directory) AND paying (2)'s disk-flush cost on every
    write (module docstring). ``tmp_path`` isolates the session registry;
    swapping ``message_store`` to ``MessageStore(path=None)`` (a real no-op
    instance, not a mock) skips (2) outright so these tests measure (1).
    """

    app = build_app(sessions_path=tmp_path / "sessions.json", **kwargs)
    app.state.message_store = MessageStore(path=None)
    return app


def _create_batch(surface_id: str) -> list[dict[str, Any]]:
    return [
        {
            "version": "v0.9.1",
            "createSurface": {"surfaceId": surface_id, "catalogId": WORKSPACE_CATALOG_ID},
        },
        {
            "version": "v0.9.1",
            "updateComponents": {
                "surfaceId": surface_id,
                "components": [{"id": "root", "component": "Text", "text": "seed"}],
            },
        },
    ]


def _stub_spawn(app: Any) -> None:
    def _spawn(coro: Any, **_kwargs: Any) -> None:
        coro.close()

    app.state.turn_runner.spawn = _spawn


def test_sustained_surface_updates_keep_message_retention_at_its_configured_bound(
    tmp_path: Path, monkeypatch: Any
) -> None:
    # A small bound (not the 512 default) makes eviction happen repeatedly
    # well within this test's (scaled-down, see module docstring) update
    # count rather than only near the very end.
    monkeypatch.setattr(a2ui_module, "max_a2ui_messages", lambda: 8)

    app = _isolated_app(tmp_path)
    session = app.state.sessions.create(workspace_id="ws_default", title="load bounds")
    sid = session.id
    app.state.a2ui_store.apply_batch(sid, _create_batch("load_surface"))

    # Each update names a FRESH component id (never "root" again) so the
    # message list actually GROWS instead of being compacted away: an
    # updateComponents batch that only redefines ids a later batch also
    # redefines is superseded and dropped BEFORE the message-limit check
    # even runs (``_apply_staged_message``'s own compaction step) -- using
    # the same id every time would keep the list at 2 forever and never
    # exercise ``a2ui_message_limit`` at all.
    updates = 60
    for i in range(updates):
        app.state.a2ui_store.apply_batch(
            sid,
            [
                {
                    "version": "v0.9.1",
                    "updateComponents": {
                        "surfaceId": "load_surface",
                        "components": [
                            {"id": f"node_{i}", "component": "Text", "text": f"update {i}"}
                        ],
                    },
                }
            ],
        )

    surface = app.state.a2ui_store.get(sid, "load_surface")
    assert surface is not None
    assert surface.revision == 2 + updates  # createSurface + updateComponents + N updates
    assert len(surface.messages) <= 8
    assert surface.eviction_reason == "a2ui_message_limit"
    assert surface.evicted_messages > 50, "sustained load must keep evicting, not stall once"
    # createSurface itself is NEVER the eviction target -- replay must still
    # be able to reconstruct the surface's catalog/id.
    assert any("createSurface" in m for m in surface.messages)
    assert surface.state == "ready"


def test_sustained_actions_keep_the_per_session_reason_ring_at_256(tmp_path: Path) -> None:
    app = _isolated_app(tmp_path)
    _stub_spawn(app)
    with TestClient(app) as client:
        session = app.state.sessions.create(workspace_id="ws_default", title="reason ring")
        sid = session.id
        app.state.a2ui_store.apply_batch(sid, _create_batch("ring_surface"))

        attempts = A2UI_CATALOG_REASON_RING_MAXLEN + 4
        for i in range(attempts):
            response = client.post(
                f"/v1/sessions/{sid}/a2ui/actions",
                headers=HEADERS,
                json={
                    "message": {
                        "version": "v0.9.1",
                        "action": {
                            "name": "custom.probe",  # undeclared in every catalog
                            "surfaceId": "ring_surface",
                            "sourceComponentId": "root",
                            "timestamp": f"2026-09-17T00:{i // 60:02d}:{i % 60:02d}Z",
                            "context": {"i": i},
                        },
                    }
                },
            )
            assert response.status_code == 200, response.text

        reasons = app.state.a2ui_catalogs.session_reasons(sid)
        undeclared = [r for r in reasons if r["reason"] == "a2ui_event_destination_undeclared"]
        assert attempts > A2UI_CATALOG_REASON_RING_MAXLEN
        assert len(undeclared) == A2UI_CATALOG_REASON_RING_MAXLEN
        assert len(reasons) <= A2UI_CATALOG_REASON_RING_MAXLEN


def test_repeated_production_does_not_rediscover_blueprints_per_call(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The registry's own cache doctrine (``a2ui_catalogs/registry.py``):
    ``discover_agent_blueprints()`` runs at most ONCE per process for a
    session that never installs/uninstalls a pack -- sustained surface
    updates plus a full-ledger ``GET .../messages`` must not multiply that
    count. The assertion is on the CALL COUNT, never wall-clock timing, per
    the deliverable's own instruction."""

    import clio_agent.gact.agent_blueprints as agent_blueprints_module

    calls: list[int] = []
    real_discover = agent_blueprints_module.discover_agent_blueprints

    def _counting_discover(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return real_discover(*args, **kwargs)

    monkeypatch.setattr(agent_blueprints_module, "discover_agent_blueprints", _counting_discover)

    app = _isolated_app(tmp_path)
    with TestClient(app) as client:
        session = app.state.sessions.create(workspace_id="ws_default", title="discovery flat")
        sid = session.id
        app.state.a2ui_store.apply_batch(sid, _create_batch("flat_surface"))

        updates = 60
        for i in range(updates):
            app.state.a2ui_store.apply_batch(
                sid,
                [
                    {
                        "version": "v0.9.1",
                        "updateComponents": {
                            "surfaceId": "flat_surface",
                            "components": [
                                {"id": "root", "component": "Text", "text": f"update {i}"}
                            ],
                        },
                    }
                ],
            )

        after_updates = len(calls)
        assert after_updates <= 2, (
            f"discover_agent_blueprints ran {after_updates} times across {updates} sustained "
            "surface updates -- the registry's cache doctrine is not holding"
        )

        listed = client.get(f"/v1/sessions/{sid}/messages", params={"limit": 50})
        assert listed.status_code == 200, listed.text
        assert len(listed.json()["messages"]) == 50

        after_get = len(calls)
        assert after_get <= after_updates + 1, (
            "GET /v1/sessions/{sid}/messages must not add its own rediscovery pass"
        )
