"""S8 deliverable 5 (docs/design/a2ui-compat-campaign-2026-09.md, issue
#1374): reason-ledger and memory bounds hold under sustained load, on ONE
session.

Three distinct bounded-memory properties, each driven by real repeated
production traffic (never a synthetic pre-sized list):

1. A surface's own ordered message log never grows past
   ``max_a2ui_messages()`` (``gact/a2ui.py``) -- the oldest message that
   is not ``createSurface`` or the one merged ``updateComponents`` is
   evicted with the typed ``a2ui_message_limit`` reason. ``updateComponents``
   traffic (G2 merge-gate finding, gact-tui#513 comment 5937313752:
   materializes into ONE current-state message, `a2ui_component_fold.py`)
   never needs this eviction at all -- only sustained ``updateDataModel``
   traffic still grows the list and exercises it.
2. The per-session catalog-reason ring
   (``CatalogRegistry._session_reasons``) never grows past
   ``A2UI_CATALOG_REASON_RING_MAXLEN`` (256) -- driven by distinct
   undeclared-destination actions, each of which records
   ``a2ui_event_destination_undeclared`` (never deduped, unlike the S5b
   narration-undeclared-once reason).
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
actually about, from (2)'s separate, pre-existing cost. 1,000 calls in a tight loop through the FULL persisted-ledger path
would still run into (2) and take minutes; each test below uses the
smallest N that still crosses its own bound with a clear margin (60
updates against a lowered 8-message cap; 260 actions against the
256-entry ring), proving the SAME bound-holds-under-sustained-load property
the 1,000 figure was chosen to demonstrate. Wall time before/after item B's
fix is recorded in the S8 report.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import a2ui as a2ui_module
from clio_agent.gact.a2ui import A2UIComponentLimitExceededError
from clio_agent.gact.a2ui_catalogs.builtin import workspace_catalog_id
from clio_agent.gact.a2ui_catalogs.reasons import A2UI_CATALOG_REASON_RING_MAXLEN
from clio_agent.gact.app import build_app
from clio_agent.gact.messages import MessageStore
from tests import _hang_guard
from tests.test_gact.input_capture import capture_inputs

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


def _capture_inputs(app: Any, monkeypatch: Any = None) -> list[Any]:
    """Hold model admission and observe actual confirmed Core inputs."""
    return capture_inputs(app, monkeypatch)


def test_sustained_data_model_updates_keep_message_retention_at_its_configured_bound(
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

    # `updateDataModel` (never `updateComponents`, G2 merge-gate finding,
    # gact-tui#513 comment 5937313752): `_apply_staged_message` now
    # MATERIALIZES every `updateComponents` into the ONE current component
    # state (`a2ui_component_fold.py`), so a sustained run of those no
    # longer grows the message list at all -- see
    # ``test_sustained_component_updates_never_grow_past_one_merged_message``
    # below for that bound. `updateDataModel` is the one message kind that
    # still accumulates one entry per write, so it is what still exercises
    # ``a2ui_message_limit`` eviction here.
    updates = 60
    for i in range(updates):
        app.state.a2ui_store.apply_batch(
            sid,
            [
                {
                    "version": "v0.9.1",
                    "updateDataModel": {
                        "surfaceId": "load_surface",
                        "path": "/counter",
                        "value": i,
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
    # Nor is the one merged updateComponents message -- evicting it would
    # blank the surface's entire rendered component tree, not just history.
    assert any("updateComponents" in m for m in surface.messages)
    assert surface.state == "ready"


def test_sustained_component_updates_never_grow_past_one_merged_message(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Materialization (G2 merge-gate finding, gact-tui#513 comment
    5937313752) means sustained `updateComponents` traffic never needs
    message-limit eviction to stay bounded in the first place: every update,
    however many DISTINCT component ids it introduces, folds into the SAME
    one current-state message. A bound even lower than the default (and
    lower than the previous version of this test exercised) still never
    evicts.
    """

    monkeypatch.setattr(a2ui_module, "max_a2ui_messages", lambda: 8)

    app = _isolated_app(tmp_path)
    session = app.state.sessions.create(workspace_id="ws_default", title="load bounds")
    sid = session.id
    app.state.a2ui_store.apply_batch(sid, _create_batch("load_surface"))

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
    assert len(surface.messages) == 2  # createSurface + the one merged updateComponents
    assert "createSurface" in surface.messages[0]
    assert "updateComponents" in surface.messages[1]
    assert surface.eviction_reason == ""
    assert surface.evicted_messages == 0
    merged_components = surface.messages[1]["updateComponents"]["components"]
    assert len(merged_components) == 1 + updates  # the seeded "root" plus every "node_i"
    assert surface.state == "ready"


