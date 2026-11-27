"""A plugin says where its own state shows the person set it up; its servers' last
instructions are kept by plugin, for finding it while none of them runs."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from clio_agent.plugins import host_semantics, recorded_instructions
from clio_agent.plugins.loader import load_plugin
from clio_agent.plugins.setup_state import is_set_up, setup_files
from clio_agent.tools import mcp_server_instructions
from tests.test_plugins.helpers import plugin


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    store = tmp_path / "store"
    monkeypatch.setattr("clio_agent.plugins.service.plugin_store_root", lambda: store)
    monkeypatch.setattr(host_semantics, "_BY_PLUGIN", {})
    recorded_instructions.forget_loaded()
    yield store
    recorded_instructions.forget_loaded()


def _record(*files: str) -> SimpleNamespace:
    return SimpleNamespace(id="sites", host={"setup": {"files": list(files)}} if files else {})


def test_the_manifest_declares_setup_files(tmp_path: Path) -> None:
    root = plugin(
        tmp_path,
        "sites",
        ext={"setup": {"files": ["config/sites.yaml", "accounts.json"]}},
        servers={"s": {"type": "stdio", "command": "python3", "args": ["s.py"]}},
    )
    record = load_plugin(root, trusted=True)

    assert record.host["setup"] == {"files": ["config/sites.yaml", "accounts.json"]}
    assert setup_files(record) == ["config/sites.yaml", "accounts.json"]
    assert setup_files(SimpleNamespace(host={})) == []


@pytest.mark.parametrize(
    ("name", "text", "expected"),
    [
        ("sites.yaml", "- id: lab\n  kind: ssh\n", True),
        ("sites.yaml", "[]\n", False),
        ("sites.yaml", "{}\n", False),
        ("sites.yaml", "sites: {}\nversion:\n", False),
        ("sites.yaml", "sites: {}\nversion: 1\n", True),
        ("sites.yaml", "sites:\n  lab: {kind: ssh}\n", True),
        ("sites.yaml", "# nothing yet\n", False),
        ("sites.yaml", "  \n", False),
        ("sites.yaml", "[unclosed\n", False),
        ("sites.json", '{"lab": {"kind": "ssh"}}', True),
        ("sites.json", "null", False),
        ("token", "abc\n", True),
        ("token", "", False),
    ],
)
def test_a_declared_file_counts_when_it_holds_a_value(
    tmp_path: Path, name: str, text: str, expected: bool
) -> None:
    data = tmp_path / "data"
    (data / "config").mkdir(parents=True)
    (data / "config" / name).write_text(text, encoding="utf-8")

    assert is_set_up(_record(f"config/{name}"), data) is expected


def test_missing_undeclared_and_escaping_files_do_not_count(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    outside = tmp_path / "outside.yaml"
    outside.write_text("- id: lab\n", encoding="utf-8")

    assert not is_set_up(_record("config/sites.yaml"), data)
    assert not is_set_up(_record(), data)
    try:
        os.symlink(outside, data / "linked.yaml")
    except OSError:
        pytest.skip("symlinks unavailable")
    assert not is_set_up(_record("linked.yaml"), data)


def test_the_default_folder_is_the_plugins_data(_isolated: Path) -> None:
    folder = _isolated / "data" / "sites" / "config"
    folder.mkdir(parents=True)
    (folder / "sites.yaml").write_text("- id: lab\n", encoding="utf-8")

    assert is_set_up(_record("config/sites.yaml"))


def test_a_plugin_servers_live_instructions_are_kept_by_plugin(_isolated: Path) -> None:
    host_semantics.remember({"remote": {"plugin": "sites", "host": {}}})
    mcp_server_instructions.record_instructions("remote", "Sites: lab (ssh).")
    mcp_server_instructions.record_instructions("loose", "Not a plugin's server.")
    mcp_server_instructions.record_instructions("remote", "Old text.", cached=True)

    assert recorded_instructions.plugin_instructions("sites") == "Sites: lab (ssh)."
    kept = json.loads((_isolated / "server_instructions.json").read_text(encoding="utf-8"))
    assert kept["schema"] == 1 and list(kept["plugins"]) == ["sites"]
    assert kept["plugins"]["sites"]["remote"]["text"] == "Sites: lab (ssh)."

    # A new process reads them back; an unchanged text does not rewrite the file.
    recorded_instructions.forget_loaded()
    assert recorded_instructions.plugin_instructions("sites") == "Sites: lab (ssh)."
    stamp = (_isolated / "server_instructions.json").stat().st_mtime_ns
    recorded_instructions.remember("sites", "remote", "Sites: lab (ssh).")
    assert (_isolated / "server_instructions.json").stat().st_mtime_ns == stamp

    # A server that now sends none forgets them.
    mcp_server_instructions.record_instructions("remote", None)
    assert recorded_instructions.plugin_instructions("sites") == ""
    for name in ("remote", "loose"):
        mcp_server_instructions.forget_instructions(name)


def test_a_server_two_plugins_name_is_kept_for_neither() -> None:
    host_semantics.remember({"shared": {"plugin": "one", "host": {}}})
    host_semantics.remember({"shared": {"plugin": "two", "host": {}}})

    assert host_semantics.server_plugin("shared") == ""
    assert host_semantics.server_plugin("absent") == ""


def test_an_unwritable_store_keeps_the_text_for_this_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*_args: object) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(recorded_instructions, "_write", refuse)
    recorded_instructions.remember("sites", "remote", "Sites: lab.")

    assert recorded_instructions.plugin_instructions("sites") == "Sites: lab."
