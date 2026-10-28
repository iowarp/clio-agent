"""Superseded marketplace snapshots are collected; anything a record names is kept."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from clio_agent.plugins.snapshot_gc import (
    REMOVED_ABANDONED_STAGING,
    REMOVED_SUPERSEDED,
    collect_superseded_snapshots,
)

OLD = time.time() - 3600


def _snapshot(root: Path, market: str, digest: str, *, age: float = OLD) -> Path:
    path = root / "snapshots" / market / digest
    (path / "skills").mkdir(parents=True)
    (path / "skills" / "SKILL.md").write_text("x" * 100)
    os.utime(path, (age, age))
    return path


def _store(tmp_path: Path) -> Path:
    root = tmp_path / "plugins"
    current = _snapshot(root, "market", "current")
    pending = _snapshot(root, "market", "pending")
    _snapshot(root, "market", "old")
    _snapshot(root, "market", "pinned")
    _snapshot(root, "gone", "old")
    state = {
        "version": 1,
        "marketplaces": {"market": {"snapshot": str(current)}},
        "plugins": {},
        "pending": {"market/p": {"root": str(pending / "plugin")}},
        "agents": {},
    }
    (root / "state.json").write_text(json.dumps(state))
    (root / "resolved").mkdir()
    pinned = {"plugins": [{"root": str(root / "snapshots" / "market" / "pinned")}]}
    (root / "resolved" / "abc.json").write_text(json.dumps(pinned))
    return root


def test_only_unreferenced_snapshots_are_removed(tmp_path: Path) -> None:
    root = _store(tmp_path)
    result = collect_superseded_snapshots(root)
    removed = sorted(Path(row["path"]).relative_to(root).as_posix() for row in result.removed)
    assert removed == ["snapshots/gone/old", "snapshots/market/old"]
    assert {row["reason"] for row in result.removed} == {REMOVED_SUPERSEDED}
    assert result.bytes_freed == 200 and result.kept == 3
    left = sorted(p.name for p in (root / "snapshots" / "market").iterdir())
    assert left == ["current", "pending", "pinned"]
    assert not (root / "snapshots" / "gone").exists()


def test_dry_run_removes_nothing(tmp_path: Path) -> None:
    root = _store(tmp_path)
    result = collect_superseded_snapshots(root, dry_run=True)
    assert len(result.removed) == 2
    assert (root / "snapshots" / "market" / "old").is_dir()


def test_a_fresh_copy_is_kept_and_abandoned_staging_is_typed(tmp_path: Path) -> None:
    root = _store(tmp_path)
    _snapshot(root, "market", "racing", age=time.time())
    _snapshot(root, "market", "old.partial-0123")
    result = collect_superseded_snapshots(root)
    reasons = {Path(row["path"]).name: row["reason"] for row in result.removed}
    assert reasons["old.partial-0123"] == REMOVED_ABANDONED_STAGING
    assert "racing" not in reasons
    assert (root / "snapshots" / "market" / "racing").is_dir()


def test_an_unreadable_resolution_removes_nothing(tmp_path: Path) -> None:
    root = _store(tmp_path)
    (root / "resolved" / "broken.json").write_text("{not json")
    with pytest.raises(ValueError):
        collect_superseded_snapshots(root)
    assert (root / "snapshots" / "market" / "old").is_dir()


def test_install_and_doctor_gc_skip_while_a_server_is_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.runtime import disk_gc, document_install

    root = _store(tmp_path)
    monkeypatch.setenv("CLIO_PLUGIN_STORE", str(root))
    monkeypatch.setattr(disk_gc, "live_peer_clio_processes", lambda: ["gact 4242"])
    assert document_install.collect_snapshots_after_install() == {
        "status": "skipped",
        "reason": "marketplace_snapshots_skipped_live",
    }
    assert (root / "snapshots" / "market" / "old").is_dir()
    monkeypatch.setattr(disk_gc, "live_peer_clio_processes", lambda: [])
    outcome = document_install.collect_snapshots_after_install()
    assert outcome["status"] == "collected" and outcome["removed"] == 2
    row = disk_gc._collect_marketplace_snapshots(now=time.time(), dry_run=True)
    assert row.reason == "superseded_collected" and row.removed == 0 and row.kept == 3
