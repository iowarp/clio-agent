"""Every built-in file tool names its target ``path`` (qualification T8b/T8c, ledger 21).

The showcase drives the real FS server with the arguments a model sends, then presents
each recorded result through the shared presentation contract: small and large reads,
an empty file, a created, an overwritten and an edited file, a refused edit, a policy
refusal and the declared start row. It asserts what a reader sees (file link, content,
diff, outcome) and that presentation leaves the model's result unchanged.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from clio_agent.gact.folder_access import requested_folders
from clio_agent.gact.hooks.claude_code_hooks import claude_envelope
from clio_agent.gact.hooks.wire import HookEnvelope
from clio_agent.gact.presentation_observer import completed_presentation
from clio_agent.gact.provenance.normalization import _TOOL_INPUT_FIELDS
from clio_agent.gact.runtime.permission_policies import _permission_path_from_args
from clio_agent.tools.gateway import _list_tools_sync
from clio_agent.tools.mcp_results import call_tool_result_to_observer
from clio_agent.tools.result_errors import structured_tool_result_error
from clio_agent.tools.servers.fs_server import fs_server
from clio_agent.tools.tool_presentation import present_mcp_result, starting_presentation
from tests._config_layer import set_config

FS_TOOLS = ("list_directory", "search_text", "glob", "read_file", "write_file", "edit_file")
LONG_NAME = "station-quality-summary-with-a-deliberately-long-unbroken-name.md"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    # The config file layer shadows the environment (tests/_config_layer.py), so the
    # allowed roots are set there: otherwise a launch whose roots include the checkout
    # (which holds the pytest runtime) makes "elsewhere" allowed.
    set_config("tools.file_policy.allowed_roots", [str(tmp_path)])
    return tmp_path


async def _call(tool: str, args: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """One real call, completed the way the GACT observer completes it.

    The MCP boundary attaches the adapter's presentation to the observer copy; the
    observer reads the recorded failure and finishes the presentation from both.
    """
    async with Client(fs_server, cache=False) as client:
        raw = await client.call_tool(tool, args, raise_on_error=False)
    observed = call_tool_result_to_observer(raw)
    before = copy.deepcopy(observed)
    name = f"fs_{tool}"
    enriched = {**observed, "presentation": present_mcp_result(name, args, observed)}
    error = structured_tool_result_error(observed)
    if error is None and observed.get("isError"):
        error = "tool_result_is_error"
    result, view = completed_presentation(
        name, args, enriched, observed.get("structuredContent"), "", error=error
    )
    assert observed == before == result, "presentation must not change the model's result"
    return observed, view


def _block(view: dict[str, Any], kind: str) -> dict[str, Any]:
    return next(block for block in view["blocks"] if block["type"] == kind)


def test_every_fs_tool_names_its_target_path() -> None:
    listed = {tool.name: tool.input_schema for tool in _list_tools_sync(fs_server)}
    for name in FS_TOOLS:
        properties = listed[name]["properties"]
        assert "path" in properties, name
        assert "filepath" not in properties, name
    for name in ("read_file", "write_file", "edit_file"):
        assert "path" in listed[name]["required"], name


async def test_the_old_argument_name_is_refused_and_names_the_new_one(root: Path) -> None:
    """The T8b failure, inverted: ``path=`` writes; ``filepath=`` is a validation error."""
    observed, _ = await _call("write_file", {"path": str(root / "out/1.txt"), "content": "one"})
    assert observed["structuredContent"]["created"] is True
    assert (root / "out/1.txt").read_text(encoding="utf-8") == "one"

    async with Client(fs_server, cache=False) as client:
        raw = await client.call_tool(
            "write_file",
            {"filepath": str(root / "out/2.txt"), "content": "two"},
            raise_on_error=False,
        )
    assert raw.is_error is True
    text = raw.content[0].text
    assert "path" in text and "filepath" in text
    assert not (root / "out/2.txt").exists()


async def test_showcase_reads_small_large_and_empty_files(root: Path) -> None:
    source = root / "src" / "gateway.py"
    source.parent.mkdir()
    source.write_text("def route(request):\n    return request.path\n", encoding="utf-8")
    _, view = await _call("read_file", {"path": str(source)})
    link = _block(view, "link")
    assert (link["label"], link["uri"], link["line"]) == ("gateway.py", str(source), 1)
    code = _block(view, "code")
    assert code["language"] == "python"
    assert code["text"] == "def route(request):\n    return request.path"
    assert view["summary"] == "Lines 1–2 of 2"
    assert view["action"] == "Read"

    large = root / LONG_NAME
    large.write_text(
        "---\ntitle: Station quality\nowner: triage\n---\n"
        + "".join(f"- reading {i:05d}: {'wide value ' * 12}\n" for i in range(5_000)),
        encoding="utf-8",
    )
    observed, view = await _call("read_file", {"path": str(large), "limit": 400})
    assert observed["structuredContent"]["truncated"] is True
    assert _block(view, "link")["label"] == LONG_NAME
    assert "Title: Station quality" in _block(view, "text")["text"]
    assert "- reading 00000:" in _block(view, "markdown")["text"]
    assert view["summary"].startswith("Lines 1–") and view["summary"].endswith("of 5,004")
    following = observed["structuredContent"]["next_offset"]
    assert any(
        f"next read starts at line {following:,}" in block.get("text", "")
        for block in view["blocks"]
    )

    empty = root / "empty.log"
    empty.write_text("", encoding="utf-8")
    _, view = await _call("read_file", {"path": str(empty)})
    assert view["summary"] == "Empty file"
    assert [block["type"] for block in view["blocks"]] == ["link"]


async def test_showcase_writes_and_edits_show_a_truthful_diff(root: Path) -> None:
    target = root / "notes" / "plan.md"
    _, created = await _call("write_file", {"path": str(target), "content": "alpha\nbeta\n"})
    assert _block(created, "link")["label"] == "plan.md"
    assert "+alpha\n+beta" in _block(created, "diff")["text"]
    assert created["action"] == "Write"

    _, overwritten = await _call("write_file", {"path": str(target), "content": "alpha\nB\n"})
    diff = _block(overwritten, "diff")["text"]
    assert "-beta\n+B" in diff and "+alpha" not in diff

    _, edited = await _call(
        "edit_file", {"path": str(target), "old_string": "alpha", "new_string": "Alpha"}
    )
    assert "-alpha\n+Alpha" in _block(edited, "diff")["text"]
    assert edited["action"] == "Edit"
    assert target.read_text(encoding="utf-8") == "Alpha\nB\n"


async def test_showcase_failures_keep_their_reason(root: Path) -> None:
    target = root / "dupes.txt"
    target.write_text("x\nx\n", encoding="utf-8")
    observed, view = await _call(
        "edit_file", {"path": str(target), "old_string": "x", "new_string": "y"}
    )
    assert observed["structuredContent"]["error"]["code"] == "old_string_not_unique"
    assert _block(view, "link")["label"] == "dupes.txt"
    assert target.read_text(encoding="utf-8") == "x\nx\n"

    assert view["status"] == "failed"
    assert [block["type"] for block in view["blocks"]] == ["link", "text"], "no empty diff"
    assert "old_string occurs 2 times" in _block(view, "text")["text"]
    assert _block(view, "text")["severity"] == "error"

    outside = root.parent / "outside.txt"
    outside.write_text("not granted", encoding="utf-8")
    observed, view = await _call("read_file", {"path": str(outside)})
    assert observed["isError"] is True
    assert view["status"] == "failed"
    assert view["subject"] == "file-link"
    assert _block(view, "link")["label"] == "outside.txt"
    assert "outside allowed roots" in _block(view, "text")["text"]
    assert "not granted" not in str(view)


def test_showcase_running_row_names_the_declared_file(root: Path) -> None:
    path = str(root / "data" / LONG_NAME)
    for name, action in (("fs_read_file", "Read"), ("fs_write_file", "Write")):
        view = starting_presentation(name, {"path": path, "content": "payload"})
        assert view is not None
        assert view["action"] == action
        [link] = view["blocks"]
        assert (link["type"], link["target"], link["uri"]) == ("link", "file", path)
        assert link["label"] == LONG_NAME
        assert "payload" not in str(view)


def test_consumers_read_path_from_the_file_tools(root: Path) -> None:
    path = str(root.parent / "elsewhere" / "data.csv")
    assert _permission_path_from_args({"path": path}) == path
    assert requested_folders("fs_read_file", {"path": path}) == [str(root.parent / "elsewhere")]
    for name in ("fs_read_file", "fs_write_file", "fs_edit_file"):
        assert _TOOL_INPUT_FIELDS[name] == ("path",)


@pytest.mark.parametrize(
    ("tool", "claude_name", "key"),
    [
        ("fs_read_file", "Read", "file_path"),
        ("fs_write_file", "Write", "file_path"),
        ("fs_edit_file", "Edit", "file_path"),
        ("fs_glob", "Glob", "path"),
        ("fs_search_text", "Grep", "path"),
    ],
)
def test_claude_code_hooks_see_their_own_path_keys(tool: str, claude_name: str, key: str) -> None:
    """Read/Write/Edit call the file ``file_path``; Glob and Grep call the folder ``path``."""
    body = claude_envelope(
        HookEnvelope(
            hook_event_name="PreToolUse",
            session_id="s",
            cwd="/work",
            tool_name=tool,
            tool_input={"path": "/work/a.py"},
        )
    )
    assert body["tool_name"] == claude_name
    assert body["tool_input"] == {key: "/work/a.py"}
