"""A Claude Code plugin's commands and subagents reach the model with their files resolved."""

from __future__ import annotations

from pathlib import Path

import pytest

from clio_agent.gact.catalog import _load_command_files_from_disk
from clio_agent.plugins.claude_code import import_claude_plugin
from clio_agent.plugins.command_text import MAX_INCLUDE_BYTES, expand_plugin_text
from tests.test_plugins.helpers import write


def _plugin(root: Path) -> Path:
    write(root / ".claude-plugin" / "plugin.json", '{"name": "paper"}')
    write(root / "project" / "README.md", "Records live under project/.")
    write(
        root / "commands" / "new-paper.md",
        "---\ndescription: Start a paper.\n---\n# Start\n\n"
        "@${CLAUDE_PLUGIN_ROOT}/project/README.md\n"
        "@${CLAUDE_PLUGIN_ROOT}/missing.md\n"
        "@/etc/hostname\n"
        "Use ${CLAUDE_PLUGIN_ROOT}/templates/outline.md for $ARGUMENTS.\n",
    )
    write(
        root / "agents" / "writer.md",
        "---\nname: writer\ndescription: Writes.\n---\nRead @${CLAUDE_PLUGIN_ROOT}/x.md first.\n",
    )
    return root.resolve()


def test_an_include_inside_the_plugin_is_inlined_and_others_say_why_not(tmp_path: Path) -> None:
    root = _plugin(tmp_path / "paper")
    text = (root / "commands" / "new-paper.md").read_text()
    out = expand_plugin_text(text, root)
    included = root / "project" / "README.md"
    assert f'<file path="{included}">\nRecords live under project/.\n</file>' in out
    assert "missing.md\n(not inlined: no such file in the plugin)" in out
    assert "/etc/hostname\n(not inlined: outside the plugin folder)" in out
    assert f"Use {root}/templates/outline.md for $ARGUMENTS." in out
    assert "${CLAUDE_PLUGIN_ROOT}" not in out


def test_a_large_file_is_left_for_the_model_to_read(tmp_path: Path) -> None:
    write(tmp_path / "big.md", "x" * (MAX_INCLUDE_BYTES + 1))
    out = expand_plugin_text("@${PLUGIN_ROOT}/big.md", tmp_path)
    assert "(not inlined: larger than 64 KiB; read the file)" in out


def test_plugin_commands_and_subagents_are_expanded_where_they_load(tmp_path: Path) -> None:
    root = _plugin(tmp_path / "paper")
    rows = _load_command_files_from_disk(
        home=tmp_path / "home",
        cwd=tmp_path / "cwd",
        extra_roots=[(root / "commands", "plugin", {"plugin": "paper", "command_scope": "plugin"})],
    )
    (row,) = [r for r in rows if r["id"].endswith("new-paper")]
    assert "Records live under project/." in row["prompt_template"]
    record = import_claude_plugin(root)
    assert record.subagents[0].prompt == f"Read @{root}/x.md first."


def test_installed_plugin_files_are_readable_but_not_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.tools.file_policy import FileAccessPolicy, FilePolicyError

    store = tmp_path / "store"
    monkeypatch.setenv("CLIO_PLUGIN_STORE", str(store))
    target = write(store / "snapshots" / "paper" / "abc" / "templates" / "outline.md", "# Outline")
    policy = FileAccessPolicy.from_mapping({"CLIO_ALLOWED_ROOTS": str(tmp_path / "work")})
    assert policy.validate_read(str(target)) == target.resolve()
    with pytest.raises(FilePolicyError):
        policy.validate_write(str(target))
