"""Listing-cache validity mechanics (#942): TTL, fingerprint, env keying, drops.

The live-spawn integration (sequential bound, cached zero-spawn boot,
round-trip equality incl. the aliased ``meta`` field) is pinned in
test_mcp_fleet_lifecycle.py; these are the pure cache-contract tests.
"""

from __future__ import annotations

import json
import time

import pytest
from mcp.types import Tool

from clio_agent.tools import listing_cache

TOOL = Tool(
    name="echo",
    description="stub",
    inputSchema={"type": "object", "properties": {"text": {"type": "string"}}},
)


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(listing_cache, "_cache_path", lambda: tmp_path / "cache.json")
    yield


def _launcher(tmp_path):
    launcher = tmp_path / "launcher.exe"
    launcher.write_text("launcher")
    return str(launcher)


def test_store_then_load_roundtrip(tmp_path) -> None:
    cmd = _launcher(tmp_path)
    listing_cache.store_listing("ns", cmd, ("serve",), [TOOL])
    loaded = listing_cache.load_listing("ns", cmd, ("serve",))
    assert loaded is not None and len(loaded) == 1
    assert loaded[0] == TOOL


def test_meta_survives_the_roundtrip(tmp_path) -> None:
    """Tool.meta is aliased ``_meta`` — a dump without by_alias silently drops
    it (and every MCP tag riding on it). Load-bearing regression lock."""

    tagged = TOOL.model_copy(update={"meta": {"fastmcp": {"tags": ["a", "b"]}}})
    cmd = _launcher(tmp_path)
    listing_cache.store_listing("ns", cmd, ("serve",), [tagged])
    loaded = listing_cache.load_listing("ns", cmd, ("serve",))
    assert loaded is not None
    assert loaded[0].meta == {"fastmcp": {"tags": ["a", "b"]}}


def test_expired_entry_is_dropped_typed(tmp_path, monkeypatch) -> None:
    cmd = _launcher(tmp_path)
    listing_cache.store_listing("ns", cmd, ("serve",), [TOOL])
    two_days = time.time() - 48 * 3600
    entries = listing_cache._load()
    (key,) = entries
    entries[key]["listed_at"] = two_days
    listing_cache._save(entries)
    assert listing_cache.load_listing("ns", cmd, ("serve",)) is None
    assert listing_cache._load() == {}, "expired entry must leave the cache"


def test_launcher_change_invalidates(tmp_path) -> None:
    cmd = _launcher(tmp_path)
    listing_cache.store_listing("ns", cmd, ("serve",), [TOOL])
    # Grow the launcher binary — fingerprint (size:mtime) moves.
    with open(cmd, "a", encoding="utf-8") as fh:
        fh.write("moved")
    assert listing_cache.load_listing("ns", cmd, ("serve",)) is None
    assert listing_cache._load() == {}


def test_env_separates_entries(tmp_path) -> None:
    """Same argv, different declared env → distinct entries; env is hashed,
    never stored verbatim (it may carry secrets)."""

    cmd = _launcher(tmp_path)
    listing_cache.store_listing("ns", cmd, ("serve",), [TOOL], env={"API_KEY": "sekrit"})
    assert listing_cache.load_listing("ns", cmd, ("serve",)) is None, "env-less lookup must miss"
    assert (
        listing_cache.load_listing("ns", cmd, ("serve",), env={"API_KEY": "other"}) is None
    ), "different env must miss"
    hit = listing_cache.load_listing("ns", cmd, ("serve",), env={"API_KEY": "sekrit"})
    assert hit is not None
    raw = listing_cache._cache_path().read_text(encoding="utf-8")
    assert "sekrit" not in raw, "env values must never be persisted"


def test_unresolvable_launcher_stores_nothing(tmp_path) -> None:
    listing_cache.store_listing("ns", str(tmp_path / "missing.exe"), ("serve",), [TOOL])
    assert listing_cache._load() == {}


def test_malformed_cache_file_is_typed_not_fatal(tmp_path) -> None:
    listing_cache._cache_path().write_text("{nope")
    assert listing_cache.load_listing("ns", "cmd", ("serve",)) is None


def test_schema_mismatch_drops(tmp_path) -> None:
    listing_cache._cache_path().write_text(
        json.dumps({"schema": "clio-agent.mcp-listing-cache.v0", "entries": {"k": {}}})
    )
    assert listing_cache._load() == {}


def test_store_prunes_expired_entries(tmp_path) -> None:
    """The cache must not grow monotonically (test runs key unique tmp paths)."""

    cmd = _launcher(tmp_path)
    listing_cache.store_listing("old", cmd, ("old",), [TOOL])
    entries = listing_cache._load()
    (key,) = entries
    entries[key]["listed_at"] = time.time() - 48 * 3600
    listing_cache._save(entries)
    listing_cache.store_listing("new", cmd, ("new",), [TOOL])
    remaining = listing_cache._load()
    assert len(remaining) == 1, "expired entries must be pruned on store"
    (entry,) = remaining.values()
    assert entry["namespace"] == "new"
