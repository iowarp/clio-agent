"""Tests for the clio-core context-plane MCP server (epic #667).

In-memory via ``Client(server)`` (RULE 8) over a LocalFS store: an expert publishes
context under a scope and another discovers/retrieves it — the blackboard primitive.
"""

from __future__ import annotations

import json

import pytest
from fastmcp import Client

from clio_agent.arc.storage import make_arc_store
from clio_agent.tools.servers.clio_core_server import build_clio_core_server


def _parse(result: object) -> dict:
    data = getattr(result, "data", result)
    if isinstance(data, dict):
        return data
    if isinstance(data, str):
        return json.loads(data)
    raise AssertionError(f"unexpected result type: {type(data)!r}")


def _server(tmp_path):
    return build_clio_core_server(make_arc_store(backend="local", data_dir=str(tmp_path)))


@pytest.mark.asyncio
async def test_publish_list_get_drop(tmp_path):
    async with Client(_server(tmp_path)) as client:
        r = _parse(await client.call_tool(
            "context_publish",
            {"scope": "agentA", "name": "finding1", "content": "HDF5 dataset chunk compression"},
        ))
        assert r["published"] is True and r["bytes"] > 0

        r = _parse(await client.call_tool("context_list", {"scope": "agentA"}))
        assert r["names"] == ["finding1"] and r["count"] == 1

        r = _parse(await client.call_tool("context_get", {"scope": "agentA", "name": "finding1"}))
        assert r["found"] is True and "HDF5" in r["content"]

        r = _parse(await client.call_tool("context_drop", {"scope": "agentA", "name": "finding1"}))
        assert r["dropped"] is True

        r = _parse(await client.call_tool("context_get", {"scope": "agentA", "name": "finding1"}))
        assert r["found"] is False


@pytest.mark.asyncio
async def test_search_discovers_the_right_record(tmp_path):
    async with Client(_server(tmp_path)) as client:
        await client.call_tool("context_publish", {
            "scope": "agentA", "name": "hdf5",
            "content": "HDF5 dataset chunk sizes compression filters and shapes",
        })
        await client.call_tool("context_publish", {
            "scope": "agentA", "name": "seismic",
            "content": "earthquake waveform catalog station magnitude and epicenter",
        })
        r = _parse(await client.call_tool(
            "context_search", {"scope": "agentA", "query": "HDF5 compression filters", "k": 3}
        ))
        assert r["hits"] and r["hits"][0]["name"] == "hdf5"


@pytest.mark.asyncio
async def test_scopes_are_isolated(tmp_path):
    async with Client(_server(tmp_path)) as client:
        await client.call_tool("context_publish", {"scope": "agentA", "name": "x", "content": "alpha"})
        await client.call_tool("context_publish", {"scope": "agentB", "name": "y", "content": "beta"})
        ra = _parse(await client.call_tool("context_list", {"scope": "agentA"}))
        rb = _parse(await client.call_tool("context_list", {"scope": "agentB"}))
        assert ra["names"] == ["x"]
        assert rb["names"] == ["y"]


@pytest.mark.asyncio
async def test_get_missing_is_clean(tmp_path):
    async with Client(_server(tmp_path)) as client:
        r = _parse(await client.call_tool("context_get", {"scope": "agentA", "name": "nope"}))
        assert r["found"] is False


@pytest.mark.asyncio
async def test_separator_injection_is_rejected(tmp_path):
    """('a','b::c') and ('a::b','c') must not alias to the same key — the reserved
    separator is rejected on write so a publish can't collide with another scope."""
    async with Client(_server(tmp_path)) as client:
        await client.call_tool("context_publish", {"scope": "agentA", "name": "ok", "content": "x"})
        with pytest.raises(Exception, match="reserved separator"):
            await client.call_tool(
                "context_publish", {"scope": "agentA", "name": "b::c", "content": "y"}
            )
        with pytest.raises(Exception, match="reserved separator"):
            await client.call_tool(
                "context_publish", {"scope": "agentA::b", "name": "c", "content": "z"}
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bm25_search_on_real_cte():
    """The clio-core MCP over REAL clio-core CTE: semantic BM25 ranking, not the LocalFS
    word-overlap fallback the other tests use."""
    store = make_arc_store(backend="cte")
    server = build_clio_core_server(store)
    try:
        async with Client(server) as client:
            await client.call_tool("context_publish", {
                "scope": "clio_core_it", "name": "hdf5",
                "content": "HDF5 dataset chunk sizes compression filters and dataset shapes",
            })
            await client.call_tool("context_publish", {
                "scope": "clio_core_it", "name": "seismic",
                "content": "earthquake waveform catalog station picks magnitude and epicenter",
            })
            r = _parse(await client.call_tool(
                "context_search", {"scope": "clio_core_it", "query": "HDF5 compression filters", "k": 3}
            ))
            assert r["semantic"] is True  # real BM25 on CTE, not word-overlap
            assert r["hits"] and r["hits"][0]["name"] == "hdf5"
    finally:
        store.delete("context", "clio_core_it::hdf5")
        store.delete("context", "clio_core_it::seismic")
