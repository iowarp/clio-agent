"""Permanent differential test: ``A2UIStore``'s incremental projection cache
must ALWAYS equal a from-scratch fold of the same persisted parts.

Adversarial re-review of feat/a2ui-s8 (issue #1374, focused re-review) found
two BLOCKING/HIGH divergences the item-B fold-fix's own call-count tests
(``test_a2ui_projection_cache.py``) never would have caught, because those
only count HOW MANY TIMES a full fold ran -- never compare the cached
result's CONTENT against an independent from-scratch fold:

1. (BLOCKING) A repair-exhausted surface's ``error`` field survived a LATER
   ``updateComponents`` that should have cleared it (a from-scratch fold's
   action pass runs once, at the END, against the FINAL revision -- an
   incremental fold that runs the a2ui pass and the action pass in separate
   calls left a stale ``error`` from an EARLIER revision on the wire).
2. (HIGH) A createSurface recreating a previously-DELETED id built a BRAND
   NEW record with empty ``actions`` -- a from-scratch fold always
   re-attaches that id's whole action history afterward (session-scoped);
   the incremental fold only re-attached history for surfaces with a NEW
   action part THIS call, silently detaching history on recreate and making
   the idempotency check for a re-posted action cache-order-dependent.

Both are proven here via :func:`_assert_matches_fresh`, called after EVERY
scripted step: it independently re-folds the SAME ``A2UIStore._parts()``
output via ``project_a2ui_parts``/``fold_action_records`` with NO existing
state (the from-scratch, indisputably-correct reference) and asserts the
FULL wire dict (every field: ``error``, ``actions``, ``state``, ``revision``,
...) is byte-identical to what the cached ``A2UIStore.list_wire()`` returns.
This is the reviewer's manual probe, made permanent so future drift in the
incremental-fold seam (``a2ui.py::project_a2ui_parts``,
``a2ui_actions/record.py::fold_action_records``, ``a2ui_store.py::_project``)
fails a test instead of silently shipping a stale wire value.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from clio_agent.gact.a2ui import project_a2ui_parts
from clio_agent.gact.a2ui_actions.record import fold_action_records

from .test_a2ui_actions import _error_envelope, _stub_spawn
from .test_a2ui_v3 import HEADERS, _create_message, _session_client


def _update_message(surface_id: str, text: str) -> dict[str, Any]:
    return {
        "version": "v0.9.1",
        "updateComponents": {
            "surfaceId": surface_id,
            "components": [{"id": "root", "component": "Text", "text": text}],
        },
    }


def _delete_message(surface_id: str) -> dict[str, Any]:
    return {"version": "v0.9.1", "deleteSurface": {"surfaceId": surface_id}}


def _fresh_wire(app: Any, session_id: str) -> list[dict[str, Any]]:
    """Independently re-fold ``A2UIStore._parts()`` from scratch (the reference)."""

    from clio_agent.gact.a2ui_catalogs.activation import session_catalog_resolver  # noqa: PLC0415

    store = app.state.a2ui_store
    a2ui_parts = store._parts(session_id, part_type="a2ui")
    action_parts = store._parts(session_id, part_type="a2ui_action")
    surfaces, _ = project_a2ui_parts(
        a2ui_parts, session_id, catalogs=session_catalog_resolver(app, session_id)
    )
    fold_action_records(action_parts, session_id, surfaces)
    rows = sorted(surfaces.values(), key=lambda row: row.created_at)
    return [row.to_wire() for row in rows]


def _assert_matches_fresh(app: Any, session_id: str, *, step: str = "") -> None:
    """The reviewer's probe, made permanent: cached projection == from-scratch fold."""

    cached = app.state.a2ui_store.list_wire(session_id)
    fresh = _fresh_wire(app, session_id)
    assert cached == fresh, (
        f"cached A2UIStore projection diverged from a from-scratch fold"
        f"{f' after {step}' if step else ''}:\ncached={cached}\nfresh={fresh}"
    )


def test_plain_updates_match_a_fresh_fold(tmp_path: Path) -> None:
    """Scenario: plain updates."""

    client, sid, _ = _session_client(tmp_path)
    app = client.app

    client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_create_message("s1")]},
    )
    _assert_matches_fresh(app, sid, step="createSurface")
    for i in range(4):
        client.post(
            f"/v1/sessions/{sid}/a2ui/messages",
            headers=HEADERS,
            json={"messages": [_update_message("s1", f"u{i}")]},
        )
        _assert_matches_fresh(app, sid, step=f"update {i}")


