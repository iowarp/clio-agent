"""Spilled shell output is owned by its session and deleted with it (#1487).

``.clio/tool-output/`` must not grow forever: each session's spills live under
``.clio/tool-output/<session_id>/`` and the session-delete path removes that
folder, with one typed log line per cleanup. No timers, no TTL.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.tools.servers.shell_spill_store import (
    SPILLS_DELETED_REASON,
    delete_session_spills,
    spill_directory,
)


def _seed(root: Path, sid: str, names: tuple[str, ...]) -> Path:
    folder = spill_directory(root, session_id=sid)
    folder.mkdir(parents=True)
    for name in names:
        (folder / name).write_text("x" * 10, encoding="utf-8")
    return folder


def test_delete_session_spills_removes_only_that_session(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    mine = _seed(tmp_path, "sess_mine", ("a.stdout.txt", "a.stderr.txt"))
    other = _seed(tmp_path, "sess_other", ("b.stdout.txt",))

    with caplog.at_level(logging.INFO, logger="clio_agent.tools.servers.shell_spill_store"):
        removed = delete_session_spills(tmp_path, "sess_mine")

    assert removed == 2
    assert not mine.exists()
    assert (other / "b.stdout.txt").exists()
    lines = [r.getMessage() for r in caplog.records if SPILLS_DELETED_REASON in r.getMessage()]
    assert len(lines) == 1
    assert "session=sess_mine" in lines[0] and "files=2" in lines[0]


def test_delete_session_spills_is_a_noop_without_a_folder(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="clio_agent.tools.servers.shell_spill_store"):
        assert delete_session_spills(tmp_path, "sess_none") == 0
    assert not [r for r in caplog.records if SPILLS_DELETED_REASON in r.getMessage()]


def test_delete_session_spills_refuses_an_unsafe_session_id(tmp_path: Path) -> None:
    victim = tmp_path / ".clio" / "keep.txt"
    victim.parent.mkdir(parents=True)
    victim.write_text("keep", encoding="utf-8")
    assert delete_session_spills(tmp_path, "..") == 0
    assert delete_session_spills(tmp_path, "../..") == 0
    assert victim.exists()


def test_session_delete_route_removes_that_sessions_spills(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    app = build_app(sessions_path=tmp_path / "s.json")
    workspace = app.state.workspaces.create(name="spill-ws", root_path=str(root))
    doomed = app.state.sessions.create(workspace_id=workspace.id, title="doomed")
    kept = app.state.sessions.create(workspace_id=workspace.id, title="kept")
    doomed_dir = _seed(root, doomed.id, ("c.stdout.txt",))
    kept_dir = _seed(root, kept.id, ("d.stdout.txt",))

    client = TestClient(app)
    assert client.delete(f"/v1/sessions/{doomed.id}").status_code == 204

    assert not doomed_dir.exists()
    assert (kept_dir / "d.stdout.txt").exists()
