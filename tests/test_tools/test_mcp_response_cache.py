"""Server cache hints: ttlMs/cacheScope honored client-side (#1285, C1-S5 item 3).

The mcp SDK already implements the full SEP-2549 machinery (``mcp/client/
caching.py::ClientResponseCache`` -- TTL, scope, the 24h clamp, eviction on
listChanged/resourceUpdated notifications) and fastmcp already exposes
server-side hints via ``FastMCP(cache_ttl=..., cache_scope=...)``
(``fastmcp/server/caching.py``) -- verified by reading both directly, not
duplicated here. What CLIO owns: (1) actually passing ``cache=`` when building
an execution-path client (``tools/mcp_runtime.py::make_mcp_client`` -- before
this slice, zero clio_agent code anywhere set ``cache=``, so every client left
the SDK's caching entirely inert regardless of what a server hinted), and
(2) an exerciser arm that ACTUALLY sets a cache hint so the mechanism is
provable end to end, not just read from source.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from fastmcp import Client
from mcp.client.caching import CacheConfig, CacheEntry, CacheKey

from clio_agent.tools.mcp_runtime import make_mcp_client, response_cache_enabled
from tests.test_tools.mcp_exerciser import build_exerciser_server


@dataclass
class _RecordingStore:
    """A ResponseCacheStore that counts get/set calls to prove a hit avoided a re-fetch."""

    entries: dict[CacheKey, CacheEntry] = field(default_factory=dict)
    get_calls: int = 0
    set_calls: int = 0

    async def get(self, key: CacheKey) -> CacheEntry | None:
        self.get_calls += 1
        return self.entries.get(key)

    async def set(self, key: CacheKey, entry: CacheEntry) -> None:
        self.set_calls += 1
        self.entries[key] = entry

    async def delete(self, key: CacheKey) -> None:
        self.entries.pop(key, None)

    async def clear(self) -> None:
        self.entries.clear()


@pytest.mark.asyncio
async def test_cache_hinted_server_second_list_tools_is_served_from_cache() -> None:
    server = build_exerciser_server(cache_ttl=60, cache_scope="private")
    store = _RecordingStore()
    async with Client(server, cache=CacheConfig(store=store, partition="test", target_id="exerciser")) as client:
        first = await client.list_tools()
        second = await client.list_tools()

    assert [t.name for t in first] == [t.name for t in second]
    assert store.set_calls == 1, "the second list_tools() must be served from cache, not re-fetched"
    assert store.get_calls >= 2, "both calls must at least check the cache"


@pytest.mark.asyncio
async def test_uncached_server_never_populates_the_store() -> None:
    """Regression guard: cache_ttl=None (the pre-#1285-item-3 default) means
    fastmcp emits no hint, so nothing gets cached even with cache= enabled."""

    server = build_exerciser_server()  # no cache_ttl
    store = _RecordingStore()
    async with Client(server, cache=CacheConfig(store=store, partition="test", target_id="exerciser")) as client:
        await client.list_tools()
        await client.list_tools()

    assert store.set_calls == 0


def test_response_cache_enabled_defaults_false() -> None:
    assert response_cache_enabled() is False


def test_response_cache_enabled_respects_config(monkeypatch) -> None:
    monkeypatch.setenv("CLIO_MCP_RESPONSE_CACHE_ENABLED", "true")
    from clio_agent import conf

    conf.reload()
    try:
        assert response_cache_enabled() is True
    finally:
        conf.reload()


class _FakeClient:
    """Records the kwargs make_mcp_client would pass to a real fastmcp Client."""

    def __init__(self, target: Any, **kwargs: Any) -> None:
        self.target = target
        self.kwargs = kwargs


def test_make_mcp_client_omits_cache_by_default() -> None:
    client = make_mcp_client(build_exerciser_server(), client_cls=_FakeClient)
    assert "cache" not in client.kwargs


def test_make_mcp_client_passes_cache_true_when_enabled(monkeypatch) -> None:
    monkeypatch.setenv("CLIO_MCP_RESPONSE_CACHE_ENABLED", "true")
    from clio_agent import conf

    conf.reload()
    try:
        client = make_mcp_client(build_exerciser_server(), client_cls=_FakeClient)
        assert client.kwargs.get("cache") is True
    finally:
        conf.reload()
