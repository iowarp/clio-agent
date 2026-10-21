"""Stage 2 resolution: packs, deltas, cascades, collisions, hook order, prompt."""

from __future__ import annotations

from pathlib import Path

import pytest

from clio_agent.plugins.loader import load_plugin
from clio_agent.plugins.resolve import Catalog, Delta, ResolutionError, resolve_agent
from clio_agent.sdk import Agent, Requires
from tests.test_plugins.helpers import NATIVE, fragment, plugin, tools_module


def _core(root: Path) -> Path:
    return plugin(
        root,
        "core",
        files={
            "ai.iowarp.clio/tools/runtime.py": tools_module(
                "query_tasks", "wait_tasks", "ask_user"
            ),
            "ai.iowarp.clio/flows/react.py": (
                "from clio_agent.sdk import Flow\n\nclass React(Flow):\n    id = 'react'\n"
            ),
            "ai.iowarp.clio/prompt/guidelines.md": fragment(
                "core", "guidelines", "guidelines", "Be grounded."
            ),
            "ai.iowarp.clio/prompt/tasks.md": fragment(
                "core", "tasks", "capabilities", "Tasks have handles.", "{tools: [query_tasks]}"
            ),
        },
    )


def _catalog(root: Path, *paths: Path, trusted: bool = True) -> Catalog:
    records = [load_plugin(p, marketplace="test", trusted=True) for p in (_core(root), *paths)]
    return Catalog(
        records=records, trusted=lambda r: trusted or r.id == "core", native_tools=NATIVE
    )


def _spec(*plugins: str, requires: Requires | None = None):
    class Probe(Agent):
        id = "probe"
        version = "1.0.0"
        role = "a test agent"

    Probe.plugins = ["core", *plugins]
    Probe.requires = requires or Requires()
    return Probe.spec()


def test_pack_expands_and_member_removal_keeps_pack(tmp_path: Path) -> None:
    documents = plugin(tmp_path, "documents", ext={"kind": "pack", "members": ["pdf", "xlsx"]})
    pdf = plugin(tmp_path, "pdf", files={"ai.iowarp.clio/tools/t.py": tools_module("view_pdf")})
    xlsx = plugin(
        tmp_path,
        "xlsx",
        files={
            "skills/sheets/SKILL.md": "---\nname: sheets\ndescription: Sheets.\n---\nUse them.\n"
        },
    )
    catalog = _catalog(tmp_path, documents, pdf, xlsx)
    full = resolve_agent(catalog, _spec("documents"), marketplace="test")
    assert full.plugin_ids == ["core", "documents", "pdf", "xlsx"]
    assert {row["id"]: row["provenance"]["label"] for row in full.plugins}[
        "xlsx"
    ] == "pack documents"
    trimmed = resolve_agent(
        catalog, _spec("documents"), marketplace="test", delta=Delta(remove=("xlsx",))
    )
    assert trimmed.plugin_ids == ["core", "documents", "pdf"]
    assert trimmed.removed == [{"id": "xlsx", "reason": "removed", "because": ""}]
    assert "sheets" not in [s["id"] for s in trimmed.skills]
    # Once its last member leaves, the pack has nothing left to group and leaves too.
    emptied = resolve_agent(
        catalog, _spec("documents"), marketplace="test", delta=Delta(remove=("xlsx", "pdf"))
    )
    assert emptied.plugin_ids == ["core"]
    assert {"id": "documents", "reason": "pack_empty", "because": ""} in emptied.removed


def test_removing_required_plugin_cascades(tmp_path: Path) -> None:
    shell = plugin(
        tmp_path, "shell", files={"ai.iowarp.clio/tools/t.py": tools_module("shell_bash")}
    )
    github = plugin(tmp_path, "github", ext={"requires": ["shell"]})
    catalog = _catalog(tmp_path, shell, github)
    resolved = resolve_agent(catalog, _spec("github"), marketplace="test")
    assert resolved.plugin_ids == ["core", "github", "shell"]
    assert (
        next(r for r in resolved.plugins if r["id"] == "shell")["provenance"]["label"]
        == "required by github"
    )
    removed = resolve_agent(
        catalog, _spec("github"), marketplace="test", delta=Delta(remove=("shell",))
    )
    assert removed.plugin_ids == ["core"]
    assert {"id": "github", "reason": "cascade", "because": "shell"} in removed.removed


