"""Marketplace lifecycle regressions: pins, edits, rollback and durable removals."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from clio_agent.gact import agent_blueprint_sources as sources
from clio_agent.gact import default_registry_migration as migration
from clio_agent.gact.agent_blueprint_refresh import (
    uninstall_agent_blueprint,
    update_installed_agent_blueprint,
)
from clio_agent.gact.agent_blueprints import install_agent_blueprint, read_install_metadata


def _pack(root: Path, text: str = "Original") -> Path:
    root.mkdir(parents=True)
    (root / "AGENT.md").write_text(
        f"---\nid: demo\nversion: 1.0.0\ntitle: Demo\n---\n{text}\n", encoding="utf-8"
    )
    return root


def _install(source: Path, cwd: Path, *, pin: str = "") -> Path:
    result = install_agent_blueprint(
        source=str(source), scope="workspace", cwd=cwd, pinned_commit=pin
    )
    return Path(result["installed"][0]["root"])


def test_failed_copy_preserves_installed_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _pack(tmp_path / "source")
    root = _install(source, tmp_path)
    prior = read_install_metadata(root)
    (source / "AGENT.md").write_text((source / "AGENT.md").read_text() + "Update")

    def fail_copy(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(migration, "copytree_extended", fail_copy)
    with pytest.raises(OSError, match="disk full"):
        _install(source, tmp_path)
    assert (root / "AGENT.md").read_text().endswith("Original\n")
    assert read_install_metadata(root) == prior


def test_failed_swap_restores_previous_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _pack(tmp_path / "source")
    root = _install(source, tmp_path)
    rename = migration.rename_extended

    def fail_new(source: Path, destination: Path) -> None:
        if ".new-" in source.name:
            raise OSError("sharing violation")
        rename(source, destination)

    monkeypatch.setattr(migration, "rename_extended", fail_new)
    with pytest.raises(OSError, match="sharing violation"):
        _install(source, tmp_path)
    assert (root / "AGENT.md").read_text().endswith("Original\n")


def test_individual_update_refuses_local_edits(tmp_path: Path) -> None:
    source = _pack(tmp_path / "source")
    root = _install(source, tmp_path)
    edited = (root / "AGENT.md").read_text() + "Unsaved upstream draft\n"
    (root / "AGENT.md").write_text(edited)
    with pytest.raises(ValueError, match="local_edits_present"):
        update_installed_agent_blueprint(blueprint_id="demo", scope="workspace", cwd=tmp_path)
    assert (root / "AGENT.md").read_text() == edited


def test_individual_update_preserves_pin(tmp_path: Path) -> None:
    source = _pack(tmp_path / "source")
    for args in (
        ["init"],
        ["add", "."],
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "-m",
            "initial",
        ],
    ):
        subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True)
    pin = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    root = _install(source, tmp_path, pin=pin)
    update_installed_agent_blueprint(blueprint_id="demo", scope="workspace", cwd=tmp_path)
    assert read_install_metadata(root)["pinned_commit"] == pin
    with pytest.raises(ValueError, match="pinned_revision"):
        _install(source, tmp_path)
    (source / "AGENT.md").write_text((source / "AGENT.md").read_text() + "Uncommitted edit\n")
    with pytest.raises(ValueError, match="uncommitted changes"):
        update_installed_agent_blueprint(blueprint_id="demo", scope="workspace", cwd=tmp_path)
    assert (root / "AGENT.md").read_text().endswith("Original\n")


def test_workspace_uninstall_survives_bulk_refresh(tmp_path: Path) -> None:
    source = _pack(tmp_path / "source")
    _install(source, tmp_path)
    uninstall_agent_blueprint(blueprint_id="demo", scope="workspace", cwd=tmp_path)
    skips = sources.source_install_skip_ids(scope="workspace", cwd=tmp_path)
    assert list(skips.values()) == ["user_uninstalled"]
    assert next(iter(skips)).endswith("::demo")
    assert sources.source_install_skip_ids(scope="workspace", cwd=tmp_path / "other") == {}
    _install(source, tmp_path)
    assert sources.source_install_skip_ids(scope="workspace", cwd=tmp_path) == {}


def test_forgotten_default_source_is_not_resurrected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sources, "sources_path", lambda: tmp_path / "sources.json")
    source = _pack(tmp_path / "source")
    root = _install(source, tmp_path)
    arguments = {
        "source": str(source),
        "ref": "main",
        "pinned_commit": "",
        "install_root": root.parent,
    }
    row = sources.record_default_agent_blueprint_source(**arguments)
    assert sources.delete_agent_blueprint_source(row["id"])
    assert sources.record_default_agent_blueprint_source(**arguments) == {}
    assert sources.load_agent_blueprint_sources() == []
    assert root.exists(), "forgetting a marketplace must retain installed blueprints"
    sources.upsert_agent_blueprint_source(row)
    assert sources.record_default_agent_blueprint_source(**arguments)["id"] == row["id"]


def test_other_source_cannot_overwrite_installed_owner(tmp_path: Path) -> None:
    source = _pack(tmp_path / "source")
    root = _install(source, tmp_path)
    foreign = _pack(tmp_path / "foreign", "Other author")
    other = _install(foreign, tmp_path)
    assert root != other
    assert read_install_metadata(root)["source"] == str(source)
    assert read_install_metadata(other)["source"] == str(foreign)
    from clio_agent.gact.blueprint_identity import AmbiguousBlueprintError, installed_root

    with pytest.raises(AmbiguousBlueprintError):
        installed_root(root.parent, "demo")
    uninstall_agent_blueprint(
        blueprint_id=f"workspace::{read_install_metadata(other)['source_id']}::demo",
        scope="workspace",
        cwd=tmp_path,
    )
    assert root.exists()
    assert not other.exists()


def test_discovery_preserves_default_marketplace_configuration_and_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read cannot reset a user's pin, failed operation or available choices."""
    monkeypatch.setattr(sources, "sources_path", lambda: tmp_path / "sources.json")
    source = _pack(tmp_path / "source")
    root = _install(source, tmp_path)
    arguments = {
        "source": str(source),
        "ref": "main",
        "pinned_commit": "",
        "install_root": root.parent,
    }
    row = sources.record_default_agent_blueprint_source(**arguments)
    row.update(name="Lab marketplace", pinned_commit="abc123", status="error", error="Offline")
    sources.upsert_agent_blueprint_source(row)
    before = sources.load_agent_blueprint_sources()[0]
    uninstall_agent_blueprint(blueprint_id="demo", scope="workspace", cwd=tmp_path)
    assert sources.record_default_agent_blueprint_source(**arguments) == before
    assert sources.load_agent_blueprint_sources() == [before]
    assert before["available_blueprints"][0]["id"] == "demo"


def test_qualified_file_routes_do_not_choose_an_ambiguous_legacy_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from clio_agent.gact.app import build_app

    monkeypatch.setenv("CLIO_USER_DIR", str(tmp_path / "user"))
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as client:
        identities = []
        for label in ("first", "second"):
            source = _pack(tmp_path / label, label)
            response = client.post(
                "/v1/agent-blueprints/install", json={"source": str(source), "scope": "global"}
            )
            assert response.status_code == 201, response.text
            identities.append(response.json()["installed"][0]["identity"])
        assert len(set(identities)) == 2
        assert client.get("/v1/agent-blueprints/demo/files").status_code == 409
        for identity, label in zip(identities, ("first", "second"), strict=True):
            response = client.get(f"/v1/agent-blueprints/{identity}/files/read?path=AGENT.md")
            assert response.status_code == 200, response.text
            assert label in response.text
        response = client.delete(f"/v1/agent-blueprints/{identities[1]}?scope=global")
        assert response.status_code == 200, response.text
        assert client.get(f"/v1/agent-blueprints/{identities[0]}/files").status_code == 200
