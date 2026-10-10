"""F054: the bundled marketplace stays ONE source when CLIO's install path moves.

The bundled source is the running checkout's ``external/clio-agent-marketplace``
path, so a CLIO started from a second checkout against the same home installed
every bundled blueprint again under ``<src_id>--<id>`` and each bare id became
``ambiguous_blueprint``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from clio_agent.gact import agent_blueprint_refresh as refresh
from clio_agent.gact.agent_blueprint_sources import (
    load_agent_blueprint_sources,
    record_default_agent_blueprint_source,
    source_registry_id,
)
from clio_agent.gact.agent_blueprints import (
    DEFAULT_REGISTRY_REF,
    _install_root,
    _write_install_metadata,
    install_agent_blueprint,
    read_install_metadata,
)
from clio_agent.gact.blueprint_identity import AmbiguousBlueprintError, installed_root
from clio_agent.gact.default_registry_relocation import reconcile_moved_default_registry_copies
from tests._marketplace import MARKETPLACE_ROOT, REPO_ROOT

# Only the path shape is used; every pack is a local fixture, never a network clone.
pytestmark = pytest.mark.marketplace

FIXTURE_PACK = Path(__file__).resolve().parents[1] / "fixtures" / "a2ui_packs" / "builtins"


def _checkout(tmp_path: Path, name: str, packs: tuple[str, ...]) -> Path:
    registry = tmp_path / name / MARKETPLACE_ROOT.relative_to(REPO_ROOT)
    for pack_id in packs:
        root = registry / pack_id
        shutil.copytree(FIXTURE_PACK, root)
        agent_md = root / "AGENT.md"
        agent_md.write_text(
            agent_md.read_text(encoding="utf-8").replace(
                "id: a2ui-builtins-pack", f"id: {pack_id}"
            ),
            encoding="utf-8",
        )
    return registry


def _dirs(install_root: Path) -> list[Path]:
    return sorted(p for p in install_root.iterdir() if p.is_dir())


def _install(source: Path, home: Path, cwd: Path) -> None:
    install_agent_blueprint(
        source=str(source), scope="global", cwd=cwd, home=home, ref=DEFAULT_REGISTRY_REF
    )


@pytest.fixture
def moved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path, Path]:
    """Packs installed from checkout A (pre-identity metadata) then from checkout B."""
    monkeypatch.setenv("CLIO_USER_DIR", str(tmp_path / "user"))
    home, cwd = tmp_path / "home", tmp_path / "cwd"
    old = _checkout(tmp_path, "old-checkout", ("pack-a", "pack-b"))
    new = _checkout(tmp_path, "new-checkout", ("pack-a", "pack-b"))
    _install(old, home, cwd)
    install_root = _install_root(home=home, cwd=cwd, scope="global")
    for path in _dirs(install_root):  # baseline wrote no source_id
        metadata = read_install_metadata(path)
        metadata.pop("source_id", None)
        _write_install_metadata(path, metadata)
    _install(new, home, cwd)
    return old, new, home, install_root


def test_second_checkout_duplicates_every_bundled_id(moved: tuple[Path, Path, Path, Path]) -> None:
    _, _, _, install_root = moved
    names = [p.name for p in _dirs(install_root)]
    assert len(names) == 4
    assert {"pack-a", "pack-b"} <= set(names)
    with pytest.raises(AmbiguousBlueprintError):
        installed_root(install_root, "pack-a")


def test_reconcile_keeps_one_copy_repointed_to_current_checkout(
    moved: tuple[Path, Path, Path, Path],
) -> None:
    old, new, _, install_root = moved

    actions = reconcile_moved_default_registry_copies(source=str(new), install_root=install_root)

    assert [p.name for p in _dirs(install_root)] == ["pack-a", "pack-b"]
    assert {(a["action"], a["id"]) for a in actions} == {
        ("retired", "pack-a"),
        ("retired", "pack-b"),
        ("repointed", "pack-a"),
        ("repointed", "pack-b"),
    }
    metadata = read_install_metadata(installed_root(install_root, "pack-a"))
    assert metadata["source"] == str(new)
    assert metadata["source_id"] == source_registry_id(str(new), DEFAULT_REGISTRY_REF)
    assert metadata["repointed_from"] == str(old)
    # Idempotent: a second boot changes nothing.
    assert reconcile_moved_default_registry_copies(source=str(new), install_root=install_root) == []


def test_reconcile_never_removes_a_copy_with_local_edits(
    moved: tuple[Path, Path, Path, Path],
) -> None:
    _, new, _, install_root = moved
    edited = next(p for p in _dirs(install_root) if p.name.endswith("--pack-a"))
    (edited / "AGENT.md").write_text(
        (edited / "AGENT.md").read_text(encoding="utf-8") + "\nlocal edit\n", encoding="utf-8"
    )

    actions = reconcile_moved_default_registry_copies(source=str(new), install_root=install_root)

    assert {"action": "kept", "id": "pack-a", "path": str(edited)} in actions
    assert edited.is_dir()
    assert not any(p.name.endswith("--pack-b") for p in _dirs(install_root))


def test_old_only_copies_are_repointed_and_sync_installs_no_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_USER_DIR", str(tmp_path / "user"))
    home, cwd = tmp_path / "home", tmp_path / "cwd"
    old = _checkout(tmp_path, "old-checkout", ("pack-a",))
    new = _checkout(tmp_path, "new-checkout", ("pack-a",))
    _install(old, home, cwd)
    install_root = _install_root(home=home, cwd=cwd, scope="global")

    reconcile_moved_default_registry_copies(source=str(new), install_root=install_root)
    refresh._SYNC_COMPLETED_FOR.clear()
    assert refresh.sync_local_registry_packs(source=str(new), home=home, cwd=cwd, pinned="") == ""

    assert [p.name for p in _dirs(install_root)] == ["pack-a"]
    assert read_install_metadata(install_root / "pack-a")["source"] == str(new)


def test_unrelated_marketplace_copies_are_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_USER_DIR", str(tmp_path / "user"))
    home, cwd = tmp_path / "home", tmp_path / "cwd"
    user_market = tmp_path / "my-market"
    shutil.copytree(_checkout(tmp_path, "x", ("pack-a",)), user_market)
    new = _checkout(tmp_path, "new-checkout", ("pack-a",))
    _install(user_market, home, cwd)
    _install(new, home, cwd)
    install_root = _install_root(home=home, cwd=cwd, scope="global")

    assert reconcile_moved_default_registry_copies(source=str(new), install_root=install_root) == []
    assert len(_dirs(install_root)) == 2


def test_default_source_row_is_replaced_not_duplicated(
    moved: tuple[Path, Path, Path, Path],
) -> None:
    old, new, _, install_root = moved
    for source in (old, new):
        record_default_agent_blueprint_source(
            source=str(source),
            ref=DEFAULT_REGISTRY_REF,
            pinned_commit="",
            install_root=install_root,
        )

    defaults = [row for row in load_agent_blueprint_sources() if row.get("is_default")]
    assert [row["source"] for row in defaults] == [str(new)]