def test_repair_exhaustion_then_update_clears_the_stale_error(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Scenario: repair-exhaustion then update (item 1, BLOCKING).

    **Sabotage:** revert ``_apply_staged_message``'s ``surface.error = ""``
    -> the post-exhaustion update no longer clears ``error`` -> the final
    ``_assert_matches_fresh`` call goes red (cached still carries
    ``a2ui_repair_exhausted``; a fresh fold, whose action pass runs against
    the NEW revision, never would have)."""

    client, sid, _ = _session_client(tmp_path)
    app = client.app
    spawned = _stub_spawn(app, monkeypatch)
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_create_message("surface_1")]},
    )
    _assert_matches_fresh(app, sid, step="createSurface")

    envelope = _error_envelope(
        "surface_1", "VALIDATION_FAILED", path="/root/0", message="unknown component"
    )
    first = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": envelope}
    )
    assert first.status_code == 200, first.text
    _assert_matches_fresh(app, sid, step="first VALIDATION_FAILED (repair)")

    second = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": envelope}
    )
    assert second.status_code == 200, second.text
    assert second.json()["state"] == "failed"
    assert second.json()["reason"] == "a2ui_repair_exhausted"
    _assert_matches_fresh(app, sid, step="second VALIDATION_FAILED (exhausted)")

    surface = app.state.a2ui_store.get(sid, "surface_1")
    assert surface is not None
    assert surface.state == "failed"
    assert surface.error == "a2ui_repair_exhausted"
    assert len(spawned) == 1  # no second repair turn

    # A genuine new revision: the stale repair-exhausted error must clear.
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_update_message("surface_1", "recovered")]},
    )
    _assert_matches_fresh(app, sid, step="post-exhaustion update")

    surface = app.state.a2ui_store.get(sid, "surface_1")
    assert surface is not None
    assert surface.state == "ready"
    assert surface.error == "", (
        "a repair-exhausted error scoped to an OLD revision must not survive a "
        "later successful update -- a from-scratch fold's action pass, run once "
        "at the end against the FINAL revision, never would have set it here"
    )


def test_delete_createsurface_action_repost_matches_a_fresh_fold(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Scenario: delete then createSurface then action re-post (item 2, HIGH).

    **Sabotage:** revert the ``created_surface_ids``/``reattach_surface_ids``
    threading -> the recreated surface's ``actions`` stays empty until a NEW
    action part arrives -> ``_assert_matches_fresh`` goes red right after
    recreate (fresh fold re-attaches the FULL session-scoped action history
    for surface_id "s1" immediately; the sabotaged cache does not) -- and,
    separately, the re-post would be accepted as a NEW record (idempotency
    lookup finds nothing in the empty ``actions``) instead of deduped."""

    client, sid, _ = _session_client(tmp_path)
    app = client.app
    spawned = _stub_spawn(app, monkeypatch)
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_create_message("s1")]},
    )
    _assert_matches_fresh(app, sid, step="createSurface s1")

    action = {
        "version": "v0.9.1",
        "action": {
            "name": "earthscope.stations.selected",
            "surfaceId": "s1",
            "sourceComponentId": "source",
            "timestamp": "2026-09-17T00:00:00Z",
            "context": {"stationIds": ["SGPS"]},
        },
    }
    first = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )
    assert first.status_code == 200, first.text
    original_action_id = first.json()["action_id"]
    _assert_matches_fresh(app, sid, step="first action post")

    client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_delete_message("s1")]},
    )
    _assert_matches_fresh(app, sid, step="deleteSurface s1")

    client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_create_message("s1")]},
    )
    _assert_matches_fresh(app, sid, step="recreate s1")

    recreated = app.state.a2ui_store.get(sid, "s1")
    assert recreated is not None
    assert len(recreated.actions) == 1, (
        "a recreated surface must re-attach its id's SESSION-scoped action "
        "history immediately, not only after a fresh action part arrives"
    )

    # The SAME action envelope again (identical idempotency key) -- the
    # from-scratch contract: a surface id's action history is session
    # history, so this is a DUPLICATE of the pre-delete record, not new.
    repost = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )
    assert repost.status_code == 200, repost.text
    assert repost.json()["action_id"] == original_action_id, (
        "a re-post after delete+recreate must dedupe against the ORIGINAL record "
        "(from-scratch semantics), not be accepted as a new click"
    )
    assert len(spawned) == 1, "the duplicate must not spawn a second turn"
    _assert_matches_fresh(app, sid, step="action re-post after recreate")

    surface = app.state.a2ui_store.get(sid, "s1")
    assert surface is not None
    assert len(surface.actions) == 1


