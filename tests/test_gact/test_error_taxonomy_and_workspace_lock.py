"""#770 Wave-C C4 — error taxonomy + workspace store-lock regression.

Two defects this suite locks down:

1. **Taxonomy** — resource-not-found 404s across the GACT routes were
   tagged ``error="internal_error"``. A not-found is a *client* error;
   the on-contract tag is ``"not_found"``. Every 404 that means "the
   thing you named does not exist" must carry ``not_found``.

2. **Workspace store lock** — ``PATCH /v1/workspaces/{wid}`` mutated the
   live ``Workspace`` object returned by ``WorkspaceStore.get()`` and
   called the private ``_flush()`` *outside* the store lock, never
   bumping ``updated_at``. That races the locked mutators and violates
   the store contract. The PATCH must route through
   ``WorkspaceStore.update()`` so it serialises under ``self._lock`` and
   bumps ``updated_at``.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


def _client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "s.json"))


# --- (a) taxonomy -------------------------------------------------------

# One representative not-found 404 per affected route module. Each must
# report ``error.error == "not_found"`` (not ``internal_error``).
_NOT_FOUND_CASES = [
    ("GET", "/v1/workspaces/ws_missing", None),  # workspaces.py
    ("PUT", "/v1/agents/nope", {}),  # agents.py (upsert)
    ("DELETE", "/v1/agents/nope", None),  # agents.py (delete)
    ("POST", "/v1/sessions/sess_missing/commands/help", None),  # catalog.py
    ("GET", "/v1/sessions/sess_missing/context/policy", None),  # context.py
    ("GET", "/v1/sessions/sess_missing/context/frames", None),  # diffs.py
    ("GET", "/v1/sessions/sess_missing/messages", None),  # messages.py
    ("GET", "/v1/sessions/sess_missing/tasks", None),  # misc.py
    ("GET", "/v1/sessions/sess_nope/events", None),  # misc.py (SSE)
    ("POST", "/v1/permissions/perm_missing", {"action": "allow"}),  # permissions.py
    ("DELETE", "/v1/schedules/sched_missing", None),  # schedules.py
    ("POST", "/v1/sessions/sess_nope/cancel", None),  # sessions.py
]


@pytest.mark.parametrize(("method", "path", "body"), _NOT_FOUND_CASES)
def test_not_found_404s_are_tagged_not_found(
    tmp_path: Path, method: str, path: str, body: dict | None
) -> None:
    c = _client(tmp_path)
    resp = c.request(method, path, json=body)
    assert resp.status_code == 404, (method, path, resp.text)
    assert resp.json()["error"]["error"] == "not_found", (method, path, resp.text)


# --- (b) workspace PATCH routes through the store lock ------------------


def test_patch_workspace_bumps_updated_at_and_applies_name(tmp_path: Path) -> None:
    c = _client(tmp_path)
    created = c.post("/v1/workspaces", json={"name": "before", "root_path": "/tmp/a"}).json()
    wid = created["id"]
    before = created["updated_at"]

    resp = c.patch(f"/v1/workspaces/{wid}", json={"name": "after"})
    assert resp.status_code == 200
    patched = resp.json()
    assert patched["name"] == "after"
    # update() bumps updated_at; the buggy direct-mutation path never did.
    assert patched["updated_at"] != before

    # Persisted view agrees (flush happened under the lock).
    fetched = c.get(f"/v1/workspaces/{wid}").json()
    assert fetched["name"] == "after"
    assert fetched["updated_at"] == patched["updated_at"]


def test_patch_unknown_workspace_404s_not_found(tmp_path: Path) -> None:
    c = _client(tmp_path)
    resp = c.patch("/v1/workspaces/ws_missing", json={"name": "x"})
    assert resp.status_code == 404
    assert resp.json()["error"]["error"] == "not_found"


def test_patch_workspace_metadata_and_config_alias(tmp_path: Path) -> None:
    c = _client(tmp_path)
    wid = c.post("/v1/workspaces", json={"name": "m"}).json()["id"]

    c.patch(f"/v1/workspaces/{wid}", json={"metadata": {"colour": "blue"}})
    # The desktop sends ``config`` as an alias for metadata.
    c.patch(f"/v1/workspaces/{wid}", json={"config": {"pinned": True}})

    row = c.app.state.workspaces.get(wid)
    assert row.metadata["colour"] == "blue"
    assert row.metadata["pinned"] is True


def test_patch_workspace_serialises_with_concurrent_creates(tmp_path: Path) -> None:
    """PATCH must take the same store lock as create(), so a storm of
    concurrent PATCHes + creates loses no workspace and lands every
    rename (no torn write / flush racing a create)."""

    c = _client(tmp_path)
    # Seed a pool of workspaces to PATCH.
    pool = [c.post("/v1/workspaces", json={"name": f"w{i}"}).json()["id"] for i in range(8)]

    errors: list[str] = []

    def patch_worker(wid: str, idx: int) -> None:
        try:
            r = c.patch(f"/v1/workspaces/{wid}", json={"name": f"renamed-{idx}"})
            if r.status_code != 200:
                errors.append(f"patch {wid}: {r.status_code} {r.text}")
        except Exception as exc:  # noqa: BLE001 - surfaced via assert
            errors.append(f"patch {wid}: {exc!r}")

    def create_worker(idx: int) -> None:
        try:
            r = c.post("/v1/workspaces", json={"name": f"created-{idx}"})
            if r.status_code != 201:
                errors.append(f"create {idx}: {r.status_code} {r.text}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"create {idx}: {exc!r}")

    threads: list[threading.Thread] = []
    for idx, wid in enumerate(pool):
        threads.append(threading.Thread(target=patch_worker, args=(wid, idx)))
    for idx in range(8):
        threads.append(threading.Thread(target=create_worker, args=(idx,)))

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors

    rows = {w["id"]: w for w in c.get("/v1/workspaces").json()["workspaces"]}
    # Every seeded workspace still present and carries its rename.
    for idx, wid in enumerate(pool):
        assert wid in rows, f"lost workspace {wid}"
        assert rows[wid]["name"] == f"renamed-{idx}"
    # All 8 concurrent creates landed (+ ws_default + 8 seeded).
    created_names = {w["name"] for w in rows.values()}
    for idx in range(8):
        assert f"created-{idx}" in created_names
