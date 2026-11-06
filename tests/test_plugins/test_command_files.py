"""A plugin's command files: read recursively, labelled by subfolder, and checked."""

from __future__ import annotations

from pathlib import Path

from clio_agent.gact.runtime.command_table import file_row, place, render_command
from clio_agent.plugins.claude_code import import_claude_plugin
from clio_agent.plugins.commands import builtin_command_names, command_diagnostics
from clio_agent.plugins.loader import load_plugin
from clio_agent.plugins.store import builtin_records
from tests.test_plugins.helpers import plugin, write


def _codes(found: list) -> list[str]:
    return sorted(d.code for d in found)


def test_native_commands_are_read_recursively_with_their_subfolder_label(tmp_path: Path) -> None:
    root = plugin(
        tmp_path,
        "web",
        files={
            "ai.iowarp.clio/commands/frontend/component.md": "---\ndescription: A component.\n---\nX",
            "ai.iowarp.clio/commands/review.md": "Review.",
        },
    )
    record = load_plugin(root, trusted=True)
    rows = {(c.id, c.label) for c in record.commands}
    assert rows == {("component", "frontend"), ("review", "")}


def test_a_native_include_resolves_against_the_plugin_folder_not_its_clio_folder(
    tmp_path: Path,
) -> None:
    """``${CLAUDE_PLUGIN_ROOT}`` is the plugin folder, for a subfolder command too (DS9)."""

    root = plugin(
        tmp_path,
        "web",
        files={
            "templates/component.md": "Use the house component template.",
            "ai.iowarp.clio/commands/frontend/component.md": (
                "Build $1.\n@${CLAUDE_PLUGIN_ROOT}/templates/component.md\n"
            ),
        },
    )
    record = load_plugin(root, trusted=True)
    (command,) = record.commands
    row = file_row(
        Path(command.path), label=command.label, source="plugin", plugin="web", root=record.root
    )
    assert "Use the house component template." in row["prompt_template"]
    assert "not inlined" not in row["prompt_template"]
    assert render_command(row["prompt_template"], "Button").startswith("Build Button.")


def test_claude_code_commands_are_read_recursively(tmp_path: Path) -> None:
    root = tmp_path / "paper"
    write(root / ".claude-plugin" / "plugin.json", '{"name": "paper"}')
    write(root / "commands" / "draft" / "section.md", "Draft $1.")
    record = import_claude_plugin(root)
    assert [(c.id, c.label) for c in record.commands] == [("section", "draft")]


def test_core_ships_the_builtins_with_handlers() -> None:
    core = next(r for r in builtin_records() if r.id == "core")
    assert {c.id: c.handler for c in core.commands} == {
        "cache-stats": "cache-stats",
        "clear": "clear",
        "cron": "cron",
        "dump-trace": "dump-trace",
        "goal": "goal",
        "loop": "loop",
    }
    assert builtin_command_names() >= {"goal", "schedule"}
    assert command_diagnostics("core", core.commands, [], builtin=True) == []


def test_check_rejects_handlers_reserved_names_and_command_skill_clashes(tmp_path: Path) -> None:
    root = plugin(
        tmp_path,
        "mine",
        files={
            "ai.iowarp.clio/commands/goal.md": "Mine.",
            "ai.iowarp.clio/commands/run.md": "---\nhandler: clear\n---\n",
            "ai.iowarp.clio/commands/study.md": "Study.",
            "ai.iowarp.clio/commands/sub/study.md": "Study again.",
            "skills/study/SKILL.md": "---\nname: study\ndescription: Study.\n---\nBody.",
            "skills/schedule/SKILL.md": "---\nname: schedule\ndescription: Schedule.\n---\nBody.",
        },
    )
    record = load_plugin(root, trusted=True)
    assert record.valid, record.diagnostics  # one bad file never stops a plugin loading
    found = command_diagnostics(
        record.id, record.commands, (s.id for s in record.skills), builtin=False
    )
    assert _codes(found) == [
        "command_duplicate",
        "command_handler_not_builtin",
        "command_name_reserved",
        "command_name_reserved",
        "command_skill_clash",
        "command_skill_clash",
    ]


def _row(name: str, *, kind: str = "command", source: str = "plugin", plugin: str = "") -> dict:
    qualified = f"/{plugin or {'clio_workspace': 'project', 'claude_workspace': 'project'}.get(source, 'user')}:{name}"
    return {
        "kind": kind,
        "id": f"/{name}",
        "name": f"/{name}",
        "qualified_id": qualified,
        "plugin": plugin,
        "source": source,
        "aliases": [],
        "status": "available",
        "enabled": True,
        "disabled_reason": "",
        "disabled_detail": "",
        "path": f"{source}/{plugin}/{name}.md",
    }


def test_place_decides_reserved_shadowed_and_ambiguous_names() -> None:
    builtin = _row("goal", kind="builtin", source="builtin", plugin="core")
    builtin["aliases"] = ["/target"]
    rows = [
        builtin,
        _row("hello", source="clio_workspace"),
        _row("hello", source="claude_workspace"),
        _row("target", source="clio_user"),
        _row("notes", plugin="a"),
        _row("notes", plugin="b"),
        _row("solo", plugin="a"),
    ]
    table = place(rows)
    clio, claude, alias_clash = rows[1], rows[2], rows[3]
    assert clio["enabled"] and table.lookup("/hello") is clio
    assert claude["disabled_reason"] == "command_shadowed"
    assert claude["shadowed_by"] == clio["path"]
    assert alias_clash["disabled_reason"] == "command_name_reserved"
    assert table.lookup("/target") is builtin
    assert table.ambiguous == {"/notes": ["/a:notes", "/b:notes"]}
    assert table.lookup("/notes") is None
    assert table.lookup("/b:notes") is rows[5]
    assert table.lookup("/solo") is rows[6] and table.lookup("/a:solo") is rows[6]
    assert table.lookup("/core:goal") is builtin


def test_positional_arguments_keep_quoted_words_whole() -> None:
    assert render_command("$1 then $2; all: $ARGUMENTS; none: [$3]", '"a b" c') == (
        'a b then c; all: "a b" c; none: []'
    )
    assert render_command("", "just text") == "just text"
    # An unbalanced quote still splits on spaces rather than failing.
    assert render_command("$1|$2", "it's fine") == "it's|fine"


def test_a_server_name_the_gateway_refuses_is_a_load_error(tmp_path: Path) -> None:
    """``_`` separates a server's tool prefix, so ``fixture_docs`` would never mount."""

    from clio_agent.plugins.manifest import validate_server

    assert "no '_'" in validate_server(tmp_path, "fixture_docs", {"type": "stdio", "command": "x"})
    assert validate_server(tmp_path, "fixture-docs", {"type": "stdio", "command": "x"}) == ""
