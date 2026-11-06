"""The Claude Code importer maps plugins into the same internal record."""

from __future__ import annotations

import json
from pathlib import Path

from clio_agent.plugins.claude_code import import_claude_plugin, map_matcher
from clio_agent.plugins.marketplace import read_claude_marketplace
from tests.test_plugins.helpers import write


def _claude_plugin(
    root: Path, name: str, manifest: dict, files: dict[str, str] | None = None
) -> Path:
    base = root / name
    write(base / ".claude-plugin" / "plugin.json", json.dumps({"name": name, **manifest}))
    for relative, text in (files or {}).items():
        write(base / relative, text)
    return base


def test_dependency_only_plugin_becomes_a_pack(tmp_path: Path) -> None:
    root = _claude_plugin(
        tmp_path, "clio-hpc", {"version": "1.0.2", "dependencies": ["clio-slurm", "clio-spack"]}
    )
    record = import_claude_plugin(root)
    assert (record.kind, record.members, record.requires) == (
        "pack",
        ("clio-slurm", "clio-spack"),
        (),
    )


def test_dependencies_on_content_become_requirements_and_hooks_map(tmp_path: Path) -> None:
    hooks = {
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Write|Edit",
                    "hooks": [
                        {
                            "type": "command",
                            "command": 'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/v.py" hook',
                            "timeout": 30,
                        }
                    ],
                }
            ],
            "Notification": [{"hooks": [{"type": "command", "command": "true"}]}],
        }
    }
    root = _claude_plugin(
        tmp_path,
        "clio-dataset-report",
        {"version": "1.0.0", "dependencies": ["clio-hdf5"]},
        {
            "hooks/hooks.json": json.dumps(hooks),
            "hooks/codex.json": "{}",
            "scripts/v.py": "print('ok')\n",
            "skills/dataset-report/SKILL.md": "---\nname: dataset-report\ndescription: Report.\n---\nDo it.\n",
            ".mcp.json": json.dumps(
                {"clio-hdf5": {"command": "clio-kit", "args": ["mcp-server", "hdf5"]}}
            ),
        },
    )
    record = import_claude_plugin(root)
    assert record.kind == "plugin" and record.requires == ("clio-hdf5",)
    assert record.mcp_servers == {
        "clio-hdf5": {"type": "stdio", "command": "clio-kit", "args": ["mcp-server", "hdf5"]}
    }
    [hook] = record.hooks
    assert hook.event == "PostToolUse" and hook.kind == "command"
    assert hook.command == "python3"
    assert len(hook.args) == 2 and Path(hook.args[0]) == root / "scripts/v.py"
    assert hook.args[1] == "hook"
    assert hook.tools == "fs_write_file|fs_edit_file"
    assert hook.timeout_ms == 30000 and record.format == "claude-code"
    assert [d.code for d in record.diagnostics] == ["hook_event_unmapped"]
    # The hook script is executable content even though it is not under hooks/.
    write(root / "scripts/v.py", "print('changed')\n")
    assert import_claude_plugin(root).executable_digest != record.executable_digest


def test_agents_map_tools_and_model_preference(tmp_path: Path) -> None:
    root = _claude_plugin(
        tmp_path,
        "clio-agents",
        {"version": "1.0.2"},
        {
            "agents/reviewer.md": "---\nname: reviewer\ndescription: Review.\ntools: Read, Glob, Grep\nmodel: claude-sonnet-5\n---\nReview it.\n",
            "agents/notebook.md": "---\nname: notebook\ntools: NotebookEdit\n---\nx\n",
        },
    )
    record = import_claude_plugin(root)
    notebook, reviewer = record.subagents
    assert reviewer.tools == ("fs_read_file", "fs_glob", "fs_search_text")
    assert reviewer.plugins == ("clio-agents", "workspace")
    assert reviewer.prefers == {"model": "claude-sonnet-5"}
    # An unmapped tool leaves only itself out, with a typed warning (PC4); core's tools stay.
    assert notebook.id == "notebook" and notebook.tools == ()
    assert not record.errors
    [unmapped] = [d for d in record.diagnostics if d.code == "tool_unmapped"]
    assert unmapped.severity == "warning" and "NotebookEdit" in unmapped.message
    # Glob maps onto fs_glob whole; Grep's multiline mode has no counterpart, and the
    # import says so with a typed warning, never silently.
    [lossy] = [d for d in record.diagnostics if d.code == "tool_mapped_lossy"]
    assert lossy.severity == "warning" and "reviewer" in lossy.message
    assert "Grep" in lossy.message and "Glob" not in lossy.message


