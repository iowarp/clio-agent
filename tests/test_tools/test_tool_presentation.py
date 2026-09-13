from __future__ import annotations

import os

from clio_agent.tools.tool_presentation import (
    capture_tool_presentation,
    enrich_tool_observer_result,
)


def test_file_write_diff_is_added_only_to_observer_result(tmp_path) -> None:
    target = tmp_path / "result.txt"
    target.write_text("FIRST", encoding="utf-8")
    os.environ["CLIO_ALLOWED_ROOTS"] = str(tmp_path)
    try:
        args = {"filepath": str(target), "new_content": "SECOND"}
        snapshot = capture_tool_presentation("fs_apply_edit_write", args)
    finally:
        os.environ.pop("CLIO_ALLOWED_ROOTS", None)

    original = {
        "content": [],
        "structuredContent": {"path": str(target), "written_path": str(target), "ok": True},
    }
    enriched = enrich_tool_observer_result(original, args, snapshot)

    assert original["structuredContent"].get("unified_diff") is None
    assert enriched["structuredContent"]["unified_diff"].endswith("-FIRST\n+SECOND")
    assert enriched["structuredContent"]["lines_added"] == 1
    assert enriched["structuredContent"]["lines_removed"] == 1


def test_non_file_tool_has_no_presentation_snapshot() -> None:
    assert capture_tool_presentation("shell_bash", {"command": "echo hi"}) is None