def test_core_cannot_be_removed(tmp_path: Path) -> None:
    with pytest.raises(ResolutionError, match="core_not_removable"):
        resolve_agent(
            _catalog(tmp_path), _spec(), marketplace="test", delta=Delta(remove=("core",))
        )


def test_tool_collision_names_both_plugins(tmp_path: Path) -> None:
    a = plugin(tmp_path, "a", files={"ai.iowarp.clio/tools/t.py": tools_module("shell_bash")})
    b = plugin(tmp_path, "b", files={"ai.iowarp.clio/tools/t.py": tools_module("shell_bash")})
    with pytest.raises(ResolutionError) as caught:
        resolve_agent(_catalog(tmp_path, a, b), _spec("a", "b"), marketplace="test")
    assert "tool 'shell_bash' is exposed by both a and b" in str(caught.value)
    assert caught.value.diagnostics[0].code == "tool_collision"


def test_hooks_run_child_to_parent_and_honor_after(tmp_path: Path) -> None:
    hook_file = "from clio_agent.sdk import hook\n\n@hook('tool_result'{extra})\ndef {name}(ev):\n    return None\n"
    parent = plugin(
        tmp_path,
        "science",
        files={"ai.iowarp.clio/hooks/h.py": hook_file.format(name="audit", extra="")},
    )
    child = plugin(
        tmp_path,
        "pdf",
        files={"ai.iowarp.clio/hooks/h.py": hook_file.format(name="redact", extra="")},
    )
    late = plugin(
        tmp_path,
        "late",
        files={
            "ai.iowarp.clio/hooks/h.py": hook_file.format(
                name="last", extra=", after='science.audit'"
            )
        },
    )

    class Science(Agent):
        id = "science"
        plugins = ["core", "science", "late"]

    class Factorio(Science):
        id = "factorio"
        plugins = Science.plugins + ["pdf"]

    resolved = resolve_agent(
        _catalog(tmp_path, parent, child, late), Factorio.spec(), marketplace="test"
    )
    assert [h["id"] for h in resolved.hooks] == ["pdf.redact", "science.audit", "late.last"]
    assert resolved.hooks[0]["event"] == "PostToolUse"


def test_a_claude_code_plugins_command_hook_resolves_in_its_dialect(tmp_path: Path) -> None:
    import json

    from clio_agent.plugins.claude_code import import_claude_plugin
    from tests.test_plugins.helpers import write

    base = tmp_path / "report"
    write(base / ".claude-plugin" / "plugin.json", json.dumps({"name": "report"}))
    hooks = {
        "PostToolUse": [{"matcher": "Write", "hooks": [{"type": "command", "command": "true"}]}]
    }
    write(base / "hooks" / "hooks.json", json.dumps({"hooks": hooks}))
    native = plugin(
        tmp_path,
        "audit",
        files={
            "ai.iowarp.clio/hooks/h.py": (
                "from clio_agent.sdk import hook\n\n@hook('tool_result')\ndef a(ev):\n    return None\n"
            )
        },
    )
    catalog = _catalog(tmp_path, native)
    catalog.records.append(import_claude_plugin(base))
    resolved = resolve_agent(catalog, _spec("audit", "report"), marketplace="test")
    dialects = {row["plugin"]: row["dialect"] for row in resolved.hooks}
    assert dialects == {"report": "claude-code", "audit": ""}


def test_hook_cycle_is_an_error(tmp_path: Path) -> None:
    hooks = (
        "from clio_agent.sdk import hook\n\n"
        "@hook('tool_call', before='cyc.second')\ndef first(ev):\n    return None\n\n"
        "@hook('tool_call', before='cyc.first')\ndef second(ev):\n    return None\n"
    )
    cyc = plugin(tmp_path, "cyc", files={"ai.iowarp.clio/hooks/h.py": hooks})
    with pytest.raises(ResolutionError) as caught:
        resolve_agent(_catalog(tmp_path, cyc), _spec("cyc"), marketplace="test")
    assert caught.value.diagnostics[0].code == "hook_cycle"
    assert "cyc.first" in str(caught.value) and "cyc.second" in str(caught.value)


