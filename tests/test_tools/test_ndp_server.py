"""Tests for the clio-kit-backed NDP MCP wrapper."""

from __future__ import annotations

import json
from importlib import import_module
from typing import Any

import pytest
from fastmcp import Client

from clio_agent.tools.gateway import gateway
from clio_agent.tools.servers.ndp_server import ndp_server

gateway_module = import_module("clio_agent.tools.gateway")
ndp_module = import_module("clio_agent.tools.servers.ndp_server")


def _parse_result(result: Any) -> dict[str, Any]:
    data = result.data
    if isinstance(data, dict):
        return data
    if isinstance(data, str):
        return json.loads(data)
    return {"raw": str(data)}


@pytest.mark.asyncio
async def test_ndp_server_lists_organizations_through_clio_kit(monkeypatch: pytest.MonkeyPatch):
    """NDP wrapper should pass exact args through to the clio-kit MCP server."""
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_call(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, args))
        return {
            "organizations": ["noaa-global-systems-laboratory"],
            "count": 1,
            "server": "global",
            "name_filter": "noaa",
            "_meta": {"tool": "list_organizations", "status": "success"},
        }

    monkeypatch.setattr(ndp_module, "_call_clio_kit_ndp_tool", fake_call)

    async with Client(ndp_server) as client:
        result = await client.call_tool(
            "list_organizations",
            {"name_filter": "noaa", "server": "global"},
        )

    assert calls == [
        ("list_organizations", {"name_filter": "noaa", "server": "global"})
    ]
    data = _parse_result(result)
    assert data["organizations"] == ["noaa-global-systems-laboratory"]
    assert data["_meta"]["status"] == "success"


@pytest.mark.asyncio
async def test_ndp_search_omits_null_arguments(monkeypatch: pytest.MonkeyPatch):
    """The wrapper should not forward null filters that change clio-kit semantics."""
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_call(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, args))
        return {"datasets": [], "count": 0, "server": "global"}

    monkeypatch.setattr(ndp_module, "_call_clio_kit_ndp_tool", fake_call)

    async with Client(ndp_server) as client:
        await client.call_tool(
            "search_datasets",
            {"search_terms": ["climate"], "server": "global", "limit": 3},
        )

    assert calls == [
        (
            "search_datasets",
            {"search_terms": ["climate"], "server": "global", "limit": 3},
        )
    ]


@pytest.mark.asyncio
async def test_gateway_exposes_ndp_tools(monkeypatch: pytest.MonkeyPatch):
    """NDP tools should be visible through CLIO's normal gateway surface."""
    async def fake_call(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {"organizations": [], "count": 0, "server": "global", "tool": tool_name}

    monkeypatch.setattr(ndp_module, "_call_clio_kit_ndp_tool", fake_call)

    async with Client(gateway) as client:
        tools = await client.list_tools()
        result = await client.call_tool("ndp_list_organizations", {"server": "global"})

    assert {
        "ndp_list_organizations",
        "ndp_search_datasets",
        "ndp_get_dataset_details",
        "ndp_stage_resource",
    } <= {
        tool.name for tool in tools
    }
    assert _parse_result(result)["server"] == "global"


@pytest.mark.asyncio
async def test_ndp_details_falls_back_to_public_ckan(monkeypatch: pytest.MonkeyPatch):
    """Global id/name details should use CKAN directly instead of brittle detail lookup."""

    async def fake_call(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError(f"unexpected clio-kit call: {tool_name} {args}")

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "success": True,
                "result": {
                    "id": "ds1",
                    "name": "salton-sea-seismic-data",
                    "title": "Salton Sea Seismic Data",
                    "owner_org": "ucr",
                    "notes": "MiniSEED waveform data",
                    "resources": [
                        {
                            "name": "Salton Sea Seismic Waveforms",
                            "url": "osdf:///ndp/public/ucr_seis/Data_Salton",
                        }
                    ],
                },
            }

    monkeypatch.setattr(ndp_module, "_call_clio_kit_ndp_tool", fake_call)
    monkeypatch.setattr(ndp_module.requests, "get", lambda *args, **kwargs: FakeResponse())

    async with Client(ndp_server) as client:
        result = await client.call_tool(
            "get_dataset_details",
            {
                "dataset_identifier": "salton-sea-seismic-data",
                "identifier_type": "name",
                "server": "global",
            },
        )

    data = _parse_result(result)
    assert data["title"] == "Salton Sea Seismic Data"
    assert data["resource_urls"] == ["osdf:///ndp/public/ucr_seis/Data_Salton"]
    assert data["_meta"]["source"] == "ckan_package_show"


@pytest.mark.asyncio
async def test_ndp_stage_resource_surfaces_osdf_transport(monkeypatch: pytest.MonkeyPatch):
    """OSDF resources should fail visibly until a Pelican-backed stage tool exists."""

    async def fake_details(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "id": "ds1",
            "name": "salton-sea-seismic-data",
            "title": "Salton Sea Seismic Data",
            "resources": [
                {
                    "name": "Salton Sea Seismic Waveforms",
                    "url": "osdf:///ndp/public/ucr_seis/Data_Salton",
                }
            ],
        }

    monkeypatch.setattr(ndp_module, "_dataset_details", fake_details)

    async with Client(ndp_server) as client:
        result = await client.call_tool(
            "stage_resource",
            {"dataset_identifier": "ds1", "server": "global"},
        )

    data = _parse_result(result)
    assert data["error"]["code"] == "unsupported_resource_transport"
    assert data["error"]["details"]["transport"] == "osdf"


def test_list_capabilities_reports_ndp_server():
    """Context capability summaries should identify the NDP server owner."""
    caps = gateway_module.list_capabilities()

    ndp_caps = [cap for cap in caps if cap["name"].startswith("ndp_")]
    assert ndp_caps
    assert {cap["server"] for cap in ndp_caps} == {"ndp"}
