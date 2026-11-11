"""One workspace per filesystem root (gact-tui#219)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from clio_agent.gact.app import build_app
from clio_agent.gact.workspace_roots import WorkspaceRootConflict, same_root
from clio_agent.gact.workspaces import WorkspaceStore


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


def _store(tmp_path: Path) -> WorkspaceStore:
    return WorkspaceStore(path=tmp_path / "workspaces.json")


# -- equivalence ----------------------------------------------------------


def test_equivalent_spellings_are_the_same_root(project: Path) -> None:
    assert same_root(str(project), f"{project}/")
    assert same_root(str(project), f"{project}/sub/..")
    assert same_root(str(project), f"{project}/./")


def test_a_symlink_to_a_root_is_the_same_root(project: Path, tmp_path: Path) -> None:
    link = tmp_path / "link"
    link.symlink_to(project, target_is_directory=True)
    assert same_root(str(project), str(link))


def test_home_relative_root_matches_its_expansion(
    project: Path, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert same_root(str(project), "~/project")


def test_different_folders_and_empty_roots_never_match(project: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    assert not same_root(str(project), str(other))
    assert not same_root("", "")
    assert not same_root(str(project), "")


# -- store ----------------------------------------------------------------


def test_create_refuses_a_root_another_workspace_pins(tmp_path: Path, project: Path) -> None:
    store = _store(tmp_path)
    first = store.create(name="a", root_path=str(project))
    with pytest.raises(WorkspaceRootConflict) as caught:
        store.create(name="b", root_path=f"{project}/")
    assert caught.value.existing.id == first.id
    assert [ws.name for ws in store.list()].count("b") == 0


def test_workspaces_without_a_root_do_not_conflict(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create(name="a")
    store.create(name="b")
    assert {ws.name for ws in store.list()} >= {"a", "b"}


def test_update_refuses_another_workspaces_root_and_changes_nothing(
    tmp_path: Path, project: Path
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    store = _store(tmp_path)
    first = store.create(name="a", root_path=str(project))
    second = store.create(name="b", root_path=str(other))
    with pytest.raises(WorkspaceRootConflict) as caught:
        store.update(second.id, name="renamed", root_path=str(project))
    assert caught.value.existing.id == first.id
    unchanged = store.get(second.id)
    assert unchanged is not None
    assert (unchanged.name, unchanged.root_path) == ("b", str(other))


def test_update_may_respell_its_own_root(tmp_path: Path, project: Path) -> None:
    store = _store(tmp_path)
    ws = store.create(name="a", root_path=str(project))
    updated = store.update(ws.id, root_path=f"{project}/")
    assert updated is not None
    assert updated.root_path == f"{project}/"


def test_stored_duplicates_load_unchanged_with_a_warning(
    tmp_path: Path, project: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rows = [
        {"id": "ws_default", "name": "default", "root_path": str(tmp_path)},
        {"id": "ws_one", "name": "one", "root_path": str(project)},
        {"id": "ws_two", "name": "two", "root_path": f"{project}/"},
    ]
    (tmp_path / "workspaces.json").write_text(json.dumps({"workspaces": rows}))
    with caplog.at_level(logging.WARNING, logger="clio_agent.gact.workspaces"):
        store = _store(tmp_path)
    assert {ws.id for ws in store.list()} == {"ws_default", "ws_one", "ws_two"}
    warning = next(r.getMessage() for r in caplog.records if "share a root" in r.getMessage())
    assert "reason=shared_root_predates_uniqueness" in warning
    assert "ws_one,ws_two" in warning


# -- routes ---------------------------------------------------------------


def _client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "s.json"))


def test_post_with_a_pinned_root_is_a_409_naming_the_owner(tmp_path: Path, project: Path) -> None:
    c = _client(tmp_path)
    first = c.post("/v1/workspaces", json={"name": "first", "root_path": str(project)})
    assert first.status_code == 201
    resp = c.post("/v1/workspaces", json={"name": "second", "root_path": f"{project}/x/.."})
    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["error"] == "workspace_root_conflict"
    assert error["recoverable"] is True
    assert error["details"]["workspace_id"] == first.json()["id"]
    assert error["details"]["workspace_name"] == "first"
    assert '"first"' in error["message"]
    names = [w["name"] for w in c.get("/v1/workspaces").json()["workspaces"]]
    assert "second" not in names


def test_patch_onto_a_pinned_root_is_a_409(tmp_path: Path, project: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    c = _client(tmp_path)
    first = c.post("/v1/workspaces", json={"name": "first", "root_path": str(project)}).json()
    second = c.post("/v1/workspaces", json={"name": "second", "root_path": str(other)}).json()
    resp = c.patch(f"/v1/workspaces/{second['id']}", json={"root_path": str(project)})
    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["workspace_id"] == first["id"]
    assert c.get(f"/v1/workspaces/{second['id']}").json()["root_path"] == str(other)
