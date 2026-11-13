"""Ranged reads, ripgrep search modes and glob on real files, and their presenters."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from clio_agent.tools.file_policy import FilePolicyError
from clio_agent.tools.fs_read import read_line_chars, read_text_range
from clio_agent.tools.fs_search_rg import glob_files, search_files
from clio_agent.tools.gateway import build_gateway
from clio_agent.tools.mcp_result_text import result_to_text
from clio_agent.tools.tool_presentation import present_mcp_result
from tests._config_layer import set_config


@pytest.fixture
def approved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "approved"
    root.mkdir()
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(root))
    set_config("tools.file_policy.allowed_roots", [str(root)])
    set_config("tools.file_policy.allow_symlinks", False)
    return root


def _observer(result) -> dict:  # noqa: ANN001 - a fastmcp ToolResult
    return {
        "content": [{"type": "text", "text": block.text} for block in result.content],
        "structuredContent": result.structured_content,
    }


def _text(result) -> str:  # noqa: ANN001
    return result.content[0].text


def test_a_read_returns_numbered_lines_with_its_range(approved: Path) -> None:
    source = approved / "notes.py"
    source.write_text("\n".join(f"line {i}" for i in range(1, 11)) + "\n", encoding="utf-8")
    result = read_text_range(str(source), offset=4, limit=3)
    lines = _text(result).split("\n")
    assert lines[0] == f"{source}  lines 4-6 of 10  (71 bytes); continue with offset=7"
    assert lines[1:] == ["     4\tline 4", "     5\tline 5", "     6\tline 6"]
    assert result.structured_content == {
        "path": str(source),
        "size_bytes": 71,
        "start_line": 4,
        "end_line": 6,
        "total_lines": 10,
        "truncated": True,
        "next_offset": 7,
    }
    view = present_mcp_result("fs_read_file", {}, _observer(result))
    code = next(block for block in view["blocks"] if block["id"] == "file")
    assert code["text"] == "line 4\nline 5\nline 6" and code["start_line"] == 4
    assert view["summary"] == "Lines 4–6 of 10"
    assert any(block["id"] == "continues" for block in view["blocks"])


def test_a_default_read_sizes_itself_to_the_result_bound(approved: Path) -> None:
    set_config("tools.unknown_window_result_tokens", 500)  # 2,000 characters
    source = approved / "big.txt"
    source.write_text("\n".join(f"row {i:05d} {'x' * 40}" for i in range(5_000)), encoding="utf-8")
    result = read_text_range(str(source))
    text = _text(result)
    assert len(text) <= 2_000
    structured = result.structured_content
    assert structured["truncated"] and structured["total_lines"] == 5_000
    header = text.split("\n")[0]
    assert f"continue with offset={structured['next_offset']}" in header
    assert header.endswith("(one result holds about 500 tokens)")  # why it stopped
    following = read_text_range(str(source), offset=structured["next_offset"])
    assert _text(following).split("\n")[1].lstrip().startswith(str(structured["next_offset"]))


def test_a_read_returns_the_lines_asked_for_up_to_the_window_share(
    approved: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a 200k window a read holds 25,000 tokens: 2,000 lines of 50 characters come
    back whole (the old fixed bound returned about 230). SABOTAGE: size ``budget`` in
    ``read_text_range`` from a fixed 12,000 and the read stops early."""
    from clio_agent.gact.runtime import context_tokens

    monkeypatch.setattr("clio_agent.gact.context.active_react_context_window", lambda: 200_000)
    monkeypatch.setattr(context_tokens, "_last_prompt_tokens", lambda: 0)
    monkeypatch.setattr(context_tokens, "_estimate_text_tokens", lambda text: len(text) // 4)
    source = approved / "code.py"
    source.write_text("\n".join(f"value_{i:05d} = compute({i}, 'x' * 9)" for i in range(3_000)))
    whole = read_text_range(str(source), limit=2_000).structured_content
    assert (whole["start_line"], whole["end_line"], whole["next_offset"]) == (1, 2_000, 2_001)
    header = _text(read_text_range(str(source), limit=2_000)).split("\n")[0]
    assert header.endswith("continue with offset=2001")  # stopped by limit, not the room
    monkeypatch.setattr("clio_agent.gact.context.active_react_context_window", lambda: 16_000)
    small = read_text_range(str(source), limit=2_000)  # 2,000 tokens: about 140 lines
    assert 100 < small.structured_content["end_line"] < 200
    assert "(one result holds about 2,000 tokens)" in _text(small).split("\n")[0]


def test_long_lines_are_cut_and_empty_and_past_end_reads_say_so(approved: Path) -> None:
    long = approved / "long.txt"
    long.write_text("a" * (read_line_chars() + 50), encoding="utf-8")
    assert "line cut" in _text(read_text_range(str(long)))
    empty = approved / "empty.txt"
    empty.write_text("", encoding="utf-8")
    assert "empty file" in _text(read_text_range(str(empty)))
    short = approved / "short.txt"
    short.write_text("one\ntwo\n", encoding="utf-8")
    assert "past the end; the file has 2 lines" in _text(read_text_range(str(short), offset=9))


def test_the_read_window_and_line_length_are_configured(approved: Path) -> None:
    set_config("fs.read_lines", 2)
    set_config("fs.read_line_chars", 5)
    source = approved / "rows.txt"
    source.write_text("abcdefgh\nsecond\nthird\n", encoding="utf-8")
    result = read_text_range(str(source))
    assert result.structured_content["end_line"] == 2
    assert "abcde… [line cut: 8 characters]" in _text(result)


def test_binary_files_name_the_reader(approved: Path) -> None:
    image = approved / "plot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
    with pytest.raises(ToolError, match="view_image"):
        read_text_range(str(image))


def test_the_model_sees_the_text_not_the_structured_summary(approved: Path) -> None:
    source = approved / "a.txt"
    source.write_text("hello\n", encoding="utf-8")
    text = result_to_text(read_text_range(str(source)))
    assert text.endswith("1\thello") and "total_lines" not in text


def _tree(root: Path) -> None:
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("def callback():\n    return 1\n", encoding="utf-8")
    (root / "src" / "b.py").write_text("callback()\ncallback()\n", encoding="utf-8")
    (root / "README.md").write_text("Mentions callback once.\n", encoding="utf-8")
    old = time.time() - 3_600
    os.utime(root / "README.md", (old, old))


def test_files_mode_lists_matching_files_newest_first_with_counts(approved: Path) -> None:
    _tree(approved)
    result = search_files(str(approved), "callback")
    files = result.structured_content["files"]
    assert {f["path"]: f["matches"] for f in files} == {
        str(approved / "src" / "a.py"): 1,
        str(approved / "src" / "b.py"): 2,
        str(approved / "README.md"): 1,
    }
    assert files[-1]["path"] == str(approved / "README.md")  # the oldest is last
    view = present_mcp_result("fs_search_text", {"path": str(approved)}, _observer(result))
    assert view["summary"] == "3 files"
    assert any(block.get("detail") == "2 matches" for block in view["blocks"])


def test_content_mode_returns_numbered_lines_with_context_and_glob(approved: Path) -> None:
    _tree(approved)
    result = search_files(
        str(approved), "return", mode="content", file_glob="*.py", context_lines=1
    )
    [match] = result.structured_content["matches"]
    assert match["line"] == 2 and match["path"].endswith("a.py")
    assert match["context"] == [{"path": match["path"], "line": 1, "text": "def callback():"}]
    view = present_mcp_result("fs_search_text", {"path": str(approved)}, _observer(result))
    link = next(block for block in view["blocks"] if block["id"] == "match-0")
    code = next(block for block in view["blocks"] if block["id"] == "context-0")
    assert link["line"] == 2 and code["start_line"] == 1


def test_a_content_search_continues_where_the_room_stopped_it(approved: Path) -> None:
    """Matches past one result are not dropped: the header names the offset, and reading
    on returns every match exactly once. SABOTAGE: ignore ``offset`` in
    ``_content_result`` and the second page repeats the first."""
    set_config("tools.unknown_window_result_tokens", 500)  # 2,000 characters
    (approved / "log.txt").write_text(
        "\n".join(f"step {i:04d} needle {'x' * 30}" for i in range(300)), encoding="utf-8"
    )
    seen: list[int] = []
    offset, pages = 0, 0
    while offset is not None:
        result = search_files(str(approved), "needle", mode="content", limit=500, offset=offset)
        structured = result.structured_content
        assert len(_text(result)) <= 2_000
        seen += [match["line"] for match in structured["matches"]]
        if structured["next_offset"] is not None:
            assert f"continue with offset={structured['next_offset']}" in _text(result)
        offset, pages = structured["next_offset"], pages + 1
    assert seen == list(range(1, 301)) and pages > 3
    assert structured["total_matches"] == 300 and not structured["truncated"]


def test_a_files_search_continues_past_its_limit(approved: Path) -> None:
    _tree(approved)
    first = search_files(str(approved), "callback", limit=2)
    assert first.structured_content["next_offset"] == 2
    assert "continue with offset=2" in _text(first)
    rest = search_files(str(approved), "callback", limit=2, offset=2)
    names = [f["path"] for f in first.structured_content["files"]]
    names += [f["path"] for f in rest.structured_content["files"]]
    assert len(set(names)) == 3 and rest.structured_content["next_offset"] is None
    view = present_mcp_result("fs_search_text", {"offset": 0}, _observer(first))
    assert any("more follow from offset 2" in b.get("text", "") for b in view["blocks"])


def test_count_mode_regex_case_and_truncation(approved: Path) -> None:
    _tree(approved)
    assert "4 matches in 3 files" in _text(search_files(str(approved), "callback", mode="count"))
    regex = search_files(str(approved), r"call\w+\(\)$", regex=True, mode="count")
    assert regex.structured_content["total_matches"] == 2
    folded = search_files(str(approved), "CALLBACK", case_sensitive=False, mode="count")
    assert folded.structured_content["total_matches"] == 4
    limited = search_files(str(approved), "callback", limit=1)
    assert limited.structured_content["truncated"] and "+2 more files" in _text(limited)


def test_glob_finds_paths_across_folders_newest_first(approved: Path) -> None:
    _tree(approved)
    result = glob_files("**/*.py", str(approved))
    assert sorted(result.structured_content["files"]) == sorted(
        [str(approved / "src" / "a.py"), str(approved / "src" / "b.py")]
    )
    view = present_mcp_result("fs_glob", {"path": str(approved)}, _observer(result))
    assert view["action"] == "Find files" and view["summary"] == "2 files match **/*.py"


def test_search_and_glob_stay_inside_the_allowed_roots(approved: Path) -> None:
    outside = approved.parent / "private"
    outside.mkdir()
    (outside / "secret.txt").write_text("needle", encoding="utf-8")
    with pytest.raises(FilePolicyError):
        search_files(str(outside), "needle")
    with pytest.raises(FilePolicyError):
        glob_files("*.txt", str(approved / ".." / "private"))
    link = approved / "linked"
    link.symlink_to(outside, target_is_directory=True)
    assert (
        search_files(str(approved), "needle", mode="count").structured_content["total_matches"] == 0
    )  # links are not followed


@pytest.mark.asyncio
async def test_the_gateway_offers_ranged_read_search_modes_and_glob(approved: Path) -> None:
    _tree(approved)
    async with Client(build_gateway({}), cache=False) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        assert {"fs_glob", "fs_search_text", "fs_read_file"} <= set(tools)
        assert tools["fs_search_text"].input_schema["properties"]["mode"]["default"] == "files"
        assert "offset" in tools["fs_read_file"].input_schema["properties"]
        read = await client.call_tool(
            "fs_read_file", {"path": str(approved / "src" / "a.py"), "limit": 1}
        )
        found = await client.call_tool("fs_glob", {"pattern": "*.md", "path": str(approved)})
    assert read.structured_content["end_line"] == 1
    assert found.structured_content["files"] == [str(approved / "README.md")]


def test_an_empty_result_says_ignored_and_hidden_files_were_skipped(tmp_path, monkeypatch) -> None:
    """A search started above a folder its repository ignores finds nothing there; the
    result says what was skipped, so 'no match' is not read as 'does not exist'."""
    import subprocess

    from clio_agent.tools.fs_search_rg import SKIPPED_NOTE, glob_files, search_files

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("runs/\n", encoding="utf-8")
    workspace = tmp_path / "runs" / "task"
    workspace.mkdir(parents=True)
    (workspace / "registry.py").write_text("DRIFT_PPM = 48\n", encoding="utf-8")
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    for mode in ("files", "content", "count"):
        result = search_files(str(tmp_path), "DRIFT_PPM", mode=mode)
        assert SKIPPED_NOTE in result.content[0].text, mode
    assert SKIPPED_NOTE in glob_files("**/registry.py", str(tmp_path)).content[0].text
    # The folder itself is searchable when named, and a hit carries no note.
    found = search_files(str(workspace), "DRIFT_PPM", mode="files")
    assert found.structured_content["total_files"] == 1
    assert SKIPPED_NOTE not in found.content[0].text


def test_a_search_has_no_clock_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default is 0: a search runs until it finishes or the turn is stopped."""
    from clio_agent.tools import fs_search_rg

    monkeypatch.delenv("CLIO_FS_SEARCH_TIMEOUT_S", raising=False)
    assert fs_search_rg.search_timeout_s() is None
    monkeypatch.setenv("CLIO_FS_SEARCH_TIMEOUT_S", "-1")
    with pytest.raises(ValueError, match="no limit"):
        fs_search_rg.search_timeout_s()


def test_stopping_the_turn_ends_a_running_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop kills the search process and says so; it is not a partial or empty result.

    A real child process stands in for a slow ripgrep. SABOTAGE: drop the cancellation
    check -> the 30 s child runs on -> the elapsed bound fails -> red.
    """
    import sys

    from clio_agent.runtime.cancellation import cancellation_checker
    from clio_agent.tools import fs_search_rg

    monkeypatch.delenv("CLIO_FS_SEARCH_TIMEOUT_S", raising=False)
    monkeypatch.setattr(fs_search_rg, "ripgrep_executable", lambda: sys.executable)
    stop_at = time.monotonic() + 0.5
    started = time.monotonic()
    with (
        cancellation_checker(lambda: time.monotonic() > stop_at),
        pytest.raises(ToolError, match="fs_search_cancelled"),
    ):
        fs_search_rg._run(["-c", "import time; time.sleep(30)"], tmp_path)
    assert time.monotonic() - started < 15


def test_an_operator_limit_returns_what_was_found_marked_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    from clio_agent.tools import fs_search_rg

    monkeypatch.setenv("CLIO_FS_SEARCH_TIMEOUT_S", "0.5")
    monkeypatch.setattr(fs_search_rg, "ripgrep_executable", lambda: sys.executable)
    script = "import time; print('a.py:1:hit', flush=True); time.sleep(30)"
    lines, partial = fs_search_rg._run(["-c", script], tmp_path)
    assert partial and lines == ["a.py:1:hit"]
