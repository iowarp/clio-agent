"""Plugins Clio writes into the private marketplace: authorship holds only for the exact
files written, each version records its diff, names the person or another marketplace
owns are refused, and the review names every executable part."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clio_agent.plugins import authored

MANIFEST = json.dumps({"name": "tally", "version": "0.1.0", "description": "Counts votes."})
FLOW = '''
from clio_agent.sdk import Flow


class Tally(Flow):
    id = "tally"
    description = (
        "Asks each voter, then counts. "
        "Options: voters."
    )
    shape = """railroad-beta
tally = sequence(oneOrMore(terminal("voter")), terminal("count"));"""

    async def forward(self, task, ctx):
        return {"answer": "counted"}
'''


def _write(root: Path, **extra: str) -> Path:
    files = {"plugin.json": MANIFEST, "ai.iowarp.clio/flows/tally.py": FLOW, **extra}
    return authored.write(root, "tally", files, "sess_1")[0]


def test_a_written_plugin_is_authored_until_a_file_changes(tmp_path: Path) -> None:
    folder = _write(tmp_path)
    record = authored.authorship(tmp_path, "tally")
    assert record is not None and record["session_id"] == "sess_1"
    # Byte caches do not count as a change.
    (folder / "ai.iowarp.clio/flows/__pycache__").mkdir()
    (folder / "ai.iowarp.clio/flows/__pycache__/tally.pyc").write_bytes(b"\0")
    assert authored.authorship(tmp_path, "tally") is not None
    (folder / "ai.iowarp.clio/flows/tally.py").write_text(FLOW + "\n# edited\n")
    assert authored.authorship(tmp_path, "tally") is None


def test_a_file_added_after_writing_ends_authorship(tmp_path: Path) -> None:
    folder = _write(tmp_path)
    (folder / "extra.py").write_text("import os\n")
    assert authored.authorship(tmp_path, "tally") is None


def test_rewriting_replaces_the_whole_folder(tmp_path: Path) -> None:
    folder = _write(tmp_path, **{"old.txt": "stale"})
    _write(tmp_path)
    assert not (folder / "old.txt").exists()
    assert authored.authorship(tmp_path, "tally") is not None


@pytest.mark.parametrize("path", ["../escape.py", "/abs/path.py", ""])
def test_a_path_outside_the_plugin_is_refused(tmp_path: Path, path: str) -> None:
    with pytest.raises(authored.AuthoredError) as raised:
        _write(tmp_path, **{path: "x"})
    assert raised.value.reason == "path_outside_plugin"


def test_a_plugin_without_a_manifest_or_with_a_bad_name_is_refused(tmp_path: Path) -> None:
    with pytest.raises(authored.AuthoredError) as raised:
        authored.write(tmp_path, "tally", {"a.py": ""}, "s")
    assert raised.value.reason == "manifest_missing"
    with pytest.raises(authored.AuthoredError) as raised:
        authored.write(tmp_path, "Bad Name", {"plugin.json": MANIFEST}, "s")
    assert raised.value.reason == "invalid_plugin_name"


def test_the_review_reads_flows_and_shape_without_importing_the_code(tmp_path: Path) -> None:
    folder = _write(tmp_path, **{"ai.iowarp.clio/flows/boom.py": "raise SystemExit(1)\n"})
    review = authored.review(folder)
    assert review["name"] == "tally" and review["version"] == "0.1.0"
    assert review["description"] == "Counts votes."
    assert review["flows"] == [
        {
            "id": "tally",
            "description": "Asks each voter, then counts. Options: voters.",
            "shape": 'railroad-beta\ntally = sequence(oneOrMore(terminal("voter")), '
            'terminal("count"));',
            "file": "ai.iowarp.clio/flows/tally.py",
        }
    ]
    # Only asking agents through ctx: nothing reached directly.
    assert review["reach"] == []
    assert set(review["files"]) == {
        "plugin.json",
        "ai.iowarp.clio/flows/tally.py",
        "ai.iowarp.clio/flows/boom.py",
    }


def test_the_review_names_what_the_code_reaches_directly(tmp_path: Path) -> None:
    code = "import subprocess\nfrom requests import get\ndef f():\n    open('x')\n    eval('1')\n"
    folder = _write(tmp_path, **{"ai.iowarp.clio/flows/more.py": code, "broken.py": "def ("})
    review = authored.review(folder)
    assert review["reach"] == ["commands", "dynamic code", "files", "network"]
    assert review["unreadable"] == ["broken.py"]


def test_it_lands_in_the_private_marketplace(tmp_path: Path) -> None:
    folder = _write(tmp_path)
    assert folder == tmp_path / "local" / "private-local" / "plugins" / "tally"
    index = tmp_path / "local" / "private-local" / "ai.iowarp.clio" / "marketplace.json"
    assert json.loads(index.read_text())["name"] == "private-local"


def test_a_new_version_records_its_diff_and_which_executable_files_changed(tmp_path: Path) -> None:
    _write(tmp_path, **{"skills/count/SKILL.md": "---\nname: count\n---\nCount once.\n"})
    folder, version = authored.write(
        tmp_path,
        "tally",
        {
            "plugin.json": MANIFEST,
            "ai.iowarp.clio/flows/tally.py": FLOW.replace("counted", "counted twice"),
            "skills/count/SKILL.md": "---\nname: count\n---\nCount twice.\n",
        },
        "sess_2",
    )
    assert version["version"] == 2 and version["session_id"] == "sess_2"
    diff = version["diff"]
    assert diff["changed"] == ["ai.iowarp.clio/flows/tally.py", "skills/count/SKILL.md"]
    assert diff["executable_changed"] == ["ai.iowarp.clio/flows/tally.py"]
    assert "-Count once." in diff["unified"] and "+Count twice." in diff["unified"]
    record = authored.authorship(tmp_path, "tally")
    assert record is not None and record["version"] == 2 and record["versions"] == 2


def test_changed_since_trusted_names_only_executable_files(tmp_path: Path) -> None:
    _write(tmp_path)
    authored.note_installed(tmp_path, "tally", "exec-v1")
    authored.write(
        tmp_path,
        "tally",
        {
            "plugin.json": MANIFEST,
            "ai.iowarp.clio/flows/tally.py": FLOW + "\n# v2\n",
            "README.md": "notes",
        },
        "sess_2",
    )
    assert authored.changed_since(tmp_path, "tally", "exec-v1") == ["ai.iowarp.clio/flows/tally.py"]
    assert authored.changed_since(tmp_path, "tally", "never-trusted") is None


def test_a_name_the_person_wrote_is_refused(tmp_path: Path) -> None:
    folder = authored.plugin_folder(tmp_path, "notes")
    folder.mkdir(parents=True)
    (folder / "plugin.json").write_text(MANIFEST)
    with pytest.raises(authored.AuthoredError) as raised:
        authored.write(tmp_path, "notes", {"plugin.json": MANIFEST}, "s")
    assert raised.value.reason == "name_taken_by_person"
    assert (folder / "plugin.json").read_text() == MANIFEST


def test_a_plugin_clio_wrote_then_the_person_edited_is_theirs(tmp_path: Path) -> None:
    folder = _write(tmp_path)
    (folder / "plugin.json").write_text(MANIFEST.replace("Counts", "Tallies"))
    with pytest.raises(authored.AuthoredError) as raised:
        _write(tmp_path)
    assert raised.value.reason == "name_taken_by_person"


def test_a_name_another_marketplace_provides_is_refused_with_the_variation_path(
    tmp_path: Path,
) -> None:
    with pytest.raises(authored.AuthoredError) as raised:
        authored.write(tmp_path, "hdf5", {"plugin.json": MANIFEST}, "s", taken_elsewhere="clio")
    assert raised.value.reason == "name_taken_by_marketplace"
    assert "variation_of" in str(raised.value) and "clio/hdf5" in str(raised.value)
    assert not authored.plugin_folder(tmp_path, "hdf5").exists()


def test_a_variation_records_what_it_varies(tmp_path: Path) -> None:
    _, version = authored.write(
        tmp_path, "hdf5-lab", {"plugin.json": MANIFEST}, "s", variation_of="clio/hdf5"
    )
    assert version["variation_of"] == "clio/hdf5"
    _, again = authored.write(tmp_path, "hdf5-lab", {"plugin.json": MANIFEST}, "s2")
    assert again["variation_of"] == "clio/hdf5"


def test_the_review_names_tools_hooks_subagents_bindings_and_servers(tmp_path: Path) -> None:
    tool = (
        "from clio_agent.sdk import tool\n\n"
        "@tool(read_only=True)\n"
        "def to_kelvin(c: float) -> float:\n"
        '    """Convert Celsius to kelvin."""\n'
        "    return c + 273.15\n"
    )
    hook = (
        "from clio_agent.sdk import hook\n\n"
        '@hook("tool_call", tools="fs_write_file")\n'
        "def protect_raw(ev):\n"
        '    """Never write to raw/."""\n'
    )
    sub = (
        "from clio_agent.sdk import Subagent\n"
        'checker = Subagent("unit-checker", description="Checks units.")\n'
    )
    manifest = json.dumps(
        {
            "name": "tally",
            "version": "0.1.0",
            "extensions": {
                "ai.iowarp.clio": {
                    "kernel_bindings": {"units": {"source": "ai.iowarp.clio/kernel/u.py"}}
                }
            },
        }
    )
    folder = authored.write(
        tmp_path,
        "tally",
        {
            "plugin.json": manifest,
            "ai.iowarp.clio/tools/units.py": tool,
            "ai.iowarp.clio/hooks/raw.py": hook,
            "ai.iowarp.clio/subagents/check.py": sub,
            "ai.iowarp.clio/kernel/u.py": "export = None\n",
            "mcp.json": json.dumps({"mcpServers": {"lab": {"command": "lab-server"}}}),
            "skills/count/SKILL.md": "text",
        },
        "s",
    )[0]
    review = authored.review(folder)
    assert review["tools"] == [
        {
            "name": "to_kelvin",
            "description": "Convert Celsius to kelvin.",
            "read_only": True,
            "file": "ai.iowarp.clio/tools/units.py",
        }
    ]
    assert review["hooks"][0]["id"] == "protect_raw"
    assert review["hooks"][0]["event"] == "tool_call"
    assert review["hooks"][0]["tools"] == "fs_write_file"
    assert review["subagents"][0]["id"] == "unit-checker"
    assert review["bindings"][0]["name"] == "units"
    assert review["servers"] == ["lab"]
    assert "skills/count/SKILL.md" not in review["executable_files"]
    assert "mcp.json" in review["executable_files"]
