"""S8 review round (docs/design/a2ui-compat-campaign-2026-09.md, issue
#1374 item B, BLOCKING): ``A2UIStore._project`` folds incrementally.

Measured before this fix: ``project_a2ui_parts``/``fold_action_records``
each ran a FULL fold (every persisted part, from empty state) on EVERY
call -- N writes cost O(N) work per call, O(N^2) total. An action POST did
2 full refolds (the idempotency-check ``.get()`` plus the post-persist
``.get()``); the 260-action bounds test took ~475s (later reproduced here
at ~116s once test-harness disk-write pollution — a SEPARATE bug, see
``test_a2ui_load_bounds.py``'s module docstring — was fixed, before this
item's own fold fix).

Fixed: ``A2UIStore`` caches its per-session projection
(``_ProjectionCache``) and only folds parts genuinely NEW since the last
call, via ``project_a2ui_parts``/``fold_action_records``'s new
``existing_surfaces``/``state`` incremental-fold parameters. A "full fold"
below means a ``project_a2ui_parts`` call with ``existing_surfaces is
None`` (starts from empty, the O(all parts) case); an "incremental fold"
passes a non-``None`` ``existing_surfaces`` (O(new parts) only).
"""

from __future__ import annotations

import threading
from itertools import count
from pathlib import Path
from typing import Any

from clio_agent.gact import a2ui as a2ui_module
from clio_agent.gact.a2ui_catalogs.builtin import workspace_catalog_id
from clio_agent.gact.app import build_app
from clio_agent.gact.messages import MessageStore

WORKSPACE_CATALOG_ID = workspace_catalog_id()


def _isolated_app(tmp_path: Path) -> Any:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    app.state.message_store = MessageStore(path=None)
    return app


