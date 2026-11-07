"""Search and glob text names the searched folder once and lists paths relative to it."""

from __future__ import annotations

from pathlib import Path

import pytest

from clio_agent.tools.fs_search_rg import glob_files, search_files


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "deep" / "workspace" / "data"
    for sub in ("a", "b/c"):
        (root / sub).mkdir(parents=True)
        (root / sub / "notes.txt").write_text("alpha\nbeta needle\n", encoding="utf-8")
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    return root


def _text(result: object) -> str:
    return "".join(c.text for c in result.content)  # type: ignore[attr-defined]


def test_glob_lists_relative_paths_and_keeps_absolute_ones_structured(tree: Path) -> None:
    """SABOTAGE: return ``name`` from ``_shown`` and every line repeats the folder."""
    result = glob_files("**/*.txt", str(tree))
    text = _text(result)
    assert text.count(str(tree)) == 1  # the header names it once
    assert {"a/notes.txt", "b/c/notes.txt"} <= set(text.splitlines())
    assert all(Path(p).is_absolute() for p in result.structured_content["files"])


@pytest.mark.parametrize("mode", ["files", "content"])
def test_search_lists_relative_paths(tree: Path, mode: str) -> None:
    result = search_files(str(tree), "needle", mode=mode)
    text = _text(result)
    assert text.count(str(tree)) == 1
    assert "b/c/notes.txt" in text
