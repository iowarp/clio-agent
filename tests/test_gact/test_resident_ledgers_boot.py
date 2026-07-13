"""Boot + route integration tests for the resident ledger set (#889).

Exercised against the real ``build_app`` / ``MessageStore`` / route handlers:

* boot on the index only — a restart does NOT make any transcript body resident;
* first ``GET /messages`` rehydrates byte-identical to the pre-restart response;
* metrics stay correct across the restart (seeded, not re-walked into residency);
* undo / delete / fork on an EVICTED session rehydrate transparently and emit the
  same wire events as an unevicted one;
* an active session (live SSE subscriber / in-flight turn) is never evicted.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from clio_agent.gact.app import (
    _replace_session_messages,
    build_app,
)
from clio_agent.gact.resident_ledgers import _session_is_active
from clio_agent.gact.types import Message, Part


def _message(mid: str, sid: str, *, role: str = "assistant", text: str = "body") -> Message:
    return Message(
        id=mid,
        session_id=sid,
        role=role,
        created_at="2026-05-20T00:00:00+00:00",
        updated_at="2026-05-20T00:00:00+00:00",
        parts=[Part(id=f"part_{mid}", type="text", text=text)],
    )


def _seed_persisted(app, sid: str, mids: list[str]) -> None:
    """Seed a session's ledger through the write-through seam (memory + DISK)."""

    msgs = [_message(m, sid, text=f"content for {m}") for m in mids]
    _replace_session_messages(app, sid, msgs)
    app.state.sessions.update(sid, message_count=len(msgs))


# --------------------------------------------------------------------------- #
# Boot residency
# --------------------------------------------------------------------------- #


def test_boot_does_not_make_bodies_resident(tmp_path: Path) -> None:
    sessions_path = tmp_path / "sessions.json"

    app1 = build_app(sessions_path=sessions_path)
    with TestClient(app1) as c1:
        sids = []
        for i in range(5):
            sid = c1.post("/v1/sessions", json={"title": f"s{i}"}).json()["id"]
            _seed_persisted(app1, sid, [f"m{i}_1", f"m{i}_2", f"m{i}_3"])
            sids.append(sid)

    # Restart: a brand-new app over the SAME on-disk stores.
    app2 = build_app(sessions_path=sessions_path)
    # The index is visible, but NO transcript body is resident after boot.
    assert app2.state.messages.resident_count == 0
    assert set(app2.state.messages) == set(sids)
    assert len(app2.state.messages) == 5

    # First access rehydrates exactly one session.
    _ = app2.state.messages.get(sids[0], [])
    assert app2.state.messages.resident_count == 1
    assert app2.state.messages.resident_session_ids == [sids[0]]


def test_first_get_messages_rehydrates_byte_identical(tmp_path: Path) -> None:
    sessions_path = tmp_path / "sessions.json"

    app1 = build_app(sessions_path=sessions_path)
    with TestClient(app1) as c1:
        sid = c1.post("/v1/sessions", json={"title": "rich"}).json()["id"]
        # A varied ledger: user + assistant, multiple parts, metadata.
        rich = [
            Message(
                id="u1",
                session_id=sid,
                role="user",
                created_at="2026-05-20T00:00:00+00:00",
                updated_at="2026-05-20T00:00:00+00:00",
                parts=[Part(id="u1p", type="text", text="analyze /tmp/f.hdf5")],
            ),
            Message(
                id="a1",
                session_id=sid,
                role="assistant",
                created_at="2026-05-20T00:00:01+00:00",
                updated_at="2026-05-20T00:00:01+00:00",
                parts=[
                    Part(id="a1p0", type="thinking", text="let me look"),
                    Part(id="a1p1", type="text", text="done — 42 rows"),
                ],
                metadata={"tools_called": [{"name": "hdf5.analyze", "ok": True}]},
            ),
        ]
        _replace_session_messages(app1, sid, rich)
        app1.state.sessions.update(sid, message_count=len(rich))
        before = c1.get(f"/v1/sessions/{sid}/messages").json()

    app2 = build_app(sessions_path=sessions_path)
    with TestClient(app2) as c2:
        assert app2.state.messages.resident_count == 0
        after = c2.get(f"/v1/sessions/{sid}/messages").json()

    assert after == before  # reload == live, across a restart, off the lazy path


