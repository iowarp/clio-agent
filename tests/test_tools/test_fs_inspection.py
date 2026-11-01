"""Real directory listing and read-policy enforcement (search: test_fs_search_read)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from clio_agent.tools import fs_inspection
from clio_agent.tools.file_policy import FileAccessPolicy, FilePolicyError
from clio_agent.tools.tool_presentation import present_mcp_result
from tests._config_layer import set_config


@pytest.fixture
def approved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Use a real private approved tree without inherited workspace grants."""
    root = tmp_path / "approved"
    root.mkdir()
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(root))
    set_config("tools.file_policy.allowed_roots", [str(root)])
    set_config("tools.file_policy.allow_symlinks", False)
    return root


def test_listing_reports_entry_types_and_bound(approved: Path) -> None:
    (approved / "src").mkdir()
    (approved / "README.md").write_text("hello", encoding="utf-8")
    result = fs_inspection.list_directory(str(approved))
    assert [(e["name"], e["type"]) for e in result["entries"]] == [
        ("README.md", "file"),
        ("src", "directory"),
    ]
    assert not result["truncated"]
    view = present_mcp_result(
        "fs_list_directory", {"path": str(approved)}, {"structuredContent": result}
    )
    assert view["action"] == "List directory"
    assert view["summary"] == "2 entries"
    assert any(block.get("label") == "README.md" for block in view["blocks"])
    limited = fs_inspection.list_directory(str(approved), limit=1)
    assert limited["truncated"] and len(limited["entries"]) == 1
    view = present_mcp_result(
        "fs_list_directory", {"path": str(approved)}, {"structuredContent": limited}
    )
    assert view["summary"].endswith(" (partial listing)")
    assert any("incomplete" in block.get("text", "") for block in view["blocks"])


def test_listing_denies_outside_and_traversal(approved: Path) -> None:
    outside = approved.parent / "private"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    for path in (outside, approved / ".." / "private"):
        with pytest.raises(FilePolicyError):
            fs_inspection.list_directory(str(path))


def test_file_and_directory_validators_keep_distinct_types(approved: Path) -> None:
    file = approved / "file.txt"
    file.write_text("x", encoding="utf-8")
    policy = FileAccessPolicy.from_env()
    with pytest.raises(FilePolicyError) as file_error:
        policy.validate_read(str(approved))
    assert file_error.value.code == "not_a_file"
    with pytest.raises(FilePolicyError) as directory_error:
        policy.validate_read_directory(str(file))
    assert directory_error.value.code == "not_a_directory"


def test_links_are_visible_but_never_listed_into(approved: Path) -> None:
    """Windows junctions work without Developer Mode or symlink privileges."""
    outside = approved.parent / "private"
    outside.mkdir()
    (outside / "secret.txt").write_text("needle", encoding="utf-8")
    link = approved / "linked"
    if sys.platform == "win32":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True
        )
    else:
        link.symlink_to(outside, target_is_directory=True)
    assert fs_inspection.list_directory(str(approved))["entries"][0]["type"] == "link"
    with pytest.raises(FilePolicyError):
        fs_inspection.list_directory(str(link))