def test_sustained_distinct_component_ids_hit_the_merged_cap_and_refuse(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """F5 (coordinator design, 2026-10-01): the merged state's component
    COUNT is bounded too, not just the message list's LENGTH -- a surface
    that accumulates distinct ids across many small updates, never any
    single oversized one, was otherwise unbounded once materialization
    (G2) made the stored list itself stay at one entry forever. A lowered
    cap (not the 256 default) makes this a fast, deterministic test.
    """

    monkeypatch.setattr(a2ui_module, "MAX_A2UI_COMPONENTS", 5)

    app = _isolated_app(tmp_path)
    session = app.state.sessions.create(workspace_id="ws_default", title="load bounds")
    sid = session.id
    app.state.a2ui_store.apply_batch(sid, _create_batch("load_surface"))  # seeds "root" (1/5)

    for i in range(4):  # "node_0".."node_3" -> exactly at the cap (5/5)
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

    surface_at_cap = app.state.a2ui_store.get(sid, "load_surface")
    assert surface_at_cap is not None
    at_cap_components = surface_at_cap.messages[1]["updateComponents"]["components"]
    assert len(at_cap_components) == 5

    with pytest.raises(A2UIComponentLimitExceededError) as excinfo:
        app.state.a2ui_store.apply_batch(
            sid,
            [
                {
                    "version": "v0.9.1",
                    "updateComponents": {
                        "surfaceId": "load_surface",
                        "components": [
                            {"id": "node_overflow", "component": "Text", "text": "one too many"}
                        ],
                    },
                }
            ],
        )
    assert excinfo.value.component_count == 6
    assert excinfo.value.limit == 5

    # The refusal rolls back: the surface still has exactly the 5 components
    # it had before the rejected call, never a partially-applied 6th.
    surface_after_refusal = app.state.a2ui_store.get(sid, "load_surface")
    assert surface_after_refusal is not None
    assert surface_after_refusal.revision == surface_at_cap.revision
    after_refusal_components = surface_after_refusal.messages[1]["updateComponents"]["components"]
    assert len(after_refusal_components) == 5
    assert all(c["id"] != "node_overflow" for c in after_refusal_components)


def test_sustained_actions_keep_the_per_session_reason_ring_at_256(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _isolated_app(tmp_path)
    with TestClient(app) as client:
        _capture_inputs(app)
        session = app.state.sessions.create(workspace_id="ws_default", title="reason ring")
        sid = session.id
        app.state.a2ui_store.apply_batch(sid, _create_batch("ring_surface"))

        # Observe the real owning I/O; neither the load nor its deadline changes.
        # Uncaptured bounded samples survive a watchdog exit on a CI worker.
        store = app.state.task_history.arc._store
        io: dict[str, dict[str, float]] = {}
        lock = threading.Lock()

        def measure(name: str, operation: Callable[..., Any]) -> Callable[..., Any]:
            def observed(*args: Any, **kwargs: Any) -> Any:
                started = time.monotonic()
                with lock:
                    io.setdefault(name, {"calls": 0, "seconds": 0, "max_seconds": 0})["calls"] += 1
                try:
                    return operation(*args, **kwargs)
                finally:
                    elapsed = time.monotonic() - started
                    with lock:
                        io[name]["seconds"] += elapsed
                        io[name]["max_seconds"] = max(io[name]["max_seconds"], elapsed)

            return observed

        for name in ("get", "put"):
            monkeypatch.setattr(store, name, measure(name, getattr(store, name)))
        from clio_agent.gact import part_atoms

        monkeypatch.setattr(
            part_atoms,
            "_append_segment_raw",
            measure("atom_append", part_atoms._append_segment_raw),
        )
        segments = app.state.task_history.arc._segments
        monkeypatch.setattr(segments, "_persist", measure("persist", segments._persist))
        started = time.monotonic()
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
            if i == 0 or (i + 1) % 32 == 0 or i + 1 == attempts:
                with lock:
                    print(
                        "A2UI_LOAD_IO "
                        + json.dumps(
                            {"actions": i + 1, "seconds": time.monotonic() - started, "io": io}
                        ),
                        file=_hang_guard._dump_stream or sys.__stderr__,
                        flush=True,
                    )

        reasons = app.state.a2ui_catalogs.session_reasons(sid)
        undeclared = [r for r in reasons if r["reason"] == "a2ui_event_destination_undeclared"]
        assert attempts > A2UI_CATALOG_REASON_RING_MAXLEN
        assert len(undeclared) == A2UI_CATALOG_REASON_RING_MAXLEN
        assert len(reasons) <= A2UI_CATALOG_REASON_RING_MAXLEN
