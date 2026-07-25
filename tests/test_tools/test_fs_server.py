"""Tests for filesystem edit tools."""

from __future__ import annotations

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
from clio_agent.tools.servers.fs_server import apply_edit_write, fs_server, propose_edit


def test_fs_tools_declare_expected_annotations_and_catalog_projects_them() -> None:
    """#1061: each fs built-in declares MCP annotations at its decorator, and the catalog
    read/write tag is a PROJECTION of those declared annotations (single source of truth)."""
    listed = {t.name: t for t in _list_tools_sync(fs_server)}
    expected = {
        "read_file": ("fs_read_file", {"readOnlyHint": True, "openWorldHint": False}, "read"),
        "propose_edit": ("fs_propose_edit", {"readOnlyHint": True, "openWorldHint": False}, "read"),
        "apply_edit_write": (
            "fs_apply_edit_write",
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
        # propose_edit stages a diff without touching disk -> read-only, NOT a write.
        if tag == "read":
            assert "write" not in entry_tags
        else:
            assert "read" not in entry_tags


def test_propose_edit_allows_new_file_under_write_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    target = tmp_path / "new.txt"

    result = propose_edit(str(target), "hello\n")

    assert result["path"] == str(target.resolve())
    assert result["lines_added"] == 1
    assert result["lines_removed"] == 0
    assert result["new_content"] == "hello\n"
    assert "hello" in result["unified_diff"]
    assert not target.exists()


def test_apply_edit_write_allows_new_file_under_write_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    target = tmp_path / "new.txt"

    result = apply_edit_write(str(target), "hello\n")

    assert result["ok"] is True
    assert result["path"] == str(target.resolve())
    assert target.read_text(encoding="utf-8") == "hello\n"


def test_apply_edit_write_uses_shared_policy_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    target = tmp_path / "shared.txt"

    direct = write_text_with_policy(str(target), "first\n")
    via_tool = apply_edit_write(str(target), "second\n")

    assert direct["path"] == via_tool["path"]
    assert via_tool["ok"] is True
    assert target.read_text(encoding="utf-8") == "second\n"


def test_apply_edit_write_rejects_outside_allowed_roots(
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
        apply_edit_write(str(outside / "new.txt"), "nope\n")

    assert exc.value.code == "outside_allowed_roots"
    assert not (outside / "new.txt").exists()


def test_gateway_apply_edit_write_respects_late_fallback_permission_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    target = tmp_path / "gated.txt"
    executor = create_sync_tool_executor(get_gateway(), timeout=5.0, setup_timeout=5.0)
    seen: list[str] = []

    try:
        set_tool_runtime_fallback(
            ToolRuntimeHooks(permission_gate=lambda name, _args: seen.append(name) or "allow")
        )
        executor.call_tool(
            "fs_apply_edit_write",
            {"filepath": str(target), "new_content": "allowed\n"},
        )
        assert target.read_text(encoding="utf-8") == "allowed\n"

        set_tool_runtime_fallback(
            ToolRuntimeHooks(permission_gate=lambda name, _args: seen.append(name) or "deny")
        )
        with pytest.raises(PermissionError, match="denied"):
            executor.call_tool(
                "fs_apply_edit_write",
                {"filepath": str(target), "new_content": "denied\n"},
            )

        assert target.read_text(encoding="utf-8") == "allowed\n"
        assert seen == ["fs_apply_edit_write", "fs_apply_edit_write"]
    finally:
        set_tool_runtime_fallback(ToolRuntimeHooks())
        executor.close()