def test_metrics_correct_across_restart(tmp_path: Path) -> None:
    sessions_path = tmp_path / "sessions.json"

    app1 = build_app(sessions_path=sessions_path)
    with TestClient(app1) as c1:
        sid = c1.post("/v1/sessions", json={"title": "m"}).json()["id"]
        _replace_session_messages(
            app1,
            sid,
            [
                _message("u", sid, role="user", text="q"),
                _message("a", sid, role="assistant", text="a"),
            ],
        )
        app1.state.sessions.update(sid, message_count=2)
        before = c1.get("/v1/metrics").json()["messages"]

    app2 = build_app(sessions_path=sessions_path)
    with TestClient(app2) as c2:
        after = c2.get("/v1/metrics").json()["messages"]
        # Seeded from disk at boot, without pinning bodies resident.
        assert app2.state.messages.resident_count == 0

    assert after["total"] == before["total"] == 2
    assert after["by_role"] == before["by_role"] == {"user": 1, "assistant": 1}


# --------------------------------------------------------------------------- #
# Mutations on an EVICTED session rehydrate transparently
# --------------------------------------------------------------------------- #


def test_undo_on_evicted_session_matches_unevicted(tmp_path: Path) -> None:
    def run(evict: bool) -> tuple[dict, list[str]]:
        app = build_app(sessions_path=tmp_path / f"s_{evict}.json")
        with TestClient(app) as client:
            sid = client.post("/v1/sessions", json={"title": "u"}).json()["id"]
            _seed_persisted(app, sid, ["m1", "m2", "m3"])
            if evict:
                del app.state.messages[sid]  # simulate an LRU/TTL eviction
                assert app.state.messages.resident_count == 0
            resp = client.post(f"/v1/sessions/{sid}/undo", json={"count": 2})
            body = resp.json()
            events = [e.type for e in app.state.bus._history[sid][-4:]]
            remaining = [m.id for m in app.state.messages[sid]]
            return {
                "status": resp.status_code,
                "deleted": body["deleted_message_ids"],
                "count": body["message_count"],
                "remaining": remaining,
                "events": events,
            }, events

    unevicted, _ = run(evict=False)
    evicted, _ = run(evict=True)
    assert evicted == unevicted
    assert evicted["remaining"] == ["m1"]
    assert evicted["events"] == [
        "message.deleted",
        "message.deleted",
        "session.undo",
        "session.updated",
    ]


def test_delete_message_on_evicted_session(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "d"}).json()["id"]
        _seed_persisted(app, sid, ["m1", "m2", "m3"])
        del app.state.messages[sid]
        assert app.state.messages.resident_count == 0

        resp = client.delete(f"/v1/sessions/{sid}/messages/m2")
        assert resp.status_code == 204
        assert [m.id for m in app.state.messages[sid]] == ["m1", "m3"]


def test_fork_on_evicted_session(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "f"}).json()["id"]
        _seed_persisted(app, sid, ["m1", "m2", "m3"])
        del app.state.messages[sid]
        assert app.state.messages.resident_count == 0

        resp = client.post(f"/v1/sessions/{sid}/fork", json={"at_message_id": "m2"})
        assert resp.status_code in (200, 201), resp.text
        fork_id = resp.json()["id"]
        # Inclusive truncation at m2 — the fork carries m1, m2 (source rehydrated
        # from disk transparently despite being evicted before the fork).
        assert [m.id for m in app.state.messages[fork_id]] == ["m1", "m2"]


