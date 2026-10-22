"""Plugins Clio writes: authorship holds only for the exact files written, and the review."""

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
    return authored.write(root, "tally", files, "sess_1")


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
