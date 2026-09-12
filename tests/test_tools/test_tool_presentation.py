from __future__ import annotations

import os
from pathlib import Path

import pytest

from clio_agent.tools.tool_presentation import (
    capture_tool_presentation,
    enrich_tool_observer_result,
    present_mcp_result,
    starting_presentation,
)


@pytest.mark.parametrize(
    "name,action",
    [
        ("fs_read_file", "Read"),
        ("fs_apply_edit_write", "Write"),
        ("fs_propose_edit", "Propose edit"),
    ],
)
def test_declared_file_header_excludes_payload(name: str, action: str) -> None:
    original = {
        "structuredContent": {
            "path": "D:/workspace/evidence.txt",
            "content": "private payload",
            "size_bytes": 15,
        }
    }
    view = present_mcp_result(name, {"filepath": "D:/workspace/evidence.txt"}, original)
    assert view["action"] == action
    assert view["subject"] == "file-link"
    subject = next(block for block in view["blocks"] if block["id"] == view["subject"])
    assert subject["label"] == "evidence.txt"
    assert subject["uri"] == "D:/workspace/evidence.txt"
    assert "private payload" not in str(subject)
    assert "presentation" not in original
    running = starting_presentation(
        name, {"filepath": "D:/workspace/evidence.txt", "new_content": "payload"}
    )
    assert running is not None and running["action"] == action
    assert running["subject"] == "file-link"
    assert [block["type"] for block in running["blocks"]] == ["link"]


def test_declared_search_and_running_shell_headers() -> None:
    view = present_mcp_result(
        "web_search", {"query": "HDF5 SWMR"}, {"structuredContent": {"results": []}}
    )
    assert view["action"] == "Search"
    assert view["blocks"][0]["text"] == "HDF5 SWMR"
    assert view["subject"] == "subject"
    running = starting_presentation("shell_bash", {"command": "echo exact"})
    assert running is not None and running["action"] == "Run"
    assert running["blocks"][0]["command"] == "echo exact"


def test_shell_presentation_uses_the_process_exit_status() -> None:
    succeeded = present_mcp_result(
        "shell_bash",
        {"command": "check"},
        {"structuredContent": {"stdout": "ok", "stderr": "", "exit_code": 0}},
    )
    failed = present_mcp_result(
        "shell_bash",
        {"command": "check"},
        {
            "structuredContent": {
                "stdout": "",
                "stderr": "failed",
                "exit_code": 1,
            }
        },
    )

    assert "status" not in succeeded
    assert succeeded["blocks"][0]["exit_code"] == 0
    assert failed["status"] == "failed"
    assert failed["blocks"][0]["exit_code"] == 1


def test_missing_declared_file_subject_does_not_invent_a_header() -> None:
    assert starting_presentation("fs_read_file", {"path": "README.md"}) is None
    original = {"ok": True, "text": "readme contents"}
    assert present_mcp_result("fs_read_file", {"path": "README.md"}, original) == {
        "summary": "",
        "blocks": [],
    }
    assert original == {"ok": True, "text": "readme contents"}


def test_unknown_mcp_with_ok_false_structured_content_surfaces_failed() -> None:
    """#1333 (de0b7dd7): ``structured_tool_result_error`` widened typed-error
    qualification to the ``{"ok": false}`` MCP convention (alongside an explicit
    ``error`` field or a ``status in {error, failed, failure}``) -- an unknown
    third-party MCP's structured ``ok: false`` result now surfaces as FAILED
    telemetry with one semantic-error block, never a silently empty/neutral
    presentation for a result that already signals its own failure."""

    assert present_mcp_result("third_party", {}, {"structuredContent": {"ok": False}}) == {
        "status": "failed",
        "summary": "",
        "blocks": [
            {
                "id": "semantic-error",
                "type": "text",
                "media_type": "",
                "text": "ok=false",
                "label": "Request failed",
                "language": "",
                "uri": "",
                "command": "",
                "timed_out": False,
                "status": "",
                "detail": "",
                "items": [],
                "action_label": "",
                "severity": "error",
            }
        ],
    }


def test_standard_presentation_marks_typed_tool_errors_failed() -> None:
    view = present_mcp_result(
        "raise_alert_card",
        {},
        {
            "error": "alert_card_no_parent",
            "message": "This session has no parent session.",
        },
    )

    assert view["status"] == "failed"
    assert view["summary"] == ""
    assert view["blocks"] == [
        {
            "id": "semantic-error",
            "type": "text",
            "media_type": "",
            "text": "This session has no parent session.",
            "label": "Request failed",
            "language": "",
            "uri": "",
            "command": "",
            "timed_out": False,
            "status": "",
            "detail": "",
            "items": [],
            "action_label": "",
            "severity": "error",
        }
    ]


def test_standard_presentation_marks_content_lane_tool_errors_failed() -> None:
    view = present_mcp_result(
        "raise_alert_card",
        {},
        {
            "content": [
                {
                    "type": "text",
                    "text": (
                        '{"error":"alert_card_no_parent",'
                        '"message":"This session has no parent session."}'
                    ),
                }
            ]
        },
    )

    assert view["status"] == "failed"
    assert view["blocks"][0]["label"] == "Request failed"
    assert view["blocks"][0]["text"] == "This session has no parent session."
    assert view["blocks"][0]["severity"] == "error"
    assert "alert_card_no_parent" not in str(view["blocks"])


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
    target.write_text("SECOND", encoding="utf-8")
    enriched = enrich_tool_observer_result(original, args, snapshot)

    assert original["structuredContent"].get("unified_diff") is None
    assert enriched["structuredContent"] == original["structuredContent"]
    assert enriched["presentation"]["blocks"][1]["text"].endswith(
        "-FIRST\n\\ No newline at end of file\n+SECOND\n\\ No newline at end of file\n"
    )


@pytest.mark.parametrize("before,after", [("same", "same\n"), ("same\n", "same")])
def test_file_diff_exposes_trailing_newline_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, before: str, after: str
) -> None:
    target = tmp_path / "newline.txt"
    target.write_text(before, encoding="utf-8")
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    args = {"filepath": str(target), "new_content": after}
    snapshot = capture_tool_presentation("fs_apply_edit_write", args)
    target.write_text(after, encoding="utf-8")
    original = {"content": [], "structuredContent": {"path": str(target), "ok": True}}
    diff = enrich_tool_observer_result(original, args, snapshot)["presentation"]["blocks"][1][
        "text"
    ]
    assert "-same\n" in diff and "+same\n" in diff
    assert diff.count("\\ No newline at end of file") == 1
    assert "presentation" not in original


def test_non_file_tool_has_no_presentation_snapshot() -> None:
    assert capture_tool_presentation("shell_bash", {"command": "echo hi"}) is None


def test_diff_preview_uses_one_context_line_not_a_ten_line_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "context.txt"
    before = "\n".join(f"line {index}" for index in range(20)) + "\n"
    target.write_text(before, encoding="utf-8")
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    args = {"filepath": str(target)}
    snapshot = capture_tool_presentation("fs_apply_edit_write", args)
    target.write_text(before.replace("line 10\n", "edited\n"), encoding="utf-8")
    view = enrich_tool_observer_result(
        {"structuredContent": {"path": str(target), "ok": True}}, args, snapshot
    )["presentation"]
    diff = view["blocks"][1]["text"]
    assert len(diff.splitlines()) == 7
    assert " line 9\n-line 10\n+edited\n line 11\n" in diff
    assert view["summary"] == ""