# --------------------------------------------------------------------------- #
# Active-session pin (real wiring)
# --------------------------------------------------------------------------- #


def test_running_session_is_active(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "a"}).json()["id"]
        assert _session_is_active(app, sid) is False
        app.state.sessions.update(sid, status="running")
        assert _session_is_active(app, sid) is True


def test_live_subscriber_pins_session_against_eviction(tmp_path: Path) -> None:
    import asyncio

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid_active = client.post("/v1/sessions", json={"title": "act"}).json()["id"]
        sid_idle = client.post("/v1/sessions", json={"title": "idle"}).json()["id"]
        sid_third = client.post("/v1/sessions", json={"title": "third"}).json()["id"]
        # Seed DISK ONLY (message_store.replace_session), leaving the resident set
        # empty so the rl.get() calls below trigger real installs -> real eviction.
        for sid, mid in ((sid_active, "a1"), (sid_idle, "b1"), (sid_third, "c1")):
            app.state.message_store.replace_session(sid, [_message(mid, sid)])
        assert app.state.messages.resident_count == 0

        # Pin sid_active through the REAL bus (not by monkeypatching rl._is_active):
        # a live SSE subscriber is a queue in bus._subs[sid], exactly as EventBus.subscribe
        # registers it, and subscriber_count reads that list. This drives the real
        # bus branch of _session_is_active (resident_ledgers.py) that build_app wired
        # into the set — sabotage: delete that branch and this pin no longer holds.
        assert _session_is_active(app, sid_active) is False
        app.state.bus._subs[sid_active].append(asyncio.Queue())  # noqa: SLF001 - real bus registry
        assert app.state.bus.subscriber_count(sid_active) == 1
        assert _session_is_active(app, sid_active) is True  # via the bus-subscriber branch

        # Shrink the count cap to 1 so touching further sessions forces eviction. The
        # bus-pinned session must be SKIPPED as an eviction victim while the genuinely
        # idle one is evicted instead.
        rl = app.state.messages
        rl._cfg = type(rl._cfg)(  # noqa: SLF001 - test seam
            max_bytes=10**12, max_sessions=1, idle_ttl_s=10**9
        )

        rl.get(sid_active, [])
        rl.get(sid_idle, [])
        rl.get(sid_third, [])  # forces eviction with sid_active pinned + sid_idle idle
        assert sid_active in rl.resident_session_ids  # pinned by the live subscriber -> survives
        assert sid_idle not in rl.resident_session_ids  # the idle session was the victim


def test_rehydrate_rows_do_not_flood_the_bounded_eviction_ring(tmp_path: Path) -> None:
    # Routine rehydration is high-frequency cache traffic; it must NOT populate the
    # shared bounded ledger_evictions ring (which would flush genuine eviction rows).
    # Genuine eviction reasons MUST still be recorded there.
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sids = []
        for i in range(3):
            sid = client.post("/v1/sessions", json={"title": f"s{i}"}).json()["id"]
            app.state.message_store.replace_session(sid, [_message(f"m{i}", sid)])
            sids.append(sid)

        rl = app.state.messages
        # Several cold rehydrations — none should land in the eviction ring.
        for sid in sids:
            rl.get(sid, [])
        ring = list(getattr(app.state, "ledger_evictions", []))
        assert not any(r.get("reason") == "rehydrate" for r in ring)

        # Now force a real eviction and confirm it IS recorded in the ring. Clear the
        # cache first so the re-gets are fresh installs (which run cap enforcement).
        rl.clear()
        rl._cfg = type(rl._cfg)(max_bytes=10**12, max_sessions=1, idle_ttl_s=10**9)  # noqa: SLF001
        rl.get(sids[0], [])
        rl.get(sids[1], [])  # over cap -> evicts the idle oldest
        ring = list(getattr(app.state, "ledger_evictions", []))
        assert any(r.get("reason") == "capacity_count" for r in ring)  # sabotage: filter all -> red
