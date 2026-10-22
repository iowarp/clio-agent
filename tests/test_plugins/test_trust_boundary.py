"""No plugin Python runs, and no trust is inherited, before the person's trust step."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clio_agent.plugins import authored
from clio_agent.plugins.check import check
from clio_agent.plugins.manifest import PLUGIN_SCHEMA
from clio_agent.plugins.service import PluginService
from clio_agent.plugins.store import PluginStore, StoreError, builtin_records
from tests.test_plugins.helpers import NATIVE, plugin, write


def _native() -> frozenset[str]:
    core = next(r for r in builtin_records() if r.id == "core")
    return NATIVE | {t.name for t in core.tools}


def _marker_module(marker: Path) -> str:
    return f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran')\n"


def _market(root: Path, name: str = "demo") -> Path:
    write(root / "ai.iowarp.clio" / "marketplace.json", json.dumps({"name": name}))
    return root


def test_checking_a_plugin_clio_wrote_never_imports_it(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    files = {
        "plugin.json": json.dumps({"$schema": PLUGIN_SCHEMA, "name": "evil", "version": "0.1.0"}),
        "ai.iowarp.clio/flows/x.py": _marker_module(marker),
    }
    folder = authored.write(tmp_path / "store", "evil", files, "sess")
    service = PluginService(PluginStore(tmp_path / "store"), native_tools=_native())
    report = check(folder, service=service, trusted=False)
    assert not marker.exists()
    assert report.ok


def test_an_untrusted_check_still_reports_broken_python(tmp_path: Path) -> None:
    files = {
        "plugin.json": json.dumps({"$schema": PLUGIN_SCHEMA, "name": "broken", "version": "0.1.0"}),
        "ai.iowarp.clio/flows/x.py": "def oops(:\n",
    }
    folder = authored.write(tmp_path / "store", "broken", files, "sess")
    service = PluginService(PluginStore(tmp_path / "store"), native_tools=_native())
    report = check(folder, service=service, trusted=False)
    assert [d.code for d in report.diagnostics if d.severity == "error"] == ["python_syntax"]


def test_another_source_cannot_take_an_installed_marketplace_name(tmp_path: Path) -> None:
    store = PluginStore(tmp_path / "store")
    default = _market(tmp_path / "default", "clio")
    plugin(default / "plugins", "hello")
    store.install(str(default), name="clio", default=True)
    other = _market(tmp_path / "other", "clio")
    plugin(other / "plugins", "evil")
    with pytest.raises(StoreError) as caught:
        store.install(str(other), name="clio")
    assert caught.value.code == "marketplace_name_taken"
    entry = store.marketplaces()["clio"]
    assert entry["url"] == str(default.resolve()) and entry["default"]
    assert [r.id for r in store.records() if r.marketplace == "clio"] == ["hello"]


def test_the_same_source_reinstalls_and_keeps_its_trust(tmp_path: Path) -> None:
    store = PluginStore(tmp_path / "store")
    market = _market(tmp_path / "m")
    plugin(market / "plugins", "hello")
    store.install(str(market))
    store.trust("demo")
    store.install(str(market))
    assert store.marketplaces()["demo"]["trusted"]


def test_an_update_holds_changed_python_without_importing_it(tmp_path: Path) -> None:
    store = PluginStore(tmp_path / "store")
    market = _market(tmp_path / "m")
    marker = tmp_path / "ran"
    plugin(market / "plugins", "p", files={"ai.iowarp.clio/tools/t.py": "X = 1\n"})
    store.install(str(market))
    store.trust("demo")
    write(market / "plugins/p/ai.iowarp.clio/tools/t.py", _marker_module(marker))
    plugin(market / "plugins", "q", files={"ai.iowarp.clio/tools/t.py": _marker_module(marker)})
    summary = store.update("demo")
    assert not marker.exists()
    assert sorted(summary["pending_trust"]) == ["p", "q"]
    assert not any(store.trusted(r) for r in store.records() if r.id == "q")


def test_an_update_holds_changed_marketplace_agents_without_importing_them(
    tmp_path: Path,
) -> None:
    store = PluginStore(tmp_path / "store")
    market = _market(tmp_path / "m")
    plugin(market / "plugins", "hello")
    write(market / "agents" / "a.py", "X = 1\n")
    store.install(str(market))
    store.trust("demo")
    marker = tmp_path / "ran"
    write(market / "agents" / "a.py", _marker_module(marker))
    summary = store.update("demo")
    assert not marker.exists()
    assert summary["pending_trust"] == ["agents"]


def test_a_changed_server_binary_or_script_needs_the_trust_step_again(tmp_path: Path) -> None:
    store = PluginStore(tmp_path / "store")
    market = _market(tmp_path / "m")
    servers = {
        "srv": {"type": "stdio", "command": "./bin/server"},
        "py": {"type": "stdio", "command": "python3", "args": ["${PLUGIN_ROOT}/server.py"]},
    }
    root = plugin(
        market / "plugins",
        "p",
        servers=servers,
        files={"bin/server": "#!/bin/sh\necho ok\n", "server.py": "print('ok')\n"},
    )
    store.install(str(market))
    store.trust("demo")
    write(root / "README.md", "Docs only.")
    assert store.update("demo")["pending_trust"] == []
    write(root / "server.py", "import os\nos.system('id')\n")
    assert store.update("demo")["pending_trust"] == ["p"]


@pytest.mark.parametrize("path", ["..\\..\\x.py", "C:/x.py", "a/b:stream", "\\\\host\\share\\x"])
def test_an_authored_path_cannot_escape_on_windows(tmp_path: Path, path: str) -> None:
    files = {"plugin.json": json.dumps({"name": "p", "version": "0.1.0"}), path: "x"}
    with pytest.raises(authored.AuthoredError):
        authored.write(tmp_path, "p", files, "sess")


def test_a_file_a_server_runs_is_executable_whatever_its_name(tmp_path: Path) -> None:
    store = PluginStore(tmp_path / "store")
    market = _market(tmp_path / "m")
    servers = {"s": {"type": "stdio", "command": "python3", "args": ["${PLUGIN_ROOT}/srv.txt"]}}
    root = plugin(market / "plugins", "p", servers=servers, files={"srv.txt": "print(1)\n"})
    store.install(str(market))
    store.trust("demo")
    write(root / "srv.txt", "import os\nos.system('id')\n")
    assert store.update("demo")["pending_trust"] == ["p"]


def test_a_claude_code_server_script_is_executable_whatever_its_name(tmp_path: Path) -> None:
    store = PluginStore(tmp_path / "store")
    market = tmp_path / "cc"
    root = market / "p"
    write(root / ".claude-plugin" / "plugin.json", json.dumps({"name": "p"}))
    servers = {"mcpServers": {"s": {"command": "python3", "args": ["${CLAUDE_PLUGIN_ROOT}/s.md"]}}}
    write(root / ".mcp.json", json.dumps(servers))
    write(root / "s.md", "print(1)\n")
    entry = {"name": "p", "source": "./p"}
    write(
        market / ".claude-plugin" / "marketplace.json",
        json.dumps({"name": "cc", "plugins": [entry]}),
    )
    store.install(str(market))
    store.trust("cc")
    write(root / "s.md", "import os\nos.system('id')\n")
    assert store.update("cc")["pending_trust"] == ["p"]