def test_lossy_hook_matcher_is_reported(tmp_path: Path) -> None:
    hooks = {
        "hooks": {
            "PreToolUse": [{"matcher": "Grep", "hooks": [{"type": "command", "command": "true"}]}]
        }
    }
    root = _claude_plugin(
        tmp_path, "search-guard", {"version": "1.0.0"}, {"hooks/hooks.json": json.dumps(hooks)}
    )
    record = import_claude_plugin(root)
    [hook] = record.hooks
    assert hook.tools == "fs_search_text"
    assert [d.code for d in record.diagnostics] == ["tool_mapped_lossy"]


def test_matcher_mapping() -> None:
    assert map_matcher("*") == ("", [])
    assert map_matcher("Bash|Frobnicate") == ("shell_bash", ["Frobnicate"])


def test_marketplace_sources_and_federation(tmp_path: Path) -> None:
    _claude_plugin(tmp_path / "plugins", "local-one", {"version": "1.0.0"})
    write(
        tmp_path / ".claude-plugin" / "marketplace.json",
        json.dumps(
            {
                "name": "kit",
                "plugins": [
                    {"name": "local-one", "source": "./plugins/local-one"},
                    {
                        "name": "tdd",
                        "source": {
                            "source": "git-subdir",
                            "url": "https://x/y.git",
                            "path": "a/b",
                            "sha": "abc123",
                        },
                    },
                    {"name": "floating", "source": {"source": "github", "repo": "o/r"}},
                ],
            }
        ),
    )
    write(
        tmp_path / ".claude-plugin" / "federated-marketplaces.json",
        json.dumps(
            {
                "marketplaces": [
                    {
                        "name": "coder",
                        "source": {"source": "github", "repo": "o/coder", "sha": "def456"},
                    }
                ]
            }
        ),
    )
    write(tmp_path / ".clio-kit" / "catalogue.json", "not read")
    contents = read_claude_marketplace(tmp_path)
    assert [p.id for p in contents.plugins] == ["local-one"]
    assert [(p.url, p.commit, p.path) for p in contents.git_plugins] == [
        ("https://x/y.git", "abc123", "a/b")
    ]
    assert [(p.name, p.url, p.commit) for p in contents.federated] == [
        ("coder", "https://github.com/o/coder.git", "def456")
    ]
    assert [d.code for d in contents.diagnostics] == ["plugin_source_unpinned"]


def test_a_marketplace_entry_names_exactly_the_skills_its_plugin_brings(tmp_path: Path) -> None:
    """anthropics/skills: several plugins share one folder, each listing its skills."""

    from clio_agent.plugins.store import PluginStore

    root = tmp_path / "skills-market"
    for name, description in (("xlsx", "Spreadsheets."), ("pdf", "PDFs."), ("long", "x" * 1100)):
        write(
            root / "skills" / name / "SKILL.md",
            f"---\nname: {name}\ndescription: {description}\n---\nBody.\n",
        )
    entries = [
        {
            "name": "document-skills",
            "source": "./",
            "strict": False,
            "skills": ["./skills/xlsx", "./skills/pdf"],
        },
        {
            "name": "long-skill",
            "source": "./",
            "strict": False,
            "skills": ["./skills/long", "./skills/gone"],
        },
    ]
    write(
        root / ".claude-plugin" / "marketplace.json", json.dumps({"name": "m", "plugins": entries})
    )
    store = PluginStore(tmp_path / "store")
    store.install(str(root))
    by_id = {r.id: r for r in store.records() if r.marketplace == "m"}
    assert [s.id for s in by_id["document-skills"].skills] == ["xlsx", "pdf"]
    assert [s.id for s in by_id["long-skill"].skills] == ["long"]
    assert {d.code for d in by_id["long-skill"].diagnostics} == {
        "skill_description_long",
        "skill_missing",
    }


