"""Pagination-correctness regression for the artifacts list route under
``include_children=true`` (iowarp/gact-tui#363, server half).

The route clamps ``limit`` (default 50 / max 200, ``_LIST_LIMIT_DEFAULT`` /
``_LIST_LIMIT_MAX`` in ``routes/artifacts.py``) and hands out a ``next_cursor``
via ``_paginate_records``. This pins that walking ``next_cursor`` to exhaustion
over a >50-record set spanning a parent AND a child session (the
``include_children`` union/dedup path) reconstructs the FULL record set with no
duplicates and no gaps — the exact shape a TUI/web client relies on when it
pages through a busy orchestrator's artifact list.

R5 (opus adversarial review): an earlier version of this test put the child
session in the SAME workspace as the parent. ``list_session_artifacts``'s
``include_children`` branch only appends a workspace to its ``workspaces``
list when the child's workspace id differs from ones already in it
(``routes/artifacts.py``), so a same-workspace child never exercises the
multi-workspace UNION + ``(workspace_id, name)`` DEDUP loop the docstring
above claims to pin — the test passed for the wrong reason. Fixed by giving
the child its own workspace (a second root directory) so the union/dedup
path is actually on the call stack.

KNOWN BOUND (R6, not fixed here): re-versioning the boundary record BETWEEN
page fetches can still skip a record under ``include_children`` the same way
plain pagination's cursor anchors on a record's OWN prior position
(``_paginate_records``'s docstring) — this suite holds the artifact set
static across the whole walk and does not probe a concurrent mutation
mid-walk, so that edge remains unexercised/unfixed by design.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from clio_agent.gact.agent_tasks import AgentTask
from clio_agent.gact.app import build_app
from clio_agent.gact.artifacts.minting import mint_tool_declared_outputs

_LIST_LIMIT_DEFAULT = 50


def _workspace(client: TestClient, root: Path, *, name: str = "w") -> str:
    return client.post("/v1/workspaces", json={"name": name, "root_path": str(root)}).json()["id"]


def _mint_one(app, sid: str, workspace_id: str, root: Path, name: str, call_id: str) -> None:
    path = root / name
    path.write_bytes(f"content for {name}".encode("utf-8"))
    minted = mint_tool_declared_outputs(
        app,
        sid,
        tool_name="producer",
        effective_args={"output_path": str(path)},
        call_id=call_id,
        workspace_id=workspace_id,
    )
    assert minted, f"fixture mint should register {name}"


def test_include_children_pagination_reconstructs_full_set_no_dupes_no_gaps(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    parent_root = tmp_path / "ws_parent"
    child_root = tmp_path / "ws_child"
    parent_root.mkdir()
    child_root.mkdir()
    with TestClient(build_app(sessions_path=tmp_path / "s.json")) as client:
        app = client.app
        wid = _workspace(client, parent_root, name="parent")
        parent = client.post("/v1/sessions", json={"workspace_id": wid}).json()["id"]

        # A real child session in a DIFFERENT workspace (R5: same-workspace would
        # never exercise the multi-workspace union/dedup loop this test pins),
        # wired via the agent-task registry (same pattern as the GAP B
        # include_children tests in test_artifacts_s5.py).
        wid2 = _workspace(client, child_root, name="child")
        child = app.state.sessions.create(
            workspace_id=wid2, title="child", parent_session_id=parent
        )
        app.state.agent_task_registry.register(
            AgentTask(
                task_id="t_pagination",
                parent_session_id=parent,
                child_session_id=child.id,
                created_at="1",
            )
        )

        # 34 records produced by the parent, 33 by the child — 67 total, comfortably
        # over the default 50-record page so at least two pages are required.
        expected_names: set[str] = set()
        for i in range(34):
            name = f"parent_{i:03d}.csv"
            _mint_one(app, parent, wid, parent_root, name, call_id=f"call_p{i}")
            expected_names.add(name)
        for i in range(33):
            name = f"child_{i:03d}.csv"
            _mint_one(app, child.id, wid2, child_root, name, call_id=f"call_c{i}")
            expected_names.add(name)
        assert len(expected_names) == 67

        collected_names: list[str] = []
        seen_artifact_ids: set[str] = set()
        params: dict[str, object] = {"include_children": True, "limit": _LIST_LIMIT_DEFAULT}
        pages = 0
        total_count_reported: int | None = None
        while True:
            resp = client.get(f"/v1/sessions/{parent}/artifacts", params=params)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            pages += 1
            if total_count_reported is None:
                total_count_reported = body["count"]
            else:
                # `count` is the full matched-set size — stable across pages.
                assert body["count"] == total_count_reported
            page_records = body["artifacts"]
            for rec in page_records:
                name = rec["name"]
                head_id = rec["head_artifact_id"]
                # No duplicates across pages.
                assert head_id not in seen_artifact_ids, (
                    f"artifact {name!r} ({head_id}) returned on more than one page"
                )
                seen_artifact_ids.add(head_id)
                collected_names.append(name)
            next_cursor = body.get("next_cursor")
            if next_cursor is None:
                break
            params = {**params, "before": next_cursor}
            # Guard against an infinite loop if pagination regresses (never legitimately
            # more pages than records / smallest possible page size + 1).
            assert pages <= 67 + 2

        # Sabotage: drop the `before` cursor resolution (or clamp incorrectly) in
        # `_paginate_records` -> either fewer than 67 rows are collected (a gap) or a
        # `head_artifact_id` repeats across pages (an overlap) -> these assertions red.
        assert total_count_reported == 67
        assert len(collected_names) == 67
        assert set(collected_names) == expected_names
        # More than one page was actually exercised (else this test would not be
        # pinning cross-page behavior at all).
        assert pages >= 2


def test_include_children_pagination_limit_below_default_still_covers_full_set(
    tmp_path, monkeypatch
):
    """Same invariant with an explicit small limit, forcing many pages."""
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    parent_root = tmp_path / "ws_parent"
    child_root = tmp_path / "ws_child"
    parent_root.mkdir()
    child_root.mkdir()
    with TestClient(build_app(sessions_path=tmp_path / "s.json")) as client:
        app = client.app
        wid = _workspace(client, parent_root, name="parent")
        parent = client.post("/v1/sessions", json={"workspace_id": wid}).json()["id"]
        # R5: a second workspace for the child (see the module docstring).
        wid2 = _workspace(client, child_root, name="child")
        child = app.state.sessions.create(
            workspace_id=wid2, title="child", parent_session_id=parent
        )
        app.state.agent_task_registry.register(
            AgentTask(
                task_id="t_pagination_small",
                parent_session_id=parent,
                child_session_id=child.id,
                created_at="1",
            )
        )

        expected_names: set[str] = set()
        for i in range(12):
            name = f"p_{i:03d}.csv"
            _mint_one(app, parent, wid, parent_root, name, call_id=f"call_sp{i}")
            expected_names.add(name)
        for i in range(11):
            name = f"c_{i:03d}.csv"
            _mint_one(app, child.id, wid2, child_root, name, call_id=f"call_sc{i}")
            expected_names.add(name)
        assert len(expected_names) == 23

        collected: list[str] = []
        seen_ids: set[str] = set()
        params: dict[str, object] = {"include_children": True, "limit": 5}
        pages = 0
        while True:
            resp = client.get(f"/v1/sessions/{parent}/artifacts", params=params)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            pages += 1
            assert len(body["artifacts"]) <= 5
            for rec in body["artifacts"]:
                assert rec["head_artifact_id"] not in seen_ids
                seen_ids.add(rec["head_artifact_id"])
                collected.append(rec["name"])
            next_cursor = body.get("next_cursor")
            if next_cursor is None:
                break
            params = {**params, "before": next_cursor}
            assert pages <= 23 + 2

        assert set(collected) == expected_names
        assert len(collected) == 23
        # ceil(23 / 5) == 5 pages.
        assert pages == 5
