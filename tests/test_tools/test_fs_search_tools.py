"""The FS search tools ``glob`` and ``grep`` (``fs_glob`` / ``fs_grep``), in memory.

Every call goes through ``fastmcp.Client(fs_server)``, so the tools are exercised
through the same MCP schema and result path the gateway uses. The pytest temp dir is
an allowed root under the test configuration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from clio_agent.tools.servers.fs_server import fs_server


async def _call(name: str, arguments: dict[str, Any]) -> Any:
    async with Client(fs_server) as client:
        return await client.call_tool(name, arguments, raise_on_error=False)


async def _data(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await _call(name, arguments)
    assert not result.is_error, result.content
    return result.structured_content


def _error_text(result: Any) -> str:
    assert result.is_error
    return " ".join(getattr(block, "text", "") for block in result.content)


def _write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    _write(root / "README.md", "# Project\nTODO: write docs\n")
    _write(root / "src" / "app.py", "def main():\n    return 'todo later'\n")
    _write(root / "src" / "pkg" / "util.py", "VALUE = 1\n# TODO refactor\n")
    _write(root / "docs" / "guide.md", "Guide\nNothing to do here\n")
    _write(root / ".git" / "config", "TODO hidden in git\n")
    _write(root / ".git" / "hooks.py", "print('TODO')\n")
    _write(root / "node_modules" / "lib" / "index.py", "TODO in dependency\n")
    _write(root / "pkg" / "__pycache__" / "mod.py", "TODO cached\n")
    return root


# ---- glob ---------------------------------------------------------------------


async def test_glob_name_pattern_matches_anywhere_below_the_root(tree: Path) -> None:
    data = await _data("glob", {"pattern": "*.py", "path": str(tree)})
    assert data["root"] == str(tree)
    assert data["pattern"] == "*.py"
    assert data["paths"] == [str(tree / "src" / "app.py"), str(tree / "src" / "pkg" / "util.py")]
    assert data["truncated"] is False


async def test_glob_relative_path_pattern_matches_relative_paths(tree: Path) -> None:
    under_src = await _data("glob", {"pattern": "src/**/*.py", "path": str(tree)})
    assert under_src["paths"] == [str(tree / "src" / "pkg" / "util.py")]
    any_depth = await _data("glob", {"pattern": "**/*.md", "path": str(tree)})
    assert any_depth["paths"] == [str(tree / "README.md"), str(tree / "docs" / "guide.md")]
    direct = await _data("glob", {"pattern": "docs/*.md", "path": str(tree)})
    assert direct["paths"] == [str(tree / "docs" / "guide.md")]


async def test_glob_skips_version_control_and_dependency_folders(tree: Path) -> None:
    data = await _data("glob", {"pattern": "*", "path": str(tree)})
    for path in data["paths"]:
        parts = Path(path).relative_to(tree).parts
        assert not {".git", "node_modules", "__pycache__"} & set(parts), path
    assert str(tree / "README.md") in data["paths"]
    # Skipped folders stay skipped even when the pattern names them.
    named = await _data("glob", {"pattern": "node_modules/**/*.py", "path": str(tree)})
    assert named["paths"] == []


async def test_glob_caps_results_at_500_and_flags_truncation(tmp_path: Path) -> None:
    root = tmp_path / "many"
    for index in range(501):
        _write(root / f"file_{index:04d}.txt")
    capped = await _data("glob", {"pattern": "*.txt", "path": str(root)})
    assert len(capped["paths"]) == 500
    assert capped["truncated"] is True
    assert capped["paths"] == sorted(capped["paths"])
    (root / "file_0500.txt").unlink()
    exact = await _data("glob", {"pattern": "*.txt", "path": str(root)})
    assert len(exact["paths"]) == 500
    assert exact["truncated"] is False


async def test_glob_rejects_a_file_as_the_search_root(tree: Path) -> None:
    result = await _call("glob", {"pattern": "*", "path": str(tree / "README.md")})
    assert "path is not a folder" in _error_text(result)


# ---- grep ---------------------------------------------------------------------


async def test_grep_returns_matching_lines_with_numbers(tree: Path) -> None:
    data = await _data("grep", {"pattern": "TODO", "path": str(tree)})
    assert data["matches"] == [
        {"path": str(tree / "README.md"), "line": 2, "text": "TODO: write docs"},
        {"path": str(tree / "src" / "pkg" / "util.py"), "line": 2, "text": "# TODO refactor"},
    ]
    assert data["skipped_files"] == 0
    assert data["truncated"] is False


async def test_grep_glob_filter_limits_the_files_read(tree: Path) -> None:
    data = await _data("grep", {"pattern": "TODO", "path": str(tree), "glob": "*.py"})
    assert [row["path"] for row in data["matches"]] == [str(tree / "src" / "pkg" / "util.py")]
    by_path = await _data("grep", {"pattern": "o", "path": str(tree), "glob": "docs/*.md"})
    assert {row["path"] for row in by_path["matches"]} == {str(tree / "docs" / "guide.md")}


async def test_grep_ignore_case(tree: Path) -> None:
    sensitive = await _data("grep", {"pattern": "todo", "path": str(tree)})
    assert [row["path"] for row in sensitive["matches"]] == [str(tree / "src" / "app.py")]
    insensitive = await _data("grep", {"pattern": "todo", "path": str(tree), "ignore_case": True})
    assert [row["path"] for row in insensitive["matches"]] == [
        str(tree / "README.md"),
        str(tree / "src" / "app.py"),
        str(tree / "src" / "pkg" / "util.py"),
    ]


async def test_grep_skips_and_counts_binary_and_oversize_files(tmp_path: Path) -> None:
    root = tmp_path / "mixed"
    _write(root / "text.txt", "needle in text\n")
    (root / "image.bin").write_bytes(b"needle\0\x01\x02binary")
    (root / "huge.log").write_bytes(b"needle\n" + b"x" * (2 * 1024 * 1024))
    data = await _data("grep", {"pattern": "needle", "path": str(root)})
    assert data["matches"] == [
        {"path": str(root / "text.txt"), "line": 1, "text": "needle in text"}
    ]
    assert data["skipped_files"] == 2


async def test_grep_accepts_a_single_file(tree: Path) -> None:
    target = tree / "src" / "app.py"
    data = await _data("grep", {"pattern": "return", "path": str(target)})
    assert data["matches"] == [{"path": str(target), "line": 2, "text": "    return 'todo later'"}]


async def test_grep_caps_matches_at_200_and_flags_truncation(tmp_path: Path) -> None:
    root = tmp_path / "lines"
    _write(root / "many.txt", "".join(f"hit {index}\n" for index in range(201)))
    data = await _data("grep", {"pattern": "hit", "path": str(root)})
    assert len(data["matches"]) == 200
    assert data["truncated"] is True


async def test_grep_invalid_regex_is_a_clear_error(tree: Path) -> None:
    result = await _call("grep", {"pattern": "([unclosed", "path": str(tree)})
    assert "invalid regular expression" in _error_text(result)


# ---- file policy --------------------------------------------------------------


@pytest.mark.parametrize("tool", ["glob", "grep"])
async def test_search_outside_allowed_roots_is_refused(tmp_path: Path, tool: str) -> None:
    from tests._config_layer import set_config

    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    _write(allowed / "ok.txt", "secret\n")
    _write(outside / "private.txt", "secret\n")
    # allowed_roots is file-layer (file > env): narrow it to ``allowed`` only.
    set_config("tools.file_policy.allowed_roots", [str(allowed)])

    result = await _call(
        tool, {"pattern": "*" if tool == "glob" else "secret", "path": str(outside)}
    )
    text = _error_text(result)
    assert "outside allowed roots" in text
    assert "private.txt" not in text
    inside = await _data(
        tool, {"pattern": "*" if tool == "glob" else "secret", "path": str(allowed)}
    )
    assert inside["root"] == str(allowed)


def test_glob_and_grep_results_present_one_row_per_hit() -> None:
    from clio_agent.tools.tool_presentation import MCP_PRESENTATION_ADAPTERS

    glob = MCP_PRESENTATION_ADAPTERS["fs_glob"].present(
        {"pattern": "*.py"},
        {"structuredContent": {"paths": ["a.py", "b.py"], "truncated": False}},
        None,
    )
    assert glob["summary"] == "2 files for *.py"
    grep = MCP_PRESENTATION_ADAPTERS["fs_grep"].present(
        {"pattern": "TODO"},
        {
            "structuredContent": {
                "matches": [{"path": "a.py", "line": 3, "text": "# TODO"}],
                "truncated": True,
            }
        },
        None,
    )
    assert grep["summary"] == "1 match for TODO (more not shown)"
    assert grep["blocks"][0]["text"] == "a.py:3: # TODO"
