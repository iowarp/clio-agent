"""Source inspection is bounded, inert and confined to the installed record."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from clio_agent.plugins import inspection
from clio_agent.plugins.records import PluginRecord


def record(root: Path) -> PluginRecord:
    """An installed record whose source may be read but never imported."""
    return PluginRecord("notes", "1.0", "Notes", "plugin", "agent-plugins", str(root))


@pytest.fixture()
def plugin_root(tmp_path: Path) -> Path:
    """Keep package source separate from the test's configuration files."""
    root = tmp_path / "package"
    root.mkdir()
    return root


def test_inspection_reads_exact_source_without_executing_and_ignores_environments(
    plugin_root: Path,
) -> None:
    text = "raise AssertionError('inspection executed code')\n"
    (plugin_root / "plugin.py").write_bytes(text.encode("utf-8"))
    (plugin_root / ".venv").mkdir()
    (plugin_root / ".venv" / "secret.py").write_text("hidden", encoding="utf-8")
    result = inspection.inspect_plugin_files(record(plugin_root))
    assert not result["incomplete"]
    assert result["files"] == [
        {
            "path": "plugin.py",
            "text": text,
            "bytes": len(text),
            "truncated": False,
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "error": "",
        }
    ]


def test_inspection_bounds_large_files_and_does_not_label_prefix_as_whole_file_hash(
    plugin_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(inspection, "MAX_FILE_BYTES", 8)
    (plugin_root / "long.md").write_text("a" * 20, encoding="utf-8")
    result = inspection.inspect_plugin_files(record(plugin_root))
    assert result["incomplete"]
    assert result["files"][0] == {
        "path": "long.md",
        "text": "a" * 8,
        "bytes": 20,
        "truncated": True,
        "sha256": "",
        "error": "",
    }


def test_inspection_bounds_the_inventory_and_reports_missing_and_non_utf8_sources(
    plugin_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(inspection, "MAX_FILES", 1)
    (plugin_root / "a.md").write_bytes(b"\xff")
    (plugin_root / "b.md").write_text("not reached", encoding="utf-8")
    result = inspection.inspect_plugin_files(record(plugin_root))
    assert result["incomplete"] and len(result["files"]) == 1
    assert result["files"][0]["error"]
    assert inspection.inspect_plugin_files(record(plugin_root / "missing"))["incomplete"]


def test_inspection_refuses_a_file_resolving_outside_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "installed"
    root.mkdir()
    (root / "escape.md").write_text("must not be read", encoding="utf-8")
    resolve = Path.resolve

    def redirected(path: Path, strict: bool = False) -> Path:
        return tmp_path / "outside.md" if path.name == "escape.md" else resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", redirected)
    assert inspection.inspect_plugin_files(record(root))["files"] == []