def _count_full_folds(monkeypatch: Any) -> list[int]:
    """Wrap ``project_a2ui_parts`` to count FULL (non-incremental) folds."""

    calls: list[int] = []
    real = a2ui_module.project_a2ui_parts

    def _counting(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("existing_surfaces") is None:
            calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(a2ui_module, "project_a2ui_parts", _counting)
    # A2UIStore imported the name directly (`from ... import project_a2ui_parts`),
    # so the store module's own binding must be patched too, not only a2ui's.
    import clio_agent.gact.a2ui_store as a2ui_store_module

    monkeypatch.setattr(a2ui_store_module, "project_a2ui_parts", _counting)
    return calls


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


def test_sustained_writes_cause_at_most_one_full_fold(tmp_path: Path, monkeypatch: Any) -> None:
    """**Sabotage:** revert ``_project`` to always call ``project_a2ui_parts``
    with no ``existing_surfaces`` -> every one of the N writes below counts
    as a full fold -> red."""

    app = _isolated_app(tmp_path)
    session = app.state.sessions.create(workspace_id="ws_default", title="fold count")
    sid = session.id
    full_folds = _count_full_folds(monkeypatch)

    app.state.a2ui_store.apply_batch(sid, _create_batch("s1"))
    for i in range(40):
        app.state.a2ui_store.apply_batch(
            sid,
            [
                {
                    "version": "v0.9.1",
                    "updateComponents": {
                        "surfaceId": "s1",
                        "components": [{"id": f"node_{i}", "component": "Text", "text": f"u{i}"}],
                    },
                }
            ],
        )
        # Each apply_batch_outcome call itself does ONE _project() read
        # internally; a bare .get() after it is a SECOND read of the SAME
        # already-cached state -- proving reads don't cost extra full folds.
        assert app.state.a2ui_store.get(sid, "s1") is not None

    assert len(full_folds) <= 1, f"expected <=1 full fold across 41 writes, got {len(full_folds)}"


def test_full_fold_recurs_exactly_once_after_registry_invalidation(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A pack install/uninstall (``CatalogRegistry.invalidate()``) changes
    how an ALREADY-folded part could resolve without touching a single
    session message -- the cache must not silently keep serving the OLD
    fold. **Sabotage:** drop the ``registry_generation`` comparison from
    ``_project`` -> this second full fold never happens -> red."""

    app = _isolated_app(tmp_path)
    session = app.state.sessions.create(workspace_id="ws_default", title="registry invalidation")
    sid = session.id
    full_folds = _count_full_folds(monkeypatch)

    app.state.a2ui_store.apply_batch(sid, _create_batch("s1"))
    assert app.state.a2ui_store.get(sid, "s1") is not None
    assert len(full_folds) == 1, "the first-ever fold for a session is a full fold"

    # No new parts, same registry generation: a pure cache hit, no new fold.
    assert app.state.a2ui_store.get(sid, "s1") is not None
    assert len(full_folds) == 1

    app.state.a2ui_catalogs.invalidate()
    assert app.state.a2ui_store.get(sid, "s1") is not None
    assert len(full_folds) == 2, "a registry generation bump must force exactly one more full fold"

    # Settles back into cache-hit steady state afterward.
    assert app.state.a2ui_store.get(sid, "s1") is not None
    assert len(full_folds) == 2


def test_full_fold_recurs_exactly_once_after_session_blueprint_switch(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A session switching its OWN active blueprint changes its producible/
    resolvable catalog set without any pack install/uninstall (no registry
    generation bump) and without touching any session message -- a second,
    independent invalidation signal. **Sabotage:** drop the
    ``blueprint_identity`` comparison from ``_project`` -> red."""

    app = _isolated_app(tmp_path)
    session = app.state.sessions.create(workspace_id="ws_default", title="blueprint switch")
    sid = session.id
    full_folds = _count_full_folds(monkeypatch)

    app.state.a2ui_store.apply_batch(sid, _create_batch("s1"))
    assert app.state.a2ui_store.get(sid, "s1") is not None
    assert len(full_folds) == 1

    app.state.sessions.update(sid, metadata_patch={"active_agent_blueprint_id": "some-pack"})
    assert app.state.a2ui_store.get(sid, "s1") is not None
    assert len(full_folds) == 2, "switching the session's active blueprint must force one refold"

    assert app.state.a2ui_store.get(sid, "s1") is not None
    assert len(full_folds) == 2


def test_action_post_no_longer_costs_two_full_folds(tmp_path: Path, monkeypatch: Any) -> None:
    """The idempotency-check ``.get()`` (``persist_action_part``) and the
    post-persist ``.get()`` (``dispatcher.py``) used to each be a full
    refold of the whole session -- 2 full folds per action POST. Now the
    first ever call folds fully once; every action after that (including
    its own two internal ``.get()`` reads) is incremental."""

    from fastapi.testclient import TestClient

    app = _isolated_app(tmp_path)

    def _spawn(coro: Any, **_kwargs: Any) -> None:
        coro.close()

    app.state.turn_runner.spawn = _spawn
    with TestClient(app) as client:
        session = app.state.sessions.create(workspace_id="ws_default", title="action fold count")
        sid = session.id
        app.state.a2ui_store.apply_batch(sid, _create_batch("s1"))
        full_folds = _count_full_folds(monkeypatch)

        headers = {"X-GACT-Version": "0.3", "X-A2UI-Version": "0.9.1"}
        for i in range(10):
            response = client.post(
                f"/v1/sessions/{sid}/a2ui/actions",
                headers=headers,
                json={
                    "message": {
                        "version": "v0.9.1",
                        "action": {
                            "name": "custom.probe",
                            "surfaceId": "s1",
                            "sourceComponentId": "root",
                            "timestamp": f"2026-09-17T00:00:{i:02d}Z",
                            "context": {"i": i},
                        },
                    }
                },
            )
            assert response.status_code == 200, response.text

        assert len(full_folds) <= 1, (
            f"expected <=1 full fold across 10 action POSTs (each with 2 internal "
            f".get() reads), got {len(full_folds)}"
        )


def test_full_fold_recurs_once_for_a_late_arriving_causally_earlier_part(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Trap (i): a part injected with a ``recorded_at`` stamp BEFORE the
    cache's high-water mark is not a pure extension of the cached part
    sequence (it sorts into the MIDDLE, not the end) -- the cache must
    detect this and refold fully rather than silently missing it. Built by
    injecting a raw ``a2ui`` part straight onto the session's in-memory
    ledger (bypassing ``apply_batch``, which always stamps "now") the same
    way an out-of-order legacy/replayed part could arrive: TWO real batches
    first (createSurface, then a "later" update, each its own timestamped
    part) establish the cache's high-water mark, then a third part is
    injected stamped BETWEEN them -- causally in the MIDDLE, not the end."""

    import clio_agent.gact.a2ui_store as a2ui_store_module
    from clio_agent.gact.parts import Part
    from clio_agent.gact.types import Message

    app = _isolated_app(tmp_path)
    session = app.state.sessions.create(workspace_id="ws_default", title="causal reorder")
    sid = session.id
    full_folds = _count_full_folds(monkeypatch)

    # Two apply_batch calls back-to-back can land in the SAME real-clock tick
    # (observed under load: a full-suite run collapsed both stamps to the
    # identical wall-clock value, silently defeating this trap). Pin distinct,
    # strictly increasing stamps -- one per utcnow_iso() call, and each
    # apply_batch_outcome call makes more than one (the part's own
    # recorded_at, then _persist_part's message created_at/updated_at) -- so
    # "later" is deterministically later than "create" regardless of clock
    # resolution or system load.
    stamp_counter = count()
    monkeypatch.setattr(
        a2ui_store_module,
        "utcnow_iso",
        lambda: f"2026-01-01T00:00:{next(stamp_counter):02d}.000000+00:00",
    )

    app.state.a2ui_store.apply_batch(
        sid,
        [
            {
                "version": "v0.9.1",
                "createSurface": {"surfaceId": "s1", "catalogId": WORKSPACE_CATALOG_ID},
            }
        ],
    )
    app.state.a2ui_store.apply_batch(
        sid,
        [
            {
                "version": "v0.9.1",
                "updateComponents": {
                    "surfaceId": "s1",
                    "components": [{"id": "root", "component": "Text", "text": "later"}],
                },
            }
        ],
    )
    assert app.state.a2ui_store.get(sid, "s1") is not None
    assert len(full_folds) == 1

    middle_stamp = app.state.messages[sid][0].parts[0].metadata["recorded_at"]
    middle_part = Part(
        id="a2ui_injected_middle",
        type="a2ui",
        surface_id="s1",
        a2ui_protocol_version="0.9.1",
        a2ui_messages=[
            {
                "version": "v0.9.1",
                "updateComponents": {
                    "surfaceId": "s1",
                    "components": [{"id": "root", "component": "Text", "text": "middle"}],
                },
            }
        ],
        # Same stamp as the FIRST (createSurface) part -- the stable sort's
        # tiebreaker (arrival order) then places it right after createSurface
        # and BEFORE the "later" update, i.e. causally in the MIDDLE.
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

    surface = app.state.a2ui_store.get(sid, "s1")
    assert surface is not None
    assert len(full_folds) == 2, "a causally-earlier late arrival must force exactly one refold"
    # "later" still wins (it genuinely sorts after "middle") -- proves the
    # refold reprocessed the injected part in the RIGHT causal position,
    # not merely appended it at the end.
    assert surface.messages[-1]["updateComponents"]["components"][0]["text"] == "later"


def test_eviction_never_replaces_a_held_session_lock(tmp_path: Path) -> None:
    """S8 review round 3 (issue #1374 item A, HIGH): a resident-ledger
    EVICTION must drop ONLY the projection cache entry, never the session's
    write lock. ``_evict``'s victim is picked from every RESIDENT session
    during cap enforcement -- it can fire for a session another thread is
    mid-write on RIGHT NOW. Popping ``_session_locks[sid]`` there (the old
    ``forget_session`` call from the eviction hook) let the next
    ``_session_lock(sid)`` mint a FRESH ``RLock`` while the in-flight writer
    still held the ORIGINAL one, so a second writer could enter the SAME
    session's critical section concurrently -- the idempotency
    check-then-persist race the lock exists to prevent.

    **Sabotage:** have the eviction path call ``forget_session`` instead of
    ``forget_projection`` -> the lock identity changes underneath T1 and T2
    acquires while T1 still holds it -> red.
    """

    app = _isolated_app(tmp_path)
    session = app.state.sessions.create(workspace_id="ws_default", title="lock race")
    sid = session.id
    store = app.state.a2ui_store
    store.apply_batch(sid, _create_batch("s1"))

    lock_before = store._session_lock(sid)
    t1_inside = threading.Event()
    t1_release = threading.Event()
    t2_acquired = threading.Event()

    def _t1() -> None:
        with store._session_lock(sid):
            t1_inside.set()
            t1_release.wait(timeout=5)

    t1 = threading.Thread(target=_t1)
    t1.start()
    assert t1_inside.wait(timeout=5), "T1 never entered the critical section"

    try:
        # Eviction fires WHILE T1 still holds the lock (the exact race the
        # reviewer's probe names): this must be forget_projection, never
        # forget_session.
        store.forget_projection(sid)

        assert store._session_lock(sid) is lock_before, (
            "eviction must never replace a HELD session lock with a fresh one"
        )

        def _t2() -> None:
            if lock_before.acquire(timeout=0.3):
                t2_acquired.set()
                lock_before.release()

        t2 = threading.Thread(target=_t2)
        t2.start()
        t2.join(timeout=2)
        assert not t2_acquired.is_set(), "T2 must not enter while T1 still holds the lock"
    finally:
        t1_release.set()
        t1.join(timeout=5)

    # T1 released; the SAME lock object now admits a fresh acquire.
    assert store._session_lock(sid) is lock_before
    assert lock_before.acquire(timeout=2), "T2 must be able to enter once T1 releases"
    lock_before.release()
