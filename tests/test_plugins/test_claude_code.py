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
    assert hook.command == "python3" and hook.args == (str(root / "scripts/v.py"), "hook")
    assert hook.tools == "fs_propose_edit|fs_apply_edit_write"
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
    [reviewer] = record.subagents
    assert reviewer.tools == ("fs_read_file", "fs_glob", "fs_grep")
    assert reviewer.plugins == ("clio-agents", "workspace")
    assert reviewer.prefers == {"model": "claude-sonnet-5"}
    [error] = record.errors
    assert error.code == "tool_unmapped" and "NotebookEdit" in error.message


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