def test_composed_prompt_sections_and_attribution(tmp_path: Path) -> None:
    shell = plugin(
        tmp_path,
        "shell",
        files={
            "ai.iowarp.clio/tools/t.py": tools_module("shell_bash"),
            "ai.iowarp.clio/prompt/gh.md": fragment(
                "shell", "prefer-gh", "capabilities", "Prefer gh.", "{tools: [shell]}"
            ),
        },
    )
    science = plugin(
        tmp_path,
        "science",
        files={
            "ai.iowarp.clio/prompt/d.md": fragment(
                "science", "frame", "domain", "Science {{agent.name}}."
            )
        },
    )
    resolved = resolve_agent(
        _catalog(tmp_path, shell, science), _spec("science", "shell"), marketplace="test"
    )
    rows = [
        (f["section"], f["id"], f["plugin"])
        for s in resolved.prompt["sections"]
        for f in s["fragments"]
    ]
    assert rows == [
        ("identity", "probe.identity", "agent:probe"),
        ("guidelines", "core.guidelines", "core"),
        ("capabilities", "core.tasks", "core"),
        ("capabilities", "shell.prefer-gh", "shell"),
        ("domain", "science.frame", "science"),
    ]
    assert resolved.prompt_text.startswith("You are probe, a test agent.\n\nBe grounded.")
    assert "Science probe." in resolved.prompt_text
    without_shell = resolve_agent(
        _catalog(tmp_path, shell, science),
        _spec("science", "shell"),
        marketplace="test",
        delta=Delta(remove=("shell",)),
    )
    assert "Prefer gh." not in without_shell.prompt_text


def test_model_requirement_union_is_attributed(tmp_path: Path) -> None:
    vision = plugin(
        tmp_path, "vision-pack", ext={"model": {"vision": True, "context_tokens": 64000}}
    )
    resolved = resolve_agent(
        _catalog(tmp_path, vision),
        _spec("vision-pack", requires=Requires(context_tokens=128000)),
        marketplace="test",
    )
    assert resolved.model["requires"] == {"vision": True, "context_tokens": 128000}
    assert resolved.model["attribution"]["vision"] == ["vision-pack"]


def test_untrusted_executable_plugin_is_refused(tmp_path: Path) -> None:
    tools = plugin(
        tmp_path, "tools", files={"ai.iowarp.clio/tools/t.py": tools_module("shell_bash")}
    )
    with pytest.raises(ResolutionError, match="plugin_untrusted"):
        resolve_agent(_catalog(tmp_path, tools, trusted=False), _spec("tools"), marketplace="test")


def test_missing_and_version_mismatch(tmp_path: Path) -> None:
    pdf = plugin(tmp_path, "pdf", version="1.4.0")
    with pytest.raises(ResolutionError, match="plugin_not_installed"):
        resolve_agent(_catalog(tmp_path), _spec("absent"), marketplace="test")
    with pytest.raises(ResolutionError, match="needs pdf@\\^2"):
        resolve_agent(_catalog(tmp_path, pdf), _spec("pdf@^2"), marketplace="test")
    assert (
        resolve_agent(_catalog(tmp_path, pdf), _spec("pdf@^1.2"), marketplace="test").plugin_ids[-1]
        == "pdf"
    )


def test_user_delta_add_then_remove(tmp_path: Path) -> None:
    pdf = plugin(tmp_path, "pdf")
    resolved = resolve_agent(
        _catalog(tmp_path, pdf), _spec(), marketplace="test", delta=Delta(add=("pdf",))
    )
    assert next(r for r in resolved.plugins if r["id"] == "pdf")["provenance"]["source"] == "user"
    assert Delta(add=("pdf",)).then(Delta(remove=("pdf",))) == Delta((), ("pdf",))
