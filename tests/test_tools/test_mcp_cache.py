"""Tests for the MCP uv-cache disk bound (iowarp/clio-agent#1001).

Failing-first coverage of the prune contract: age eviction, oldest-first size eviction,
newest-kept, typed reasons, dry-run mutating nothing, and the boot-only live-peer guard.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from clio_agent.tools import mcp_cache


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    """A clean uv-cache root under tmp_path.

    The autouse conftest fixture seeds an ``xdg/`` config subtree directly in ``tmp_path``,
    so using ``tmp_path`` itself as the cache dir would let that unrelated state count as
    the cache "archive". A dedicated subdirectory isolates the prune from it.
    """
    d = tmp_path / "uvcache"
    d.mkdir()
    return d


def _make_env(cache_dir: Path, name: str, *, size_bytes: int, age_days: float, now: float) -> Path:
    """Create an ``environments-v2/<name>`` venv-like dir of a given size + mtime."""
    env_dir = cache_dir / "environments-v2" / name
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "Lib").mkdir(exist_ok=True)
    payload = env_dir / "Lib" / "blob.bin"
    payload.write_bytes(b"\0" * size_bytes)
    mtime = now - age_days * 86400.0
    os.utime(env_dir, (mtime, mtime))
    return env_dir


def _archive_file(cache_dir: Path, size_bytes: int) -> Path:
    """A wheels-v6 archive file that must count toward size but never be evicted."""
    wheels = cache_dir / "wheels-v6"
    wheels.mkdir(parents=True, exist_ok=True)
    f = wheels / "some_wheel.whl"
    f.write_bytes(b"\0" * size_bytes)
    return f


def test_missing_cache_dir_is_noop(tmp_path: Path) -> None:
    result = mcp_cache.prune_uv_cache(tmp_path / "nope", max_bytes=1_000_000, max_age_days=14)
    assert result.existed is False
    assert result.count == 0
    assert result.bytes_freed == 0


def test_age_prune_evicts_old_keeps_new(cache_dir: Path) -> None:
    now = time.time()
    old = _make_env(cache_dir, "server-old", size_bytes=100, age_days=30, now=now)
    new = _make_env(cache_dir, "server-new", size_bytes=100, age_days=1, now=now)

    result = mcp_cache.prune_uv_cache(cache_dir, max_bytes=10**9, max_age_days=14, now=now)

    assert not old.exists(), "environment older than max_age must be evicted"
    assert new.exists(), "fresh environment must be kept"
    assert result.count == 1
    assert result.entries[0].reason == "mcp_cache_pruned_age"
    assert result.entries[0].bytes_freed >= 100


def test_size_prune_oldest_first_keeps_newest(cache_dir: Path) -> None:
    now = time.time()
    # Three ~1000-byte envs; budget only fits one -> two oldest evicted, newest kept.
    e1 = _make_env(cache_dir, "a-1", size_bytes=1000, age_days=3, now=now)
    e2 = _make_env(cache_dir, "a-2", size_bytes=1000, age_days=2, now=now)
    e3 = _make_env(cache_dir, "a-3", size_bytes=1000, age_days=1, now=now)

    result = mcp_cache.prune_uv_cache(cache_dir, max_bytes=1500, max_age_days=999, now=now)

    assert not e1.exists() and not e2.exists(), "oldest environments evicted first for size"
    assert e3.exists(), "the newest environment is kept"
    assert result.total_bytes_after <= 1500
    assert all(e.reason == "mcp_cache_pruned_size" for e in result.entries)
    assert result.count == 2


def test_size_residual_archive_flagged_not_deleted(cache_dir: Path) -> None:
    now = time.time()
    archive = _archive_file(cache_dir, size_bytes=5000)
    env = _make_env(cache_dir, "a-1", size_bytes=1000, age_days=1, now=now)

    # Budget below the archive alone: every env gets evicted but the archive stays and
    # the residual is flagged (never a silent over-budget, never a wheel delete).
    result = mcp_cache.prune_uv_cache(cache_dir, max_bytes=100, max_age_days=999, now=now)

    assert archive.exists(), "the shared wheel archive is never deleted by the boot prune"
    assert not env.exists()
    assert result.over_budget_residual is True


def test_dry_run_mutates_nothing(cache_dir: Path) -> None:
    now = time.time()
    old = _make_env(cache_dir, "server-old", size_bytes=100, age_days=30, now=now)

    result = mcp_cache.prune_uv_cache(
        cache_dir, max_bytes=10, max_age_days=14, now=now, dry_run=True
    )

    assert old.exists(), "dry-run must delete nothing"
    assert result.dry_run is True
    assert result.count >= 1, "dry-run still reports the plan"
    assert result.bytes_freed >= 100


def test_boot_prune_skips_when_peer_alive(cache_dir: Path) -> None:
    now = time.time()
    old = _make_env(cache_dir, "server-old", size_bytes=100, age_days=99, now=now)

    peer = mcp_cache and object()  # any truthy peer token
    result = mcp_cache.boot_prune_mcp_cache(
        cache_dir=cache_dir,
        others_alive=lambda: [peer],
        now=now,
    )

    assert result is None, "prune must be skipped when a peer is alive (never mid-session)"
    assert old.exists(), "nothing is deleted while a peer is alive"


def test_boot_prune_runs_when_no_peers(cache_dir: Path, monkeypatch) -> None:
    now = time.time()
    monkeypatch.setenv("CLIO_MCP_CACHE_MAX_AGE_DAYS", "14")
    monkeypatch.setenv("CLIO_MCP_CACHE_MAX_BYTES", str(10**9))
    old = _make_env(cache_dir, "server-old", size_bytes=100, age_days=30, now=now)
    new = _make_env(cache_dir, "server-new", size_bytes=100, age_days=1, now=now)

    result = mcp_cache.boot_prune_mcp_cache(
        cache_dir=cache_dir,
        others_alive=lambda: [],
        now=now,
    )

    assert result is not None and result.existed
    assert not old.exists()
    assert new.exists()


def test_invalid_bounds_fall_back_to_default(monkeypatch) -> None:
    monkeypatch.setenv("CLIO_MCP_CACHE_MAX_BYTES", "-5")
    monkeypatch.setenv("CLIO_MCP_CACHE_MAX_AGE_DAYS", "0")
    from clio_agent import conf

    conf.reload()
    assert mcp_cache.mcp_cache_max_bytes() == 2 * 1024**3
    assert mcp_cache.mcp_cache_max_age_days() == 14.0
