"""Tests for the blueprint filesystem-identity helpers (gact/blueprint_paths.py).

Split out of ``tests/test_gact/test_agent_blueprints.py`` alongside the
``_relative_to_blueprint_root`` move to its owner module (file-size ratchet,
#775/#774).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from clio_agent.gact.blueprint_paths import relative_to_blueprint_root


def _can_create_symlinks() -> bool:
    """Probe once, at collection time, whether this process can create a
    symlink. Windows without Developer Mode / SeCreateSymbolicLinkPrivilege
    raises WinError 1314 ("a required privilege is not held by the client");
    some CI/sandboxed POSIX runners also refuse symlink creation outright.
    """
    with tempfile.TemporaryDirectory(prefix="clio-symlink-probe-") as probe_dir:
        target = Path(probe_dir) / "target"
        target.write_text("x", encoding="utf-8")
        link = Path(probe_dir) / "link"
        try:
            link.symlink_to(target)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314 or isinstance(exc, PermissionError):
                return False
            raise
        return True


_SYMLINKS_SUPPORTED = _can_create_symlinks()


def test_relative_to_blueprint_root_uses_stable_lexical_identity(tmp_path: Path) -> None:
    root = tmp_path / "blueprint"
    expert = root / "experts" / "main.md"
    expert.parent.mkdir(parents=True)
    expert.write_text("# Main\n", encoding="utf-8")

    assert relative_to_blueprint_root(expert, root) == Path("experts/main.md")


@pytest.mark.skipif(
    not _SYMLINKS_SUPPORTED,
    reason=(
        "symlink creation not permitted on this platform (Windows without "
        "Developer Mode / SeCreateSymbolicLinkPrivilege, or a sandboxed runner)"
    ),
)
def test_relative_to_blueprint_root_rejects_escaping_symlink(tmp_path: Path) -> None:
    """A symlink lexically under the blueprint root but resolving OUTSIDE it
    must still be rejected -- the Windows packaged-path fallback only forgives
    an EQUIVALENT root (a resolve()-vs-lexical redirection alias), never a
    genuinely different directory."""

    root = tmp_path / "blueprint"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    escapee = outside / "escapee.md"
    escapee.write_text("# Escapee\n", encoding="utf-8")

    link = root / "escapee.md"
    link.symlink_to(escapee)

    with pytest.raises(ValueError):
        relative_to_blueprint_root(link, root)
