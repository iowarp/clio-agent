"""Tests for the agent's file tools: fs_write_file (Write) and fs_edit_file (Edit)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from clio_agent.tools.catalog import classification_tags, get_tool_entry
from clio_agent.tools.execution import (
    ToolRuntimeHooks,
    create_sync_tool_executor,
    set_tool_runtime_fallback,
)
from clio_agent.tools.file_policy import FilePolicyError
from clio_agent.tools.fs_write import write_text_with_policy
from clio_agent.tools.gateway import _list_tools_sync, _tool_annotations, get_gateway
from clio_agent.tools.servers.fs_server import edit_file, fs_server, write_file


def test_fs_tools_declare_expected_annotations_and_catalog_projects_them() -> None:
    """#1061: each fs built-in declares MCP annotations at its decorator, and the catalog
    read/write tag is a PROJECTION of those declared annotations (single source of truth)."""
    listed = {t.name: t for t in _list_tools_sync(fs_server)}
    expected = {
        "list_directory": (
            "fs_list_directory",
            {"readOnlyHint": True, "openWorldHint": False},
            "read",
        ),
        "search_text": ("fs_search_text", {"readOnlyHint": True, "openWorldHint": False}, "read"),
        "read_file": ("fs_read_file", {"readOnlyHint": True, "openWorldHint": False}, "read"),
        "write_file": (
            "fs_write_file",
            {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False},
            "write",
        ),
        "edit_file": (
            "fs_edit_file",
            {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False},
            "write",
        ),
    }
    for bare_name, (catalog_name, hints, tag) in expected.items():
        annotations = _tool_annotations(listed[bare_name])
        assert annotations is not None, bare_name
        for key, value in hints.items():
            assert annotations[key] is value, (bare_name, key)
        # The catalog tag is DERIVED from the declared annotations — not hand-authored.
        assert classification_tags(annotations) == frozenset({tag})
        entry_tags = get_tool_entry(catalog_name).tags
        assert tag in entry_tags
        if tag == "read":
            assert "write" not in entry_tags
        else:
            assert "read" not in entry_tags


def test_write_file_creates_a_new_file_and_records_the_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    target = tmp_path / "sub" / "new.txt"

    result = write_file(str(target), "hello\n")

    assert result["ok"] is True
    assert result["created"] is True
    assert result["path"] == str(target.resolve())
    assert target.read_text(encoding="utf-8") == "hello\n"
    assert result["sha256_before"] == ""
    assert result["sha256"] == hashlib.sha256(b"hello\n").hexdigest()
    assert result["lines_added"] == 1
    assert result["lines_removed"] == 0
    assert "+hello" in result["unified_diff"]


def test_write_file_overwrites_and_reports_both_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    target = tmp_path / "file.txt"
    target.write_bytes(b"one\n")

    result = write_file(str(target), "two\n")

    assert result["created"] is False
    assert target.read_bytes() == b"two\n"
    assert result["sha256_before"] == hashlib.sha256(b"one\n").hexdigest()
    assert result["sha256"] == hashlib.sha256(b"two\n").hexdigest()
    assert (result["lines_added"], result["lines_removed"]) == (1, 1)
    assert "previous_content" not in result  # Undo's copy is captured by GACT, not echoed


def test_edit_file_replaces_one_exact_occurrence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    target = tmp_path / "code.py"
    target.write_bytes(b"x = 1\ny = 2\n")

    result = edit_file(str(target), "y = 2", "y = 3")

    assert result["ok"] is True
    assert result["created"] is False
    assert result["replacements"] == 1
    assert target.read_bytes() == b"x = 1\ny = 3\n"
    assert "-y = 2" in result["unified_diff"]
    assert "+y = 3" in result["unified_diff"]
    assert result["sha256_before"] == hashlib.sha256(b"x = 1\ny = 2\n").hexdigest()


def test_edit_file_replace_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    target = tmp_path / "code.py"
    target.write_bytes(b"a a a\n")

    result = edit_file(str(target), "a", "b", replace_all=True)

    assert result["replacements"] == 3
    assert target.read_bytes() == b"b b b\n"


@pytest.mark.parametrize(
    "setup,old,code",
    [
        (b"alpha\n", "beta", "old_string_not_found"),
        (b"dup\ndup\n", "dup", "old_string_not_unique"),
        (None, "anything", "file_not_found"),
        (b"same\n", "", "old_string_empty"),
        (b"\xff\xfe\x00", "x", "file_not_text"),
    ],
)
def test_edit_file_refusals_are_typed_and_change_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setup: bytes | None, old: str, code: str
) -> None:
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    target = tmp_path / "target.txt"
    if setup is not None:
        target.write_bytes(setup)

    result = edit_file(str(target), old, "replacement")

    assert result["ok"] is False
    assert result["error"]["code"] == code
    if setup is None:
        assert not target.exists()
    else:
        assert target.read_bytes() == setup
    if code == "old_string_not_unique":
        assert result["error"]["details"]["occurrences"] == 2


@pytest.mark.parametrize(
    "before,after,body",
    [
        (
            "def answer():\n    value = 42\n    return value\n",
            "def answer():\n    value = 43\n    return value\n",
            "@@ -1,3 +1,3 @@\n def answer():\n-    value = 42\n"
            "+    value = 43\n     return value\n",
        ),
        ("a\n\nb\n", "a\n\nc\n", "@@ -1,3 +1,3 @@\n a\n \n-b\n+c\n"),
        (
            "a",
            "b",
            "@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+b\n\\ No newline at end of file\n",
        ),
        ("a", "a\n", "@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+a\n"),
        ("a\n", "a", "@@ -1 +1 @@\n-a\n+a\n\\ No newline at end of file\n"),
    ],
)
def test_write_diff_has_exact_line_spacing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, before: str, after: str, body: str
) -> None:
    """No separator-created blank rows; genuine blanks and EOF changes remain visible."""
    from clio_agent.tools.tool_presentation import present_mcp_result

    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    target = tmp_path / "spacing.py"
    target.write_bytes(before.encode())
    result = write_file(str(target), after)
    expected = "--- a/spacing.py\n+++ b/spacing.py\n" + body
    assert result["unified_diff"] == expected
    assert target.read_bytes() == after.encode()
    view = present_mcp_result(
        "fs_write_file", {"filepath": str(target)}, {"structuredContent": result}
    )
    assert next(block["text"] for block in view["blocks"] if block["type"] == "diff") == expected
    assert result["unified_diff"] == expected


def test_written_bytes_are_verbatim_never_platform_newline_translated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An authored newline reaches disk as ``\\n``, on every platform.

    Live regression (p5run2): a compute expert authored a POSIX shell script
    with ``\\n`` separators, the relay staged the file's BYTES to a Linux
    cluster, and the job failed with ``hostname\\r: not found`` -- ``write_text``
    had translated every ``\\n`` into ``\\r\\n`` under Windows' default
    ``newline=None``. The marker line still printed (a trailing CR is
    invisible), so the corruption was silent.

    This asserts BYTES on purpose. ``Path.read_text`` applies universal-newline
    translation on the way back in, so a ``read_text() == "...\\n"`` assertion
    passes against the very bug it is meant to catch -- which is why the
    existing writer coverage never saw this.
    """

    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    target = tmp_path / "authored.sh"
    body = "#!/bin/sh\nhostname\necho MARKER\n"

    result = write_text_with_policy(str(target), body)

    assert target.read_bytes() == body.encode("utf-8")
    assert b"\r" not in target.read_bytes()
    # The reported hash must describe the bytes a consumer re-hashes.
    assert result["sha256"] == hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert result["size_bytes"] == len(body.encode("utf-8"))

    # An author who genuinely wants CRLF still gets exactly that, unchanged.
    crlf = "line\r\n"
    write_text_with_policy(str(target), crlf)
    assert target.read_bytes() == crlf.encode("utf-8")


