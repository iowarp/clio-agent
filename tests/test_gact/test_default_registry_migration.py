"""One-time default-registry re-sync on a clio-agent version change (v15 S8).

A deployed install clones the registry on first run and never re-syncs a
remote source, so an upgraded box keeps stale pack snapshots (an old
base-agent with no ``a2ui_catalogs``, an old EarthScope in the pre-S8 mapping
form). ``default_registry_migration`` re-installs the UNEDITED default-registry
packs once per running version, leaves locally edited packs alone with a typed
reason, and retries on the next boot after a failure.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from clio_agent.gact import default_registry_migration as migration
from clio_agent.gact.agent_blueprint_refresh import recorded_blueprint_install_reasons
from clio_agent.gact.agent_blueprints import _install_root, install_agent_blueprint

FIXTURE_PACK = Path(__file__).resolve().parents[1] / "fixtures" / "a2ui_packs" / "builtins"


def _make_pack(registry: Path, pack_id: str, marker: str) -> Path:
    root = registry / pack_id
    shutil.copytree(FIXTURE_PACK, root)
    agent_md = root / "AGENT.md"
    agent_md.write_text(
        agent_md.read_text(encoding="utf-8").replace(
            "id: a2ui-builtins-pack", f"id: {pack_id}\nversion: {marker}"
        ),
        encoding="utf-8",
    )
    return root


def _bump(registry: Path, pack_id: str, marker: str) -> None:
    agent_md = registry / pack_id / "AGENT.md"
    text = agent_md.read_text(encoding="utf-8")
    head, _, rest = text.partition("version: ")
    agent_md.write_text(head + f"version: {marker}\n" + rest.split("\n", 1)[1], encoding="utf-8")


def _installed_version(install_root: Path, pack_id: str) -> str:
    text = (install_root / pack_id / "AGENT.md").read_text(encoding="utf-8")
    return text.split("version: ", 1)[1].split("\n", 1)[0].strip()


@pytest.fixture
def registry(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """A local registry of two installed packs; returns (registry, home, cwd, install_root)."""

    migration.reset_default_registry_migration_for_tests()
    registry = tmp_path / "registry"
    home, cwd = tmp_path / "home", tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()
    _make_pack(registry, "pack-a", "1.0.0")
    _make_pack(registry, "pack-b", "1.0.0")
    install_agent_blueprint(source=str(registry), scope="global", cwd=cwd, home=home)
    yield registry, home, cwd, _install_root(home=home, cwd=cwd, scope="global")
    migration.reset_default_registry_migration_for_tests()


def _migrate(registry: Path, home: Path, cwd: Path) -> str:
    return migration.migrate_default_registry_on_version_change(
        source=str(registry), home=home, cwd=cwd, ref="", pinned=""
    )


def test_local_edits_guard_detects_a_drifted_tree(registry: tuple[Path, Path, Path, Path]) -> None:
    _registry, _home, _cwd, install_root = registry
    assert migration.pack_locally_edited(install_root / "pack-a") is False
    (install_root / "pack-a" / "NOTES.md").write_text("mine", encoding="utf-8")
    assert migration.pack_locally_edited(install_root / "pack-a") is True


def test_a_version_change_reinstalls_unedited_packs_and_spares_edited_ones(
    registry: tuple[Path, Path, Path, Path],
) -> None:
    source, home, cwd, install_root = registry
    (install_root / "pack-b" / "NOTES.md").write_text("user edit", encoding="utf-8")
    _bump(source, "pack-a", "2.0.0")
    _bump(source, "pack-b", "2.0.0")

    assert migration.recorded_sync_version(install_root) == ""  # upgraded box, never synced
    assert _migrate(source, home, cwd) == ""

    assert _installed_version(install_root, "pack-a") == "2.0.0"
    assert _installed_version(install_root, "pack-b") == "1.0.0"
    assert (install_root / "pack-b" / "NOTES.md").read_text(encoding="utf-8") == "user edit"
    assert migration.recorded_sync_version(install_root) == (migration.running_clio_agent_version())
    reasons = recorded_blueprint_install_reasons()
    assert any(
        row["reason"] == "default_registry_pack_locally_edited" and row["blueprint_id"] == "pack-b"
        for row in reasons
    )
    assert any(row["reason"] == "default_registry_migrated" for row in reasons)


def test_the_same_version_does_not_resync(registry: tuple[Path, Path, Path, Path]) -> None:
    source, home, cwd, install_root = registry
    migration.record_sync_version(install_root, migration.running_clio_agent_version())
    _bump(source, "pack-a", "2.0.0")

    assert _migrate(source, home, cwd) == ""

    assert _installed_version(install_root, "pack-a") == "1.0.0"


def test_the_next_version_change_triggers_again(
    registry: tuple[Path, Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    source, home, cwd, install_root = registry
    migration.record_sync_version(install_root, migration.running_clio_agent_version())
    _bump(source, "pack-a", "3.0.0")
    monkeypatch.setattr(migration, "running_clio_agent_version", lambda: "99.0.0")

    assert _migrate(source, home, cwd) == ""

    assert _installed_version(install_root, "pack-a") == "3.0.0"
    assert migration.recorded_sync_version(install_root) == "99.0.0"


def test_a_failed_resync_is_typed_and_retried_next_boot(
    registry: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    _source, home, cwd, install_root = registry
    unreachable = tmp_path / "no-such-registry"

    diagnostic = _migrate(unreachable, home, cwd)

    assert "failed" in diagnostic
    assert migration.recorded_sync_version(install_root) == ""  # not recorded: retry
    assert any(
        row["reason"] == "default_registry_migration_failed"
        for row in recorded_blueprint_install_reasons()
    )
    # Next boot (a new process): the gate resets and the re-sync runs again.
    migration.reset_default_registry_migration_for_tests()
    assert _migrate(_source, home, cwd) == ""
    assert migration.recorded_sync_version(install_root) != ""


def test_packs_from_another_source_are_left_alone(
    registry: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    source, home, cwd, install_root = registry
    other = tmp_path / "other"
    _make_pack(other, "pack-c", "1.0.0")
    install_agent_blueprint(source=str(other), scope="global", cwd=cwd, home=home)
    _make_pack(source, "pack-c", "5.0.0")

    assert _migrate(source, home, cwd) == ""

    assert _installed_version(install_root, "pack-c") == "1.0.0"
    assert any(
        row["reason"] == "default_registry_pack_foreign_source" and row["blueprint_id"] == "pack-c"
        for row in recorded_blueprint_install_reasons()
    )
