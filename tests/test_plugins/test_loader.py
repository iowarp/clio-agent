"""Agent Plugins 1.0.0 manifest, mcp.json and folder-convention loading."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clio_agent.plugins.loader import PluginLoadError, load_plugin
from clio_agent.plugins.manifest import PLUGIN_SCHEMA
from tests.test_plugins.helpers import fragment, plugin, tools_module, write


def test_unknown_manifest_field_is_reported_and_ignored(tmp_path: Path) -> None:
    root = plugin(tmp_path, "demo", manifest_extra={"dependencies": ["x"]})
    record = load_plugin(root, trusted=True)
    assert record.valid
    assert [d.code for d in record.diagnostics] == ["manifest_unknown_field"]


@pytest.mark.parametrize(
    ("manifest", "message"),
    [
        (
            {"$schema": "https://agent-plugins.org/schemas/0.9.0/plugin.schema.json", "name": "x"},
            "unsupported",
        ),
        ({"$schema": PLUGIN_SCHEMA, "name": "Bad-Name"}, "must be 1-64"),
        ({"$schema": PLUGIN_SCHEMA, "name": "has--double"}, "must be 1-64"),
        ({"$schema": PLUGIN_SCHEMA, "name": "x", "author": {"name": "a", "x": "y"}}, "author"),
        ({"$schema": PLUGIN_SCHEMA, "name": "x", "version": 2}, "version must be a string"),
    ],
)
def test_fatal_manifest_errors(tmp_path: Path, manifest: dict, message: str) -> None:
    write(tmp_path / "p" / "plugin.json", json.dumps(manifest))
    with pytest.raises(PluginLoadError, match=message):
        load_plugin(tmp_path / "p", trusted=True)


def test_mcp_json_rules(tmp_path: Path) -> None:
    root = plugin(
        tmp_path,
        "servers",
        servers={
            "good": {"type": "stdio", "command": "clio-kit", "args": ["mcp-server", "hdf5"]},
            "remote": {"type": "streamable-http", "url": "https://example.org/mcp"},
            "shell-string": {"type": "stdio", "command": "uv run thing"},
            "escape": {"type": "stdio", "command": "../bin/x"},
            "insecure": {"type": "streamable-http", "url": "http://example.org/mcp"},
            "reserved": {"type": "stdio", "command": "x", "env": {"PLUGIN_ROOT": "/"}},
        },
    )
    record = load_plugin(root, trusted=True)
    assert sorted(record.mcp_servers) == ["good", "remote"]
    assert sorted(d.code for d in record.diagnostics) == ["mcp_server_invalid"] * 4


def test_python_parts_load_only_when_trusted(tmp_path: Path) -> None:
    root = plugin(
        tmp_path,
        "manufacturing",
        files={
            "ai.iowarp.clio/plugin.py": (
                "from clio_agent.sdk import Plugin, Requires\n\n"
                "class Manufacturing(Plugin):\n    id = 'manufacturing'\n    version = '1.0.0'\n"
                "    requires = ['shell']\n    model = Requires(context_tokens=64000)\n"
            ),
            "ai.iowarp.clio/tools/plato.py": (
                "from clio_agent.sdk import tool\n\n@tool(read_only=True)\n"
                "def plato_optimize(mesh: str) -> str:\n    'Optimize a mesh.'\n    return mesh\n"
            ),
            "ai.iowarp.clio/subagents/reviewer.py": (
                "from clio_agent.sdk import PromptFile, Subagent\n\n"
                "reviewer = Subagent('process-reviewer', prompt=PromptFile('prompt/reviewer.md'), plugins=['pandas'])\n"
            ),
            "ai.iowarp.clio/prompt/reviewer.md": "Review the process.\n",
            "ai.iowarp.clio/prompt/frame.md": fragment(
                "manufacturing", "frame", "domain", "Topology work."
            ),
            "skills/abaqus/SKILL.md": "---\nname: abaqus\ndescription: Abaqus input files.\n---\nBody.\n",
            "skills/Bad_Name/SKILL.md": "---\nname: Bad_Name\ndescription: x\n---\n",
        },
    )
    trusted = load_plugin(root, trusted=True)
    assert trusted.requires == ("shell",)
    assert trusted.model == {"context_tokens": 64000}
    assert [(t.name, t.kind, t.read_only) for t in trusted.tools] == [
        ("plato_optimize", "python", True)
    ]
    assert trusted.subagents[0].prompt == "Review the process."
    assert trusted.subagents[0].plugins == ("pandas",)
    assert [f.id for f in trusted.fragments] == ["manufacturing.frame"]
    assert [s.id for s in trusted.skills] == ["abaqus"]
    assert "skill_invalid" in [d.code for d in trusted.diagnostics]
    untrusted = load_plugin(root, trusted=False)
    assert untrusted.tools == () and untrusted.subagents == ()
    assert "plugin_untrusted" in [d.code for d in untrusted.diagnostics]
    assert untrusted.executable_digest == trusted.executable_digest


def test_missing_prompt_file_is_a_clear_error(tmp_path: Path) -> None:
    root = plugin(
        tmp_path,
        "broken",
        files={
            "ai.iowarp.clio/subagents/s.py": (
                "from clio_agent.sdk import PromptFile, Subagent\n\nx = Subagent('helper', prompt=PromptFile('prompt/missing.md'))\n"
            )
        },
    )
    record = load_plugin(root, trusted=True)
    assert not record.valid
    assert record.errors[0].code == "prompt_file_missing"
    assert "prompt file ai.iowarp.clio/prompt/missing.md does not exist" in record.errors[0].message


def test_fragment_rules(tmp_path: Path) -> None:
    root = plugin(
        tmp_path,
        "shell",
        files={
            "ai.iowarp.clio/prompt/a.md": fragment("other", "x", "capabilities", "Wrong prefix."),
            "ai.iowarp.clio/prompt/b.md": fragment("shell", "y", "guidelines", "Not core."),
            "ai.iowarp.clio/prompt/c.md": fragment(
                "shell", "z", "capabilities", "Fine.", "{tools: [shell_bash]}"
            ),
        },
    )
    record = load_plugin(root, trusted=True)
    assert [f.id for f in record.fragments] == ["shell.z"]
    assert record.fragments[0].when_tools == ("shell_bash",)
    assert sorted(d.code for d in record.errors) == ["fragment_invalid", "fragment_invalid"]


def test_pack_with_content_is_invalid(tmp_path: Path) -> None:
    root = plugin(
        tmp_path,
        "bad-pack",
        ext={"kind": "pack", "members": ["a"]},
        files={"ai.iowarp.clio/tools/t.py": tools_module("shell_bash")},
    )
    record = load_plugin(root, trusted=True)
    assert record.errors[0].code == "pack_invalid"


def test_text_change_moves_only_the_text_digest(tmp_path: Path) -> None:
    root = plugin(
        tmp_path, "p", files={"skills/s/SKILL.md": "---\nname: s\ndescription: d\n---\nv1\n"}
    )
    first = load_plugin(root, trusted=True)
    write(root / "skills/s/SKILL.md", "---\nname: s\ndescription: d\n---\nv2\n")
    second = load_plugin(root, trusted=True)
    assert first.executable_digest == second.executable_digest
    assert first.text_digest != second.text_digest
    write(root / "ai.iowarp.clio/tools/t.py", tools_module("shell_bash"))
    third = load_plugin(root, trusted=True)
    assert third.executable_digest != second.executable_digest