def test_a_skill_a_folder_of_skills_and_one_claude_plugin_each_install(tmp_path: Path) -> None:
    """Not only marketplaces: what people share as skills or single plugins installs too."""

    from clio_agent.plugins.store import PluginStore

    write(
        tmp_path / "one-skill" / "SKILL.md", "---\nname: one-skill\ndescription: One.\n---\nBody.\n"
    )
    write(
        tmp_path / "bundle" / "skills" / "a" / "SKILL.md",
        "---\nname: a\ndescription: A.\n---\nBody.\n",
    )
    write(tmp_path / "solo" / ".claude-plugin" / "plugin.json", '{"name": "solo"}')
    write(
        tmp_path / "solo" / "skills" / "b" / "SKILL.md",
        "---\nname: b\ndescription: B.\n---\nBody.\n",
    )
    store = PluginStore(tmp_path / "store")
    for source in ("one-skill", "bundle", "solo"):
        store.install(str(tmp_path / source))
    skills = {
        r.id: [s.id for s in r.skills]
        for r in store.records()
        if r.marketplace in ("one-skill", "bundle", "solo")
    }
    assert skills == {"one-skill": ["one-skill"], "bundle": ["a"], "solo": ["b"]}


def test_a_package_with_a_library_manifest_and_a_claude_form_loads_the_claude_form(
    tmp_path: Path,
) -> None:
    """A root Clio Coder library ``plugin.json`` beside ``.claude-plugin/`` (PC1)."""

    from clio_agent.plugins.marketplace import detect_format, read_marketplace

    root = tmp_path / "wtfp"
    library = {"name": "wtfp", "extensions": {"ai.iowarp.clio": {"manifestVersion": 1}}}
    write(root / "plugin.json", json.dumps(library))
    write(root / ".claude-plugin" / "plugin.json", '{"name": "wtfp"}')
    write(root / "commands" / "new-paper.md", "Start a paper.")
    write(root / "skills" / "plan" / "SKILL.md", "---\nname: plan\ndescription: Plan.\n---\nB")
    assert detect_format(root) == "claude-code-plugin"
    (record,) = read_marketplace(root, trusted=False).plugins
    assert record.format == "claude-code"
    assert [c.id for c in record.commands] == ["new-paper"]
    assert "plugin_dialect_skipped" in {d.code for d in record.diagnostics}
    # A native plugin.json (no library dialect) still wins.
    write(root / "plugin.json", '{"name": "wtfp", "version": "1.0.0"}')
    assert detect_format(root) == "plugin"


def test_an_agents_skills_are_preloaded_into_its_prompt(tmp_path: Path) -> None:
    """``skills:`` injects each skill's text, as Claude Code does; an unknown one is named."""

    root = _claude_plugin(
        tmp_path,
        "paper",
        {"version": "1.0.0"},
        {
            "skills/write-section/SKILL.md": "---\nname: write-section\ndescription: W.\n---\nDraft from the plan.\n",
            "agents/writer.md": (
                "---\nname: writer\ntools: Read\nskills:\n  - paper:write-section\n  - other:thing\n"
                "---\nYou write sections.\n"
            ),
        },
    )
    record = import_claude_plugin(root)
    (writer,) = record.subagents
    assert writer.prompt.startswith("You write sections.\n\n# Skill: write-section\n")
    assert "Draft from the plan." in writer.prompt
    [missing] = [d for d in record.diagnostics if d.code == "skill_unmapped"]
    assert missing.severity == "warning" and "other:thing" in missing.message


def test_unmapped_parts_are_named_and_injections_are_read_like_any_plugin(tmp_path: Path) -> None:
    """PC5: ``output-styles/`` is reported; ``ai.iowarp.clio/injections`` loads for either format."""

    from clio_agent.injections.check import injection_diagnostics
    from clio_agent.injections.plugin_set import _read

    root = _claude_plugin(
        tmp_path,
        "paper",
        {"version": "1.0.0", "lspServers": {"tex": {}}},
        {
            "output-styles/terse.md": "Be terse.",
            "ai.iowarp.clio/injections/remind.md": (
                "---\nevent: turn_start\n---\nKeep sections in paper/.\n"
            ),
        },
    )
    record = import_claude_plugin(root)
    unmapped = sorted(d.message for d in record.diagnostics if d.code == "component_unmapped")
    assert unmapped == [
        "lspServers (language servers) has no CLIO equivalent and does not load",
        "output-styles/ (output styles) has no CLIO equivalent and does not load",
    ]
    assert injection_diagnostics(record) == []
    (injection,) = _read(record.root, record.id, record.text_digest)
    assert injection.event == "turn_start" and "Keep sections" in injection.text