def test_late_recorded_at_matches_a_fresh_fold(tmp_path: Path) -> None:
    """Scenario: a late-arriving part stamped before the cache's high-water mark."""

    from clio_agent.gact.parts import Part
    from clio_agent.gact.types import Message

    client, sid, _ = _session_client(tmp_path)
    app = client.app
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_create_message("s1")]},
    )
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_update_message("s1", "later")]},
    )
    _assert_matches_fresh(app, sid, step="two real batches")

    middle_stamp = app.state.messages[sid][0].parts[0].metadata["recorded_at"]
    middle_part = Part(
        id="a2ui_injected_middle",
        type="a2ui",
        surface_id="s1",
        a2ui_protocol_version="0.9.1",
        a2ui_messages=[_update_message("s1", "middle")],
        metadata={"recorded_at": middle_stamp},
    )
    app.state.messages[sid].append(
        Message(
            id="msg_injected_middle",
            session_id=sid,
            role="assistant",
            created_at=middle_stamp,
            updated_at=middle_stamp,
            parts=[middle_part],
        )
    )
    _assert_matches_fresh(app, sid, step="late-arriving causally-earlier part")


def test_catalog_registry_invalidate_matches_a_fresh_fold(tmp_path: Path) -> None:
    """Scenario: catalog uninstall/reinstall (``CatalogRegistry.invalidate()``,
    the exact signal an install/uninstall route emits)."""

    client, sid, _ = _session_client(tmp_path)
    app = client.app
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_create_message("s1")]},
    )
    _assert_matches_fresh(app, sid, step="createSurface")

    app.state.a2ui_catalogs.invalidate()
    _assert_matches_fresh(app, sid, step="registry invalidate (uninstall/reinstall)")

    client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_update_message("s1", "after-invalidate")]},
    )
    _assert_matches_fresh(app, sid, step="update after invalidate")


def test_ledger_replace_matches_a_fresh_fold(tmp_path: Path) -> None:
    """Scenario: ledger replace via ``_replace_session_messages`` (compaction/undo)."""

    from clio_agent.gact.session_store import _replace_session_messages

    client, sid, _ = _session_client(tmp_path)
    app = client.app
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_create_message("s1")]},
    )
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_update_message("s1", "before-replace")]},
    )
    _assert_matches_fresh(app, sid, step="before replace")

    # A no-semantic-change replace (the same messages, a new list object) --
    # exactly what compaction/undo do to the ledger even when the CONTENT
    # they keep is unchanged.
    _replace_session_messages(app, sid, list(app.state.messages[sid]))
    _assert_matches_fresh(app, sid, step="_replace_session_messages")

    client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_update_message("s1", "after-replace")]},
    )
    _assert_matches_fresh(app, sid, step="update after replace")


def test_resident_eviction_matches_a_fresh_fold(tmp_path: Path) -> None:
    """Scenario: resident eviction (focused re-review item 3).

    ``ResidentLedgerSet`` evicting a session's ledger for real capacity/
    idle-TTL pressure must drop ``A2UIStore``'s own projection cache entry
    too (the ``on_evict`` hook wired in ``resident_ledgers.py::
    build_resident_ledger_set``) -- otherwise the cache, which holds every
    surface's full message list, outlives the SAME bound #889 built the
    resident set to enforce.

    **Sabotage:** drop the ``on_evict`` wiring -> the cache entry survives
    eviction -> the post-eviction full-fold count assertion goes red (and,
    separately, the entry would still be present in ``_projection_cache``)."""

    client, sid, _ = _session_client(tmp_path)
    app = client.app
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_create_message("s1")]},
    )
    _assert_matches_fresh(app, sid, step="createSurface")

    store = app.state.a2ui_store
    assert sid in store._projection_cache, "a cache entry must exist before eviction"

    resident = app.state.messages
    assert sid in resident._resident, "the session's ledger must be resident before eviction"
    resident._evict(sid, "capacity_bytes")

    assert sid not in store._projection_cache, (
        "A2UIStore's projection cache must be dropped when the resident ledger set "
        "evicts the session -- otherwise it outlives the SAME #889 memory bound"
    )
    assert sid not in resident._resident

    full_folds: list[int] = []
    import clio_agent.gact.a2ui as a2ui_module
    import clio_agent.gact.a2ui_store as a2ui_store_module

    real = a2ui_module.project_a2ui_parts

    def _counting(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("existing_surfaces") is None:
            full_folds.append(1)
        return real(*args, **kwargs)

    for target in (a2ui_module, a2ui_store_module):
        target.project_a2ui_parts = _counting  # type: ignore[attr-defined]
    try:
        _assert_matches_fresh(app, sid, step="rehydrate after eviction")
        assert len(full_folds) == 1, (
            f"exactly one full refold expected right after eviction, got {len(full_folds)}"
        )
        assert app.state.a2ui_store.get(sid, "s1") is not None
        assert len(full_folds) == 1, "settles back to cache-hit steady state"
    finally:
        a2ui_module.project_a2ui_parts = real
        a2ui_store_module.project_a2ui_parts = real

    assert sid in resident._resident, "the read above must have rehydrated the ledger"
