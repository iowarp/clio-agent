"""Tests for the blueprint filesystem-identity helpers (gact/blueprint_paths.py).

Split out of ``tests/test_gact/test_agent_blueprints.py`` alongside the
``_relative_to_blueprint_root`` move to its owner module (file-size ratchet,
#775/#774).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clio_agent.gact.blueprint_paths import relative_to_blueprint_root


def test_relative_to_blueprint_root_uses_stable_lexical_identity(tmp_path: Path) -> None:
    root = tmp_path / "blueprint"
    expert = root / "experts" / "main.md"
    expert.parent.mkdir(parents=True)
    expert.write_text("# Main\n", encoding="utf-8")

    assert relative_to_blueprint_root(expert, root) == Path("experts/main.md")


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
    try:
        link.symlink_to(escapee)
    except OSError as exc:
        # Windows without Developer Mode / SeCreateSymbolicLinkPrivilege raises
        # WinError 1314 ("a required privilege is not held by the client");
        # some CI/sandboxed POSIX runners also refuse symlink creation outright.
        if getattr(exc, "winerror", None) == 1314 or isinstance(exc, PermissionError):
            pytest.skip(f"symlink creation not permitted on this platform: {exc}")
        raise

    with pytest.raises(ValueError):
        relative_to_blueprint_root(link, root)
