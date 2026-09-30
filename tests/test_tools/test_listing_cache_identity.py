"""A clio-kit server's cached listing is valid exactly while its code is unchanged.

The TTL was the only invalidation for a clio-kit server: nothing local changes when
its served code does. clio-kit names that code (``clio-kit mcp-server-identity``:
the hash of the server's embedded source and lock), so a listing stored with it is
reused at any age while the identity matches and dropped the moment it differs.
A launcher that cannot say keeps the TTL, logged typed.
"""

from __future__ import annotations

import time

import pytest
from mcp.types import Tool

from clio_agent.tools import listing_cache

TOOL = Tool(name="geocode", description="stub", inputSchema={"type": "object"})


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(listing_cache, "_cache_path", lambda: tmp_path / "cache.json")
    listing_cache._IDENTITIES.clear()
    yield
    listing_cache._IDENTITIES.clear()


def _clio_kit(tmp_path) -> str:
    launcher = tmp_path / "clio-kit.EXE"
    launcher.write_text("launcher")
    return str(launcher)


def _serving(monkeypatch, identity: str | None) -> list[tuple[str, ...]]:
    asked: list[tuple[str, ...]] = []

    def ask(command: str, name: str) -> str | None:
        asked.append((command, name))
        return identity

    monkeypatch.setattr(listing_cache, "_ask_clio_kit_identity", ask)
    return asked


def _age(days: float) -> None:
    entries = listing_cache._load()
    for entry in entries.values():
        entry["listed_at"] = time.time() - days * 86400
    listing_cache._save(entries)


def test_an_unchanged_server_reuses_its_listing_at_any_age(tmp_path, monkeypatch) -> None:
    cmd = _clio_kit(tmp_path)
    _serving(monkeypatch, "code-v1")
    listing_cache.store_listing("geo", cmd, ("mcp-server", "geo"), [TOOL])
    _age(30)

    assert listing_cache.load_listing("geo", cmd, ("mcp-server", "geo")) == [TOOL]


def test_a_changed_server_drops_its_listing_at_once(tmp_path, monkeypatch) -> None:
    cmd = _clio_kit(tmp_path)
    _serving(monkeypatch, "code-v1")
    listing_cache.store_listing("geo", cmd, ("mcp-server", "geo"), [TOOL])
    listing_cache._IDENTITIES.clear()
    _serving(monkeypatch, "code-v2")

    assert listing_cache.load_listing("geo", cmd, ("mcp-server", "geo")) is None
    assert listing_cache._load() == {}


def test_the_identity_is_asked_once_per_server(tmp_path, monkeypatch) -> None:
    cmd = _clio_kit(tmp_path)
    asked = _serving(monkeypatch, "code-v1")
    listing_cache.store_listing("geo", cmd, ("mcp-server", "geo"), [TOOL])
    for _ in range(3):
        listing_cache.load_listing("geo", cmd, ("mcp-server", "geo"))

    assert asked == [(cmd, "geo")]


def test_a_clio_kit_without_the_command_keeps_the_ttl(tmp_path, monkeypatch) -> None:
    cmd = _clio_kit(tmp_path)
    _serving(monkeypatch, None)
    listing_cache.store_listing("geo", cmd, ("mcp-server", "geo"), [TOOL])
    _age(2)

    assert listing_cache.load_listing("geo", cmd, ("mcp-server", "geo")) is None


def test_other_launchers_are_never_asked(tmp_path, monkeypatch) -> None:
    other = tmp_path / "uvx.exe"
    other.write_text("x")
    asked = _serving(monkeypatch, "code-v1")
    listing_cache.store_listing("w", str(other), ("weather-mcp",), [TOOL])

    assert listing_cache.load_listing("w", str(other), ("weather-mcp",)) == [TOOL]
    assert asked == []


def test_pruning_keeps_an_exact_entry(tmp_path, monkeypatch) -> None:
    cmd = _clio_kit(tmp_path)
    _serving(monkeypatch, "code-v1")
    listing_cache.store_listing("geo", cmd, ("mcp-server", "geo"), [TOOL])
    _age(30)
    other = tmp_path / "other.exe"
    other.write_text("x")
    listing_cache.store_listing("w", str(other), ("serve",), [TOOL])  # prunes aged entries

    assert listing_cache.load_listing("geo", cmd, ("mcp-server", "geo")) == [TOOL]
