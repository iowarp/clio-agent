"""Agent Blueprint packs install under a long user dir (WinError 206).

A realistic Windows user dir is long before CLIO adds anything: a long user name
plus a packaged app's ``LocalCache`` redirection is ~100 characters, and a pack's
deepest skill reference file adds another ~100 below ``agent-blueprints/<pack>``
(the default-registry sync stages it one level deeper, beside a random suffix).
Without ``LongPathsEnabled`` Windows refuses to create a directory past 248
characters or a file past 260 unless the call uses the extended-length form, so
``shutil.copytree`` failed with ``[WinError 206] The filename or extension is too
long`` and the pack never installed. The copy, the swap, the removal and the
checksum walk now go through :mod:`clio_agent.platform_paths`' extended-length
helpers.

The paths here are built to cross 260 characters whatever the temp dir's length,
so on Windows these tests exercise the limit itself; elsewhere they pin the same
behaviour.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from clio_agent.gact.agent_blueprint_refresh import uninstall_agent_blueprint
from clio_agent.gact.agent_blueprints import (
    _tree_checksum,
    install_agent_blueprint,
    read_install_metadata,
)
from clio_agent.gact.default_registry_migration import STAGING_DIR_NAME, replace_pack_atomically
from clio_agent.platform_paths import win_extended_path

_MAX_PATH = 260
# The deepest file in the shipped marketplace (document-production), 64 characters.
_DEEP_FILE = Path("skills/edit-ooxml-presentation/references/presentation-ooxml.md")


def _relative_files(root: Path) -> set[str]:
    """Every file under ``root``, relative, listed through the extended-length form."""
    base = win_extended_path(root.absolute())
    return {
        os.path.relpath(os.path.join(directory, name), base)
        for directory, _subdirs, names in os.walk(base)
        for name in names
    }


def _long_user_dir(tmp_path: Path) -> Path:
    """A user dir 200 characters long, like a packaged app's for a long user name.

    ``C:/Users/<long name>/AppData/Local/Packages/<app>/LocalCache/Local/clio-agent``
    is ~100 characters; 200 leaves room for a deeper home or a longer name while
    keeping the user dir itself creatable without extended paths (CLIO creates it
    with ordinary calls). Every pack file below it crosses MAX_PATH.
    """
    padding = max(1, 200 - len(str(tmp_path)) - len("/clio-agent") - 1)
    return tmp_path / ("u" * padding) / "clio-agent"


def _write_pack(root: Path, blueprint_id: str = "deep-pack") -> Path:
    (root / "experts").mkdir(parents=True)
    root.joinpath("AGENT.md").write_text(
        f"---\nid: {blueprint_id}\nversion: 0.1.0\ntitle: Deep Pack\nroot_expert: root\n---\n"
        "A pack with a deep skill reference.\n",
        encoding="utf-8",
    )
    root.joinpath("experts", "root.md").write_text(
        "---\nid: root\ntitle: Root\ntier: 1\nmodule:\n  kind: react\nprompt_id: deep.root\n---\n"
        "Coordinate.\n",
        encoding="utf-8",
    )
    deep = root / _DEEP_FILE
    deep.parent.mkdir(parents=True)
    deep.write_text("reference body\n", encoding="utf-8")
    return root


@pytest.fixture
def long_user_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    user_dir = _long_user_dir(tmp_path)
    monkeypatch.setenv("CLIO_USER_DIR", str(user_dir))
    monkeypatch.setenv("CLIO_AGENT_DISABLE_DEFAULT_REGISTRY_BOOTSTRAP", "1")
    return user_dir


def test_install_copies_a_pack_whose_files_cross_max_path(
    tmp_path: Path, long_user_dir: Path
) -> None:
    source = _write_pack(tmp_path / "src" / "deep-pack")
    installed_deep = long_user_dir / "agent-blueprints" / "deep-pack" / _DEEP_FILE
    assert len(str(installed_deep)) > _MAX_PATH  # the precondition this test is about

    result = install_agent_blueprint(
        source=str(source), scope="global", cwd=tmp_path / "cwd", home=tmp_path / "home"
    )

    assert [row["id"] for row in result["installed"]] == ["deep-pack"]
    dest = long_user_dir / "agent-blueprints" / "deep-pack"
    assert str(_DEEP_FILE) in _relative_files(dest)
    assert read_install_metadata(dest)["checksum"] == _tree_checksum(dest)

    # A reinstall removes the deep tree first; an uninstall removes it for good.
    install_agent_blueprint(
        source=str(source), scope="global", cwd=tmp_path / "cwd", home=tmp_path / "home"
    )
    assert str(_DEEP_FILE) in _relative_files(dest)
    uninstall_agent_blueprint(
        blueprint_id="deep-pack", scope="global", cwd=tmp_path / "cwd", home=tmp_path / "home"
    )
    assert not dest.exists()


def test_the_registry_swap_stages_and_replaces_a_pack_across_max_path(
    tmp_path: Path, long_user_dir: Path
) -> None:
    source = _write_pack(tmp_path / "src" / "deep-pack")
    install_root = long_user_dir / "agent-blueprints"
    install_root.mkdir(parents=True)
    staged_deep = install_root.parent / STAGING_DIR_NAME / "deep-pack.new-0123abcd" / _DEEP_FILE
    assert len(str(staged_deep)) > _MAX_PATH

    replace_pack_atomically(source, install_root, "deep-pack", {"source": "test"})
    # Again over an existing install: the old tree moves to a backup and is removed.
    replace_pack_atomically(source, install_root, "deep-pack", {"source": "test"})

    dest = install_root / "deep-pack"
    assert str(_DEEP_FILE) in _relative_files(dest)
    assert read_install_metadata(dest)["checksum"] == _tree_checksum(dest)
    assert list((install_root.parent / STAGING_DIR_NAME).iterdir()) == []


def test_the_checksum_is_unchanged_for_existing_installs(tmp_path: Path) -> None:
    """Installed packs carry the old checksum; the MAX_PATH-safe walk must reproduce it."""
    root = _write_pack(tmp_path / "pack")
    root.joinpath(".clio-install.md").write_text("metadata\n", encoding="utf-8")
    root.joinpath("experts", "b.md").write_text("b\n", encoding="utf-8")

    legacy = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if path.name == ".clio-install.md":
            continue
        legacy.update(str(path.relative_to(root)).encode())
        legacy.update(path.read_bytes())

    assert _tree_checksum(root) == legacy.hexdigest()


def test_tree_files_lists_in_sorted_relative_order(tmp_path: Path) -> None:
    from clio_agent.platform_paths import tree_files

    root = _write_pack(tmp_path / "pack")
    expected = sorted(p.relative_to(root) for p in root.rglob("*") if p.is_file())
    assert [rel for rel, _ in tree_files(root)] == expected
