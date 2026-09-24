"""One-time default-registry re-sync on a clio-agent version change (v15 S8).

A deployed install clones the registry on first run and never re-syncs a
remote source, so an upgraded box keeps stale pack snapshots (an old
base-agent with no ``a2ui_catalogs``, an old EarthScope in the pre-S8 mapping
form). ``default_registry_migration`` re-installs the UNEDITED default-registry
packs once per running version -- off the request path, under a cross-process
lock, one atomic swap per pack, never raising, with backoff after failures.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from clio_agent.gact import default_registry_migration as migration
from clio_agent.gact.agent_blueprint_refresh import (
    recorded_blueprint_install_reasons,
    write_uninstalled_tombstones,
)
from clio_agent.gact.agent_blueprints import (
    _install_root,
    _write_install_metadata,
    install_agent_blueprint,
    read_install_metadata,
)

FIXTURE_PACK = Path(__file__).resolve().parents[1] / "fixtures" / "a2ui_packs" / "builtins"
_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "clio-test",
    "GIT_AUTHOR_EMAIL": "clio-test@example.com",
    "GIT_COMMITTER_NAME": "clio-test",
    "GIT_COMMITTER_EMAIL": "clio-test@example.com",
}


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


def _reasons(reason: str, **match: object) -> list[dict[str, object]]:
    return [
        row
        for row in recorded_blueprint_install_reasons()
        if row["reason"] == reason and all(row.get(k) == v for k, v in match.items())
    ]


@pytest.fixture
def registry(tmp_path: Path) -> Iterator[tuple[Path, Path, Path, Path]]:
    """A local registry of two installed packs; yields (registry, home, cwd, install_root)."""

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


def _migrate(source: Path | str, home: Path, cwd: Path, *, now: float = 1000.0) -> str:
    migration.reset_default_registry_migration_for_tests()  # each call is a "boot"
    return migration.migrate_default_registry_on_version_change(
        source=str(source), home=home, cwd=cwd, ref="", pinned="", now=lambda: now
    )


# ---- trigger + skip rules ----------------------------------------------------------


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
    assert migration.pack_locally_edited(install_root / "pack-a") is False
    assert _installed_version(install_root, "pack-b") == "1.0.0"
    assert (install_root / "pack-b" / "NOTES.md").read_text(encoding="utf-8") == "user edit"
    assert migration.recorded_sync_version(install_root) == migration.running_clio_agent_version()
    assert _reasons("default_registry_pack_locally_edited", blueprint_id="pack-b")
    assert _reasons("default_registry_migrated")


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


def test_a_user_uninstalled_pack_is_not_resurrected(
    registry: tuple[Path, Path, Path, Path],
) -> None:
    source, home, cwd, install_root = registry
    shutil.rmtree(install_root / "pack-b")
    write_uninstalled_tombstones({"pack-b"}, home=home, cwd=cwd)

    assert _migrate(source, home, cwd) == ""

    assert not (install_root / "pack-b").exists()


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
    assert _reasons("default_registry_pack_foreign_source", blueprint_id="pack-c")


def test_a_deliberately_pinned_pack_is_left_alone(
    registry: tuple[Path, Path, Path, Path],
) -> None:
    source, home, cwd, install_root = registry
    pinned_root = install_root / "pack-a"
    metadata = read_install_metadata(pinned_root)
    _write_install_metadata(pinned_root, {**metadata, "ref": "release-1"})
    _bump(source, "pack-a", "2.0.0")

    assert _migrate(source, home, cwd) == ""

    assert _installed_version(install_root, "pack-a") == "1.0.0"
    assert _reasons("default_registry_pack_pinned", blueprint_id="pack-a")


# ---- safety: lock, atomic swap, guards, backoff ----------------------------------


def test_a_held_lock_skips_this_time_with_a_typed_reason(
    registry: tuple[Path, Path, Path, Path],
) -> None:
    from filelock import FileLock

    source, home, cwd, install_root = registry
    _bump(source, "pack-a", "2.0.0")
    other_process = FileLock(str(install_root / migration.LOCK_NAME), timeout=0)
    other_process.acquire()
    try:
        diagnostic = _migrate(source, home, cwd)
    finally:
        other_process.release()

    assert "lock" in diagnostic
    assert _installed_version(install_root, "pack-a") == "1.0.0"
    assert migration.recorded_sync_version(install_root) == ""
    assert _reasons("default_registry_migration_busy")


def test_a_failed_swap_restores_the_previous_pack(
    registry: tuple[Path, Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    source, home, cwd, install_root = registry
    _bump(source, "pack-a", "2.0.0")
    real_rename = Path.rename

    def failing_rename(self: Path, target: Path) -> Path:
        if self.name.startswith("pack-a.new-"):
            raise OSError("simulated failure moving the new pack into place")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", failing_rename)
    diagnostic = _migrate(source, home, cwd)
    monkeypatch.setattr(Path, "rename", real_rename)

    assert "failed" in diagnostic
    assert _installed_version(install_root, "pack-a") == "1.0.0"
    assert migration.pack_locally_edited(install_root / "pack-a") is False
    staging = install_root.parent / migration.STAGING_DIR_NAME
    assert not staging.exists() or not any(staging.iterdir())


def test_any_exception_is_typed_and_never_escapes(
    registry: tuple[Path, Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    source, home, cwd, install_root = registry

    def boom(*_args: object, **_kwargs: object) -> dict[str, str]:
        raise RuntimeError("checksum read exploded")

    monkeypatch.setattr(migration, "_skip_ids", boom)
    migration.reset_default_registry_migration_for_tests()
    first = migration.migrate_default_registry_on_version_change(
        source=str(source), home=home, cwd=cwd, ref="", pinned=""
    )
    second = migration.migrate_default_registry_on_version_change(
        source=str(source), home=home, cwd=cwd, ref="", pinned=""
    )

    assert "checksum read exploded" in first
    assert second == ""  # the once-per-process gate was set despite the failure
    assert _reasons("default_registry_migration_failed")


def test_a_failing_state_write_is_a_typed_failure(
    registry: tuple[Path, Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    source, home, cwd, _install_root_dir = registry

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(migration, "record_sync_version", refuse)

    assert "disk full" in _migrate(source, home, cwd)
    assert _reasons("default_registry_migration_failed")


def test_failures_back_off_on_the_same_version(
    registry: tuple[Path, Path, Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, home, cwd, install_root = registry
    unreachable = tmp_path / "no-such-registry"
    _bump(source, "pack-a", "2.0.0")

    assert "failed" in _migrate(unreachable, home, cwd, now=0.0)
    assert migration.recorded_sync_version(install_root) == ""  # not recorded: retry later
    # Within the first backoff window (1 h) the next boot does not retry.
    assert _migrate(source, home, cwd, now=600.0) == ""
    assert _installed_version(install_root, "pack-a") == "1.0.0"
    # After it, the retry runs and succeeds.
    assert _migrate(source, home, cwd, now=3601.0) == ""
    assert _installed_version(install_root, "pack-a") == "2.0.0"


def test_a_new_version_is_due_immediately_after_a_failure(
    registry: tuple[Path, Path, Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, home, cwd, install_root = registry
    assert "failed" in _migrate(tmp_path / "no-such-registry", home, cwd, now=0.0)
    _bump(source, "pack-a", "2.0.0")
    monkeypatch.setattr(migration, "running_clio_agent_version", lambda: "99.0.0")

    assert _migrate(source, home, cwd, now=10.0) == ""  # "Update all" restarted on 99.0.0

    assert _installed_version(install_root, "pack-a") == "2.0.0"


def test_the_backoff_schedule() -> None:
    state = {"failure_version": "1", "attempts": 2, "last_failure_at": 0}
    assert not migration.migration_due(state, "1", 6 * 3600 - 1)
    assert migration.migration_due(state, "1", 6 * 3600)
    assert not migration.migration_due({"clio_agent_version": "1"}, "1", 10**9)


# ---- remote source: a local bare repo cloned over file:// (no network) -------------


def test_a_remote_git_registry_is_cloned_and_resynced(tmp_path: Path) -> None:
    migration.reset_default_registry_migration_for_tests()
    work = tmp_path / "work"
    _make_pack(work, "pack-a", "1.0.0")
    bare = tmp_path / "registry.git"
    for args in (
        ["init", "-b", "main", str(work)],
        ["-C", str(work), "add", "-A"],
        ["-C", str(work), "commit", "-m", "v1"],
        ["clone", "--bare", str(work), str(bare)],
    ):
        subprocess.run(["git", *args], check=True, env=_GIT_ENV, capture_output=True)
    source = bare.as_uri()
    home, cwd = tmp_path / "home", tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()
    install_agent_blueprint(source=source, scope="global", cwd=cwd, home=home)
    install_root = _install_root(home=home, cwd=cwd, scope="global")
    _bump(work, "pack-a", "2.0.0")
    for args in (
        ["-C", str(work), "commit", "-am", "v2"],
        ["-C", str(work), "push", str(bare), "main"],
    ):
        subprocess.run(["git", *args], check=True, env=_GIT_ENV, capture_output=True)

    assert _migrate(source, home, cwd) == ""

    assert _installed_version(install_root, "pack-a") == "2.0.0"
    assert read_install_metadata(install_root / "pack-a")["source_kind"] == "git"
    assert migration.pack_locally_edited(install_root / "pack-a") is False


# ---- wiring: startup thread, discovery path ----------------------------------------


def test_the_startup_thread_is_off_when_the_bootstrap_is_disabled() -> None:
    # tests/conftest.py disables the default-registry bootstrap for isolation.
    assert migration.start_in_background() is None


def test_discovery_bootstrap_does_not_run_the_resync(
    registry: tuple[Path, Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ensure_default_registry_bootstrap`` is on the discovery (request) path, so
    the version-change re-sync must never run inside it."""

    from clio_agent.gact import agent_blueprint_refresh as refresh

    calls: list[str] = []
    monkeypatch.setattr(
        migration,
        "migrate_default_registry_on_version_change",
        lambda **_kwargs: calls.append("migrate") or "",
    )
    monkeypatch.setenv("CLIO_AGENT_DISABLE_DEFAULT_REGISTRY_BOOTSTRAP", "0")
    source, home, cwd, _root = registry
    monkeypatch.setattr(refresh, "default_registry_install_source", lambda: str(source))
    refresh.ensure_default_registry_bootstrap(home=home, cwd=cwd)

    assert calls == []


def test_first_run_marker_write_failure_never_escapes_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("read-only config dir")

    monkeypatch.setattr(migration, "record_sync_version", refuse)
    migration.record_first_run_version(tmp_path / "install-root")  # must not raise

    assert _reasons("default_registry_migration_failed", stage="first_run_marker")
