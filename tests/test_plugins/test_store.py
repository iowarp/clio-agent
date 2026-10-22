"""Install, trust, update and user agents through the store and service."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clio_agent.plugins.check import check
from clio_agent.plugins.resolve import Delta, ResolutionError
from clio_agent.plugins.service import PluginService
from clio_agent.plugins.store import PluginStore, StoreError, builtin_records
from tests.test_plugins.helpers import NATIVE, fragment, plugin, tools_module, write

AGENTS = """
from clio_agent.sdk import Agent


class Standard(Agent):
    id = "standard"
    version = "{version}"
    role = "a general research assistant"
    plugins = Agent.plugins + ["documents", "shell"]
"""


def _native() -> frozenset[str]:
    core = next(r for r in builtin_records() if r.id == "core")
    return NATIVE | {t.name for t in core.tools}


def _market(root: Path, *, version: str = "1.0.0") -> Path:
    write(root / "ai.iowarp.clio" / "marketplace.json", json.dumps({"name": "demo"}))
    plugins = root / "plugins"
    plugin(plugins, "documents", ext={"kind": "pack", "members": ["pdf", "xlsx"]})
    plugin(
        plugins,
        "pdf",
        ext={"requires": ["doc-runtime"]},
        files={"ai.iowarp.clio/tools/t.py": tools_module("view_pdf")},
    )
    plugin(plugins, "xlsx", ext={"requires": ["doc-runtime"]})
    plugin(plugins, "doc-runtime")
    plugin(
        plugins,
        "shell",
        files={
            "ai.iowarp.clio/tools/t.py": tools_module("shell_bash"),
            "ai.iowarp.clio/prompt/gh.md": fragment(
                "shell", "prefer-gh", "capabilities", "Prefer gh."
            ),
        },
    )
    write(root / "agents" / "standard.py", AGENTS.format(version=version))
    return root


def _service(tmp_path: Path) -> PluginService:
    return PluginService(PluginStore(tmp_path / "store"), native_tools=_native())


def test_external_marketplace_needs_the_trust_step(tmp_path: Path) -> None:
    service = _service(tmp_path)
    summary = service.store.install(str(_market(tmp_path / "m")))
    assert summary["trusted"] is False and summary["agents"] == []
    with pytest.raises(LookupError):
        service.lookup("demo/standard")
    trusted = service.store.trust("demo")
    assert trusted["agents"] == ["standard"]
    resolution = service.resolve("demo/standard")
    assert resolution.plugin_ids == ["core", "documents", "pdf", "xlsx", "shell", "doc-runtime"]
    assert service.pinned(resolution.digest).plugin_ids == resolution.plugin_ids


def test_default_marketplace_is_trusted_at_install(tmp_path: Path) -> None:
    service = _service(tmp_path)
    summary = service.store.install(str(_market(tmp_path / "m")), name="demo", default=True)
    assert summary["trusted"] and summary["agents"] == ["standard"]


def test_update_changing_executable_parts_waits_for_trust(tmp_path: Path) -> None:
    market = _market(tmp_path / "m")
    service = _service(tmp_path)
    service.store.install(str(market))
    service.store.trust("demo")
    write(
        market / "plugins/shell/ai.iowarp.clio/prompt/gh.md",
        fragment("shell", "prefer-gh", "capabilities", "Prefer gh, v2."),
    )
    text_update = service.store.update("demo")
    assert text_update["pending_trust"] == []
    assert "Prefer gh, v2." in service.resolve("demo/standard").prompt_text
    write(
        market / "plugins/shell/ai.iowarp.clio/tools/t.py",
        tools_module("shell_bash", "fs_read_file"),
    )
    exec_update = service.store.update("demo")
    assert exec_update["pending_trust"] == ["shell"]
    assert "fs_read_file" not in service.resolve("demo/standard").tool_names
    service.store.trust("demo")
    assert "fs_read_file" in service.resolve("demo/standard").tool_names


def test_user_agent_is_parent_plus_delta_and_follows_parent_version(tmp_path: Path) -> None:
    market = _market(tmp_path / "m")
    service = _service(tmp_path)
    service.store.install(str(market), name="demo", default=True)
    entry = service.save_user_agent("mine", extends="demo/standard", add=[], remove=["xlsx"])
    stored = json.loads((service.store.root / "user-agents" / "mine.json").read_text())
    assert stored == {
        "id": "mine",
        "name": "mine",
        "extends": "demo/standard@^1",
        "add": [],
        "remove": ["xlsx"],
        "description": "",
    }
    resolution = service.resolve(entry.ref)
    assert "xlsx" not in resolution.plugin_ids and resolution.agent["kind"] == "user"
    write(market / "agents" / "standard.py", AGENTS.format(version="1.1.0"))
    service.store.update("demo")
    assert service.resolve(entry.ref).agent["version"] == "1.1.0"
    preview = service.preview_removal(entry.ref, "doc-runtime")
    # pdf was the documents pack's last member (xlsx is already removed): the pack goes too.
    assert sorted(row["id"] for row in preview["removes"]) == ["doc-runtime", "documents", "pdf"]
    assert {"reason": "pack_empty", "because": "", "id": "documents"} in [
        row["reason"] for row in preview["removes"]
    ]
    assert preview["tools_removed"] == ["view_pdf"]
    write(market / "agents" / "standard.py", AGENTS.format(version="2.0.0"))
    service.store.update("demo")
    with pytest.raises(LookupError):
        service.resolve(entry.ref)


def test_reserved_and_missing(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(StoreError):
        service.store.trust("nope")
    with pytest.raises(ResolutionError):
        service.store.install(str(_market(tmp_path / "m")), name="demo", default=True)
        service.resolve("demo/standard", delta=Delta(remove=("core",)))


def test_plugin_check_reports_each_broken_part(tmp_path: Path) -> None:
    root = tmp_path / "broken-market"
    write(root / "ai.iowarp.clio" / "marketplace.json", json.dumps({"name": "broken"}))
    plugin(
        root / "plugins",
        "missing-prompt",
        files={
            "ai.iowarp.clio/subagents/s.py": (
                "from clio_agent.sdk import PromptFile, Subagent\n\nx = Subagent('helper', prompt=PromptFile('prompt/gone.md'))\n"
            )
        },
    )
    plugin(
        root / "plugins", "collider", files={"ai.iowarp.clio/tools/t.py": tools_module("ask_user")}
    )
    plugin(
        root / "plugins",
        "cycler",
        files={
            "ai.iowarp.clio/hooks/h.py": (
                "from clio_agent.sdk import hook\n\n@hook('tool_call', before='cycler.b')\ndef a(ev):\n    return None\n\n"
                "@hook('tool_call', before='cycler.a')\ndef b(ev):\n    return None\n"
            )
        },
    )
    plugin(root / "plugins", "fine")
    report = check(root, service=_service(tmp_path))
    by_id = {t.id: t for t in report.targets}
    assert not report.ok
    assert by_id["fine@1.0.0"].ok
    codes = {
        name: [d.code for d in t.diagnostics if d.severity == "error"] for name, t in by_id.items()
    }
    assert codes["missing-prompt@1.0.0"] == ["prompt_file_missing"]
    assert codes["collider@1.0.0"] == ["tool_collision"]
    assert codes["cycler@1.0.0"] == ["hook_cycle"]
    text = report.render()
    assert "prompt file ai.iowarp.clio/prompt/gone.md does not exist" in text
    assert "tool 'ask_user' is exposed by both core and collider" in text
    assert "hook ordering has a cycle among cycler.a, cycler.b" in text


def test_concurrent_installs_of_the_same_content_share_one_snapshot(tmp_path: Path) -> None:
    """Two installs of the same source at once (two apps on one store) each stage their own
    copy; the first names the content-addressed snapshot and the other keeps it."""

    import threading

    from clio_agent.plugins.store import _copy_tree

    source = tmp_path / "src"
    for index in range(200):
        (source / f"d{index % 10}").mkdir(parents=True, exist_ok=True)
        (source / f"d{index % 10}" / f"f{index}.txt").write_text("x" * 2000)
    target = tmp_path / "snapshots" / "m" / "digest"
    target.parent.mkdir(parents=True)
    errors: list[BaseException] = []

    def copy() -> None:
        try:
            _copy_tree(source, target)
        except BaseException as exc:  # noqa: BLE001 - collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=copy) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert sorted(p.name for p in target.parent.iterdir()) == ["digest"]
    assert len(list(target.rglob("*.txt"))) == 200