def test_write_file_uses_shared_policy_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    target = tmp_path / "shared.txt"

    direct = write_text_with_policy(str(target), "first\n")
    via_tool = write_file(str(target), "second\n")

    assert direct["path"] == via_tool["path"]
    assert via_tool["ok"] is True
    assert target.read_text(encoding="utf-8") == "second\n"


def test_write_tools_reject_outside_allowed_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    # allowed_roots is file-layer (file > env); write the NARROWER root there,
    # overwriting the fixture's ``tmp_path`` list, so ``outside`` is rejected
    # (a bare setenv would be shadowed by the fixture file — #985 residual).
    from tests._config_layer import set_config

    set_config("tools.file_policy.allowed_roots", [str(allowed)])

    with pytest.raises(FilePolicyError) as exc:
        write_file(str(outside / "new.txt"), "nope\n")
    assert exc.value.code == "outside_allowed_roots"
    assert not (outside / "new.txt").exists()

    (outside / "old.txt").write_text("keep\n", encoding="utf-8")
    with pytest.raises(FilePolicyError):
        edit_file(str(outside / "old.txt"), "keep", "nope")
    assert (outside / "old.txt").read_text(encoding="utf-8") == "keep\n"


def test_gateway_write_respects_late_fallback_permission_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    target = tmp_path / "gated.txt"
    executor = create_sync_tool_executor(get_gateway(), timeout=5.0)
    seen: list[str] = []

    try:
        set_tool_runtime_fallback(
            ToolRuntimeHooks(permission_gate=lambda name, _args: seen.append(name) or "allow")
        )
        executor.call_tool(
            "fs_write_file",
            {"filepath": str(target), "content": "allowed\n"},
        )
        assert target.read_text(encoding="utf-8") == "allowed\n"

        set_tool_runtime_fallback(
            ToolRuntimeHooks(permission_gate=lambda name, _args: seen.append(name) or "deny")
        )
        with pytest.raises(PermissionError, match="denied"):
            executor.call_tool(
                "fs_edit_file",
                {"filepath": str(target), "old_string": "allowed", "new_string": "denied"},
            )

        assert target.read_text(encoding="utf-8") == "allowed\n"
        assert seen == ["fs_write_file", "fs_edit_file"]
    finally:
        set_tool_runtime_fallback(ToolRuntimeHooks())
        executor.close()


def test_write_result_declares_written_path_for_seam_a(tmp_path):
    """#1247: a DIRECT fs_write_file call must mint its output. The
    designation vocabulary deliberately ignores bare ``path`` (input echo),
    so the writer declares the SAME value under the recognized
    ``written_path`` key -- without it, direct tool writes never minted and
    live transform records carried zero OUTPUT edges."""
    import os

    from clio_agent.gact.artifacts.designation import result_declared_paths
    from clio_agent.tools.fs_write import write_text_with_policy

    target = tmp_path / "b.csv"
    os.environ["CLIO_ALLOWED_ROOTS"] = str(tmp_path)
    try:
        result = write_text_with_policy(str(target), "header\n20\n40\n")
    finally:
        os.environ.pop("CLIO_ALLOWED_ROOTS", None)

    assert result["written_path"] == result["path"]
    declared = result_declared_paths(result)
    assert declared == {"written_path": str(target)}, (
        "seam (a) must see the written file through the recognized key"
    )
