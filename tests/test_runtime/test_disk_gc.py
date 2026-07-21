"""Tests for ``clio doctor --gc`` disk reclamation (iowarp/clio-agent#1001).

Covers the release-gating guarantees: refusal under a live clio peer, dry-run mutating
nothing, keep-newest-per-server for clio-kit environments, and a defensive skip (never a
wrong delete) when the clio-kit layout is unrecognized.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from clio_agent.runtime import disk_gc


def _make_kit_env(root: Path, name: str, *, size: int, age_days: float, now: float) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "blob.bin").write_bytes(b"\0" * size)
    mtime = now - age_days * 86400.0
    os.utime(d, (mtime, mtime))
    return d


def test_gc_refuses_under_live_peer(tmp_path: Path) -> None:
    peer = disk_gc.ProcRef(pid=4321, name="clio-agent-gact", marker="clio-agent")
    report = disk_gc.run_gc(dry_run=False, peer_prober=lambda: [peer])

    assert report.refused is True
    assert report.refusal_reason == "live_clio_peers_present"
    assert report.peers == [peer]
    assert report.locations == [], "no location is pruned once refused"


def test_clio_kit_keep_newest_per_server(tmp_path: Path) -> None:
    now = time.time()
    env_root = tmp_path / "mcp-environments"
    old_a = _make_kit_env(env_root, "pandas-aaa111", size=100, age_days=10, now=now)
    new_a = _make_kit_env(env_root, "pandas-bbb222", size=100, age_days=1, now=now)
    only_b = _make_kit_env(env_root, "gdal-ccc333", size=100, age_days=5, now=now)

    result = disk_gc._prune_clio_kit_environments(tmp_path, dry_run=False)

    assert result.reason == "kept_newest_per_server"
    assert new_a.exists(), "newest env per server is kept"
    assert only_b.exists(), "the sole env of a server is kept"
    assert not old_a.exists(), "older env of the same server is evicted"
    assert result.kept == 2
    assert result.removed == 1


def test_clio_kit_dry_run_mutates_nothing(tmp_path: Path) -> None:
    now = time.time()
    env_root = tmp_path / "mcp-environments"
    old_a = _make_kit_env(env_root, "pandas-aaa111", size=100, age_days=10, now=now)
    new_a = _make_kit_env(env_root, "pandas-bbb222", size=100, age_days=1, now=now)

    result = disk_gc._prune_clio_kit_environments(tmp_path, dry_run=True)

    assert old_a.exists() and new_a.exists(), "dry-run deletes nothing"
    assert result.removed == 1, "dry-run still reports the plan"
    assert result.bytes_freed and result.bytes_freed >= 100


def test_clio_kit_unrecognized_layout_skipped(tmp_path: Path) -> None:
    env_root = tmp_path / "mcp-environments"
    # Names WITHOUT the '<server>-<hash>' shape (no '-'): must not be guessed at.
    (env_root / "mysteryenv").mkdir(parents=True)
    (env_root / "mysteryenv" / "blob.bin").write_bytes(b"x")

    result = disk_gc._prune_clio_kit_environments(tmp_path, dry_run=False)

    assert result.reason == "clio_kit_layout_unrecognized"
    assert result.bytes_freed is None
    assert (env_root / "mysteryenv").exists(), "unrecognized layout is never deleted"


def test_clio_kit_absent_dir_skipped(tmp_path: Path) -> None:
    result = disk_gc._prune_clio_kit_environments(tmp_path, dry_run=False)
    assert result.reason == "clio_kit_layout_absent"


def test_clio_kit_none_cache_skipped() -> None:
    result = disk_gc._prune_clio_kit_environments(None, dry_run=False)
    assert result.reason == "clio_kit_cache_not_found"
    assert result.bytes_freed is None


def test_stale_temp_prune_respects_age(tmp_path: Path, monkeypatch) -> None:
    now = time.time()
    monkeypatch.setenv("CLIO_MCP_CACHE_TEMP_ROOTS", str(tmp_path))
    monkeypatch.setenv("CLIO_MCP_CACHE_TEMP_MAX_AGE_DAYS", "3")
    from clio_agent import conf

    conf.reload()

    stale = tmp_path / "pytest-of-alice"
    stale.mkdir()
    (stale / "f").write_bytes(b"\0" * 50)
    os.utime(stale, (now - 10 * 86400, now - 10 * 86400))

    fresh = tmp_path / "pytest-of-bob"
    fresh.mkdir()
    (fresh / "f").write_bytes(b"\0" * 50)
    os.utime(fresh, (now - 1 * 86400, now - 1 * 86400))

    unrelated = tmp_path / "important-data"
    unrelated.mkdir()
    os.utime(unrelated, (now - 99 * 86400, now - 99 * 86400))

    result = disk_gc._prune_stale_temp(os.environ, now=now, dry_run=False)

    assert not stale.exists(), "stale pytest basetemp evicted"
    assert fresh.exists(), "fresh basetemp kept"
    assert unrelated.exists(), "non-pytest temp dirs are never touched"
    assert result.removed == 1


def test_stale_temp_dry_run_mutates_nothing(tmp_path: Path, monkeypatch) -> None:
    now = time.time()
    monkeypatch.setenv("CLIO_MCP_CACHE_TEMP_ROOTS", str(tmp_path))
    monkeypatch.setenv("CLIO_MCP_CACHE_TEMP_MAX_AGE_DAYS", "3")
    from clio_agent import conf

    conf.reload()

    stale = tmp_path / "pytest-of-alice"
    stale.mkdir()
    (stale / "f").write_bytes(b"\0" * 50)
    os.utime(stale, (now - 10 * 86400, now - 10 * 86400))

    result = disk_gc._prune_stale_temp(os.environ, now=now, dry_run=True)
    assert stale.exists(), "dry-run deletes nothing"
    assert result.removed == 1


def test_live_peer_probe_excludes_given_pids() -> None:
    # Excluding this process's own pid must remove it from the peer set even if this
    # test runner's cmdline carries a clio marker.
    peers = disk_gc.live_peer_clio_processes(exclude_pids={os.getpid()})
    assert all(p.pid != os.getpid() for p in peers)


def test_gc_report_serializes() -> None:
    peer = disk_gc.ProcRef(pid=1, name="clio_run", marker="clio-core-daemon-pidfile")
    report = disk_gc.run_gc(dry_run=True, peer_prober=lambda: [peer])
    d = report.to_dict()
    assert d["refused"] is True
    assert d["peers"] == [str(peer)]
