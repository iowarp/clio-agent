"""Tests for the clio-kit-backed NDP MCP wrapper."""

from __future__ import annotations

import json
import subprocess
from importlib import import_module
from pathlib import Path
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


def test_clio_kit_transport_prefers_local_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / "clio-kit"
    checkout.mkdir()
    monkeypatch.setenv("CLIO_KIT_PATH", str(checkout))
    monkeypatch.delenv("CLIO_KIT_COMMAND", raising=False)

    transport = ndp_module._clio_kit_transport("ndp")

    assert transport.command == "uv"
    assert transport.args == [
        "--directory",
        str(checkout.resolve()),
        "run",
        "clio-kit",
        "mcp-server",
        "ndp",
    ]


def test_clio_kit_transport_uses_explicit_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIO_KIT_PATH", str(tmp_path / "missing"))
    monkeypatch.setenv("CLIO_KIT_COMMAND", "uv --directory /opt/clio-kit run clio-kit")

    transport = ndp_module._clio_kit_transport("ndp")

    assert transport.command == "uv"
    assert transport.args == [
        "--directory",
        "/opt/clio-kit",
        "run",
        "clio-kit",
        "mcp-server",
        "ndp",
    ]


def test_clio_kit_transport_uses_path_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIO_KIT_PATH", str(tmp_path / "missing"))
    monkeypatch.delenv("CLIO_KIT_COMMAND", raising=False)
    monkeypatch.setattr(ndp_module.shutil, "which", lambda name: "/usr/bin/clio-kit")

    transport = ndp_module._clio_kit_transport("ndp")

    assert transport.command == "/usr/bin/clio-kit"
    assert transport.args == ["mcp-server", "ndp"]


def test_clio_kit_transport_falls_back_to_uvx(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIO_KIT_PATH", str(tmp_path / "missing"))
    monkeypatch.delenv("CLIO_KIT_COMMAND", raising=False)
    monkeypatch.setattr(ndp_module.shutil, "which", lambda name: None)

    transport = ndp_module._clio_kit_transport("ndp")

    assert transport.command == "uvx"
    assert transport.args == ["--from", "clio-kit", "clio-kit", "mcp-server", "ndp"]


def test_clio_kit_launcher_source_requires_uvx_opt_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIO_KIT_PATH", str(tmp_path / "missing"))
    monkeypatch.delenv("CLIO_KIT_COMMAND", raising=False)
    monkeypatch.delenv("CLIO_KIT_ALLOW_UVX", raising=False)
    monkeypatch.setattr(ndp_module.shutil, "which", lambda name: None)

    assert ndp_module._clio_kit_launcher_source() == ""
    assert ndp_module._should_try_clio_kit("global") is False
    assert ndp_module._should_try_clio_kit("local") is True

    monkeypatch.setenv("CLIO_KIT_ALLOW_UVX", "1")

    assert ndp_module._clio_kit_launcher_source() == "uvx"
    assert ndp_module._should_try_clio_kit("global") is True


@pytest.mark.asyncio
async def test_ndp_server_lists_organizations_through_clio_kit(monkeypatch: pytest.MonkeyPatch):
    """NDP wrapper should pass exact args through to the clio-kit MCP server."""
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setenv("CLIO_KIT_COMMAND", "clio-kit")

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

    assert calls == [("list_organizations", {"name_filter": "noaa", "server": "global"})]
    data = _parse_result(result)
    assert data["organizations"] == ["noaa-global-systems-laboratory"]
    assert data["_meta"]["status"] == "success"


@pytest.mark.asyncio
async def test_ndp_list_organizations_uses_ckan_without_uvx_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("CLIO_KIT_PATH", str(tmp_path / "missing"))
    monkeypatch.delenv("CLIO_KIT_COMMAND", raising=False)
    monkeypatch.delenv("CLIO_KIT_ALLOW_UVX", raising=False)
    monkeypatch.setattr(ndp_module.shutil, "which", lambda name: None)

    async def fail_call(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError(f"unexpected clio-kit call: {tool_name} {args}")

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "success": True,
                "result": [
                    {
                        "id": "org1",
                        "name": "noaa-global-systems-laboratory",
                        "title": "NOAA Global Systems Laboratory",
                        "package_count": 3,
                    }
                ],
            }

    monkeypatch.setattr(ndp_module, "_call_clio_kit_ndp_tool", fail_call)
    monkeypatch.setattr(ndp_module.requests, "get", lambda *args, **kwargs: FakeResponse())

    async with Client(ndp_server) as client:
        result = await client.call_tool(
            "list_organizations",
            {"name_filter": "noaa", "server": "global"},
        )

    data = _parse_result(result)
    assert data["organizations"][0]["name"] == "noaa-global-systems-laboratory"
    assert data["_meta"]["source"] == "ckan_organization_list"
    assert data["_meta"]["clio_kit_skipped"] is True
    assert "uvx package launch" in data["_meta"]["clio_kit_skip_reason"]


@pytest.mark.asyncio
async def test_ndp_list_organizations_falls_back_to_ckan(
    monkeypatch: pytest.MonkeyPatch,
):
    """Fresh installs should still list global organizations if clio-kit fails."""
    monkeypatch.setenv("CLIO_KIT_COMMAND", "clio-kit")

    async def fake_call(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "error": {
                "type": "tool_error",
                "code": "clio_kit_ndp_unavailable",
                "message": "Connection closed",
                "details": {"tool": tool_name, "args": args},
            }
        }

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "success": True,
                "result": [
                    {
                        "id": "org1",
                        "name": "noaa-global-systems-laboratory",
                        "title": "NOAA Global Systems Laboratory",
                        "package_count": 3,
                    },
                    {
                        "id": "org2",
                        "name": "ucr-earth-and-planetary-sciences",
                        "title": "UCR Earth and Planetary Sciences",
                        "package_count": 1,
                    },
                ],
            }

    monkeypatch.setattr(ndp_module, "_call_clio_kit_ndp_tool", fake_call)
    monkeypatch.setattr(ndp_module.requests, "get", lambda *args, **kwargs: FakeResponse())

    async with Client(ndp_server) as client:
        result = await client.call_tool(
            "list_organizations",
            {"name_filter": "noaa", "server": "global"},
        )

    data = _parse_result(result)
    assert data["organizations"] == [
        {
            "id": "org1",
            "name": "noaa-global-systems-laboratory",
            "title": "NOAA Global Systems Laboratory",
            "package_count": 3,
        }
    ]
    assert data["_meta"]["source"] == "ckan_organization_list"
    assert data["_meta"]["clio_kit_error"]["code"] == "clio_kit_ndp_unavailable"


@pytest.mark.asyncio
async def test_ndp_search_omits_null_arguments(monkeypatch: pytest.MonkeyPatch):
    """The wrapper should not forward null filters that change search semantics."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setenv("CLIO_KIT_COMMAND", "clio-kit")

    async def fake_call(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError(f"unexpected clio-kit call: {tool_name} {args}")

    monkeypatch.setattr(ndp_module, "_call_clio_kit_ndp_tool", fake_call)

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"success": True, "result": {"count": 0, "results": []}}

    def fake_get(*args: Any, **kwargs: Any) -> FakeResponse:
        calls.append(kwargs.get("params") or {})
        return FakeResponse()

    monkeypatch.setattr(ndp_module.requests, "get", fake_get)

    async with Client(ndp_server) as client:
        await client.call_tool(
            "search_datasets",
            {"search_terms": ["climate"], "server": "global", "limit": 3},
        )

    assert calls == [{"q": "climate", "rows": 3}]


@pytest.mark.asyncio
async def test_ndp_search_reports_earthscope_search_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Search results should expose whether broad EarthScope catalog coverage happened."""
    monkeypatch.setenv("CLIO_KIT_COMMAND", "clio-kit")

    async def fake_call(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError(f"unexpected clio-kit call: {tool_name} {args}")

    monkeypatch.setattr(ndp_module, "_call_clio_kit_ndp_tool", fake_call)

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"success": True, "result": {"count": 0, "results": []}}

    monkeypatch.setattr(ndp_module.requests, "get", lambda *args, **kwargs: FakeResponse())

    async with Client(ndp_server) as client:
        narrow = await client.call_tool(
            "search_datasets",
            {"search_terms": ["GNSS", "station", "time-series"], "server": "global"},
        )
        broad = await client.call_tool(
            "search_datasets",
            {
                "search_terms": ["EarthScope", "GNSS", "GPS", "CSV"],
                "server": "global",
            },
        )
        pbo = await client.call_tool(
            "search_datasets",
            {
                "search_terms": ["EarthScope", "GNSS", "PBO", "station", "CSV", "raw_csv"],
                "server": "global",
            },
        )

    narrow_data = _parse_result(narrow)
    broad_data = _parse_result(broad)
    pbo_data = _parse_result(pbo)
    assert narrow_data["search_coverage"]["status"] == "incomplete"
    assert "EarthScope" in narrow_data["search_coverage"]["next_action"]
    assert broad_data["search_coverage"]["status"] == "covered"
    assert broad_data["search_coverage"]["broad_station_catalog_searched"] is True
    assert pbo_data["search_coverage"]["status"] == "covered"
    assert pbo_data["search_coverage"]["station_code"] is None


@pytest.mark.asyncio
async def test_ndp_search_marks_station_resource_name_csv_as_covered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Station-id resource lookup is valid coverage after station catalog filtering."""
    monkeypatch.setenv("CLIO_KIT_COMMAND", "clio-kit")

    async def fake_call(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError(f"unexpected clio-kit call: {tool_name} {args}")

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "success": True,
                "result": {
                    "count": 1,
                    "results": [
                        {
                            "id": "station-ds",
                            "name": "abcd-ci-ly-30",
                            "title": "ABCD.CI.LY_.30",
                            "resources": [
                                {
                                    "name": "ABCD.CI.LY_.30.csv",
                                    "format": "CSV",
                                    "url": "https://example.test/raw_csv/ABCD.CI.LY_.30.csv",
                                }
                            ],
                        }
                    ],
                },
            }

    monkeypatch.setattr(ndp_module, "_call_clio_kit_ndp_tool", fake_call)
    monkeypatch.setattr(ndp_module.requests, "get", lambda *args, **kwargs: FakeResponse())

    async with Client(ndp_server) as client:
        result = await client.call_tool(
            "search_datasets",
            {
                "resource_name": "ABCD",
                "resource_format": "CSV",
                "server": "global",
                "limit": 20,
            },
        )

    data = _parse_result(result)
    assert data["datasets"][0]["resource_names"] == ["ABCD.CI.LY_.30.csv"]
    assert data["search_coverage"]["status"] == "covered"
    assert data["search_coverage"]["station_resource_search"] is True
    assert data["search_coverage"]["broad_station_catalog_searched"] is False
    assert data["search_coverage"]["station_code"] == "ABCD"
    assert data["_meta"]["station_resource_filter"] == {
        "station_code": "ABCD",
        "input_count": 1,
        "output_count": 1,
        "status": "applied",
    }


@pytest.mark.asyncio
async def test_ndp_search_does_not_treat_generic_csv_resource_name_as_station(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generic CSV resource names must not be typed as station-resource coverage."""
    monkeypatch.setenv("CLIO_KIT_COMMAND", "clio-kit")

    async def fake_call(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError(f"unexpected clio-kit call: {tool_name} {args}")

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"success": True, "result": {"count": 0, "results": []}}

    monkeypatch.setattr(ndp_module, "_call_clio_kit_ndp_tool", fake_call)
    monkeypatch.setattr(ndp_module.requests, "get", lambda *args, **kwargs: FakeResponse())

    async with Client(ndp_server) as client:
        result = await client.call_tool(
            "search_datasets",
            {
                "resource_name": "earthscope_converted_data.csv",
                "resource_format": "CSV",
                "server": "global",
                "limit": 20,
            },
        )

    data = _parse_result(result)
    assert data["search_coverage"]["status"] == "incomplete"
    assert data["search_coverage"]["station_resource_search"] is False
    assert data["search_coverage"]["station_code"] is None
    assert "station_resource_filter" not in data["_meta"]


@pytest.mark.asyncio
async def test_ndp_search_filters_unrelated_earthscope_station_csv_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A precise station resource lookup must not surface another station's CSV."""
    monkeypatch.setenv("CLIO_KIT_COMMAND", "clio-kit")

    async def fake_call(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError(f"unexpected clio-kit call: {tool_name} {args}")

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "success": True,
                "result": {
                    "count": 2,
                    "results": [
                        {
                            "id": "wrong-station",
                            "name": "wwmt-ci-ly-40",
                            "title": "WWMT.CI.LY_.40",
                            "resources": [
                                {
                                    "name": "WWMT.CI.LY_.40.csv",
                                    "format": "CSV",
                                    "url": "https://example.test/raw_csv/WWMT.CI.LY_.40.csv",
                                }
                            ],
                        },
                        {
                            "id": "metadata",
                            "name": "earthscope_stations",
                            "title": "EarthScope Stations Dataset",
                            "resources": [
                                {
                                    "name": "earthscope_converted_data.csv",
                                    "format": "CSV",
                                    "url": "https://example.test/earthscope_converted_data.csv",
                                }
                            ],
                        },
                    ],
                },
            }

    monkeypatch.setattr(ndp_module, "_call_clio_kit_ndp_tool", fake_call)
    monkeypatch.setattr(ndp_module.requests, "get", lambda *args, **kwargs: FakeResponse())

    async with Client(ndp_server) as client:
        result = await client.call_tool(
            "search_datasets",
            {
                "resource_name": "UCSF.CI.LY",
                "resource_format": "CSV",
                "search_terms": ["UCSF.CI.LY", "EarthScope", "GNSS", "CSV"],
                "server": "global",
                "limit": 20,
            },
        )

    data = _parse_result(result)
    assert data["datasets"] == []
    assert data["count"] == 0
    assert data["total_found"] == 0
    assert data["search_coverage"]["station_resource_search"] is True
    assert data["search_coverage"]["station_code"] == "UCSF"
    assert data["_meta"]["station_resource_filter"] == {
        "station_code": "UCSF",
        "input_count": 2,
        "output_count": 0,
        "status": "applied",
    }


@pytest.mark.asyncio
async def test_ndp_search_suppresses_broad_coordinate_station_csv_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coordinate searches must not surface arbitrary EarthScope station CSVs."""
    monkeypatch.setenv("CLIO_KIT_COMMAND", "clio-kit")

    async def fake_call(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError(f"unexpected clio-kit call: {tool_name} {args}")

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "success": True,
                "result": {
                    "count": 2,
                    "results": [
                        {
                            "id": "metadata",
                            "name": "earthscope_stations",
                            "title": "EarthScope Stations Dataset",
                            "resources": [
                                {
                                    "name": "earthscope_converted_data.csv",
                                    "format": "CSV",
                                    "url": "https://example.test/earthscope_converted_data.csv",
                                }
                            ],
                        },
                        {
                            "id": "off-region-station",
                            "name": "yuhg-ci-ly-20",
                            "title": "YUHG.CI.LY_.20",
                            "resources": [
                                {
                                    "name": "YUHG.CI.LY_.20.csv",
                                    "format": "CSV",
                                    "url": "https://example.test/raw_csv/YUHG.CI.LY_.20.csv",
                                }
                            ],
                        },
                    ],
                },
            }

    monkeypatch.setattr(ndp_module, "_call_clio_kit_ndp_tool", fake_call)
    monkeypatch.setattr(ndp_module.requests, "get", lambda *args, **kwargs: FakeResponse())

    async with Client(ndp_server) as client:
        result = await client.call_tool(
            "search_datasets",
            {
                "search_terms": ["EarthScope", "GNSS", "CSV", "34.05", "-118.25"],
                "server": "global",
                "limit": 20,
            },
        )

    data = _parse_result(result)
    assert [row["name"] for row in data["datasets"]] == ["earthscope_stations"]
    assert data["count"] == 1
    assert data["total_found"] == 1
    assert data["_meta"]["station_resource_filter"] == {
        "input_count": 2,
        "output_count": 1,
        "status": "broad_station_resources_suppressed",
    }


@pytest.mark.asyncio
async def test_ndp_search_suppresses_broad_gnss_csv_station_results_without_station(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Broad GNSS CSV search should not offer arbitrary station CSVs for staging."""
    monkeypatch.setenv("CLIO_KIT_COMMAND", "clio-kit")

    async def fake_call(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError(f"unexpected clio-kit call: {tool_name} {args}")

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "success": True,
                "result": {
                    "count": 2,
                    "results": [
                        {
                            "id": "metadata",
                            "name": "earthscope_stations",
                            "title": "EarthScope Stations Dataset",
                            "resources": [
                                {
                                    "name": "earthscope_converted_data.csv",
                                    "format": "CSV",
                                    "url": "https://example.test/earthscope_converted_data.csv",
                                }
                            ],
                        },
                        {
                            "id": "broad-station",
                            "name": "wwmt-ci-ly-40",
                            "title": "WWMT.CI.LY_.40",
                            "resources": [
                                {
                                    "name": "WWMT.CI.LY_.40.csv",
                                    "format": "CSV",
                                    "url": "https://example.test/raw_csv/WWMT.CI.LY_.40.csv",
                                }
                            ],
                        },
                    ],
                },
            }

    monkeypatch.setattr(ndp_module, "_call_clio_kit_ndp_tool", fake_call)
    monkeypatch.setattr(ndp_module.requests, "get", lambda *args, **kwargs: FakeResponse())

    async with Client(ndp_server) as client:
        result = await client.call_tool(
            "search_datasets",
            {
                "search_terms": ["GNSS", "CSV"],
                "resource_format": "CSV",
                "server": "global",
                "limit": 20,
            },
        )

    data = _parse_result(result)
    assert [row["name"] for row in data["datasets"]] == ["earthscope_stations"]
    assert data["count"] == 1
    assert data["total_found"] == 1
    assert data["_meta"]["station_resource_filter"] == {
        "input_count": 2,
        "output_count": 1,
        "status": "broad_station_resources_suppressed",
    }
    assert data["search_coverage"]["status"] == "incomplete"


@pytest.mark.asyncio
async def test_ndp_search_uses_global_ckan_before_clio_kit(monkeypatch: pytest.MonkeyPatch):
    """Global dataset search should avoid brittle clio-kit launches when CKAN works."""
    monkeypatch.setenv("CLIO_KIT_COMMAND", "clio-kit")

    async def fake_call(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError(f"unexpected clio-kit call: {tool_name} {args}")

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "success": True,
                "result": {
                    "count": 1,
                    "results": [
                        {
                            "id": "ds1",
                            "name": "salton-sea-seismic-data",
                            "title": "Salton Sea Seismic Data",
                            "owner_org": "ucr-earth-and-planetary-sciences",
                            "notes": "MiniSEED waveform data",
                            "resources": [
                                {
                                    "name": "Salton Sea Seismic Waveforms",
                                    "url": "osdf:///ndp/public/ucr_seis/Data_Salton",
                                }
                            ],
                        }
                    ],
                },
            }

    calls: list[dict[str, Any]] = []

    def fake_get(*args: Any, **kwargs: Any) -> FakeResponse:
        calls.append(kwargs.get("params") or {})
        return FakeResponse()

    monkeypatch.setattr(ndp_module, "_call_clio_kit_ndp_tool", fake_call)
    monkeypatch.setattr(ndp_module.requests, "get", fake_get)

    async with Client(ndp_server) as client:
        result = await client.call_tool(
            "search_datasets",
            {"search_terms": ["seismic"], "server": "global", "limit": 3},
        )

    data = _parse_result(result)
    assert calls[0]["q"] == "seismic"
    assert calls[0]["rows"] == 3
    assert data["datasets"][0]["name"] == "salton-sea-seismic-data"
    assert data["datasets"][0]["resource_urls"] == ["osdf:///ndp/public/ucr_seis/Data_Salton"]
    assert data["_meta"]["source"] == "ckan_package_search"
    assert data["_meta"]["ckan_direct"] is True
    assert "clio_kit_fallback" not in data["_meta"]


@pytest.mark.asyncio
async def test_ndp_search_falls_back_to_clio_kit_if_ckan_fails(
    monkeypatch: pytest.MonkeyPatch,
):
    """Global search should still use clio-kit when public CKAN is unavailable."""
    monkeypatch.setenv("CLIO_KIT_COMMAND", "clio-kit")
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_get(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("ckan unavailable")

    async def fake_call(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, args))
        return {
            "datasets": [{"name": "fallback-dataset"}],
            "count": 1,
            "server": "global",
            "_meta": {"tool": tool_name, "status": "success"},
        }

    monkeypatch.setattr(ndp_module.requests, "get", fake_get)
    monkeypatch.setattr(ndp_module, "_call_clio_kit_ndp_tool", fake_call)

    async with Client(ndp_server) as client:
        result = await client.call_tool(
            "search_datasets",
            {"search_terms": ["seismic"], "server": "global", "limit": 3},
        )

    data = _parse_result(result)
    assert calls == [
        (
            "search_datasets",
            {"search_terms": ["seismic"], "server": "global", "limit": 3},
        )
    ]
    assert data["datasets"][0]["name"] == "fallback-dataset"
    assert data["search_coverage"]["status"] == "incomplete"


@pytest.mark.asyncio
async def test_ndp_search_uses_ckan_without_uvx_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("CLIO_KIT_PATH", str(tmp_path / "missing"))
    monkeypatch.delenv("CLIO_KIT_COMMAND", raising=False)
    monkeypatch.delenv("CLIO_KIT_ALLOW_UVX", raising=False)
    monkeypatch.setattr(ndp_module.shutil, "which", lambda name: None)

    async def fail_call(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError(f"unexpected clio-kit call: {tool_name} {args}")

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "success": True,
                "result": {
                    "count": 1,
                    "results": [
                        {
                            "id": "ds1",
                            "name": "salton-sea-seismic-data",
                            "title": "Salton Sea Seismic Data",
                            "owner_org": "ucr-earth-and-planetary-sciences",
                            "notes": "MiniSEED waveform data",
                            "resources": [
                                {
                                    "name": "Salton Sea Seismic Waveforms",
                                    "url": "osdf:///ndp/public/ucr_seis/Data_Salton",
                                }
                            ],
                        }
                    ],
                },
            }

    monkeypatch.setattr(ndp_module, "_call_clio_kit_ndp_tool", fail_call)
    monkeypatch.setattr(ndp_module.requests, "get", lambda *args, **kwargs: FakeResponse())

    async with Client(ndp_server) as client:
        result = await client.call_tool(
            "search_datasets",
            {"search_terms": ["seismic"], "server": "global", "limit": 3},
        )

    data = _parse_result(result)
    assert data["datasets"][0]["name"] == "salton-sea-seismic-data"
    assert data["_meta"]["source"] == "ckan_package_search"
    assert data["_meta"]["clio_kit_skipped"] is True
    assert "uvx package launch" in data["_meta"]["clio_kit_skip_reason"]


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
    } <= {tool.name for tool in tools}
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
                    ]
                    + [
                        {
                            "name": f"CIMIS Station #{index} Hourly Weather Data",
                            "format": "CSV",
                            "url": f"https://example.test/stations/{index}.csv",
                        }
                        for index in range(1, 19)
                    ]
                    + [
                        {
                            "name": "CIMIS Station #80 - Fresno State Hourly Weather Data",
                            "format": "CSV",
                            "url": "https://example.test/stations/80-fresnostate.csv",
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
    assert data["resource_urls"][0] == "osdf:///ndp/public/ucr_seis/Data_Salton"
    assert data["resource_summaries"][19] == {
        "index": 19,
        "name": "CIMIS Station #80 - Fresno State Hourly Weather Data",
        "format": "CSV",
        "url": "https://example.test/stations/80-fresnostate.csv",
    }
    assert data["resource_summaries_truncated"] is False
    assert data["_meta"]["source"] == "ckan_package_show"


@pytest.mark.asyncio
async def test_ndp_details_returns_structured_error_when_ckan_skip_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIO_KIT_PATH", str(tmp_path / "missing"))
    monkeypatch.delenv("CLIO_KIT_COMMAND", raising=False)
    monkeypatch.delenv("CLIO_KIT_ALLOW_UVX", raising=False)
    monkeypatch.setattr(ndp_module.shutil, "which", lambda name: None)

    async def fail_call(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError(f"unexpected clio-kit call: {tool_name} {args}")

    def fail_get(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("network down")

    monkeypatch.setattr(ndp_module, "_call_clio_kit_ndp_tool", fail_call)
    monkeypatch.setattr(ndp_module.requests, "get", fail_get)

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
    assert data["error"]["code"] == "ndp_dataset_details_unavailable"
    assert data["error"]["details"]["clio_kit_skipped"] is True
    assert "uvx package launch" in data["error"]["details"]["clio_kit_skip_reason"]


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
    monkeypatch.setattr(ndp_module.shutil, "which", lambda name: None)
    output_dir = ndp_module.Path.cwd() / "tmp" / "test-ndp-stage"
    output_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(output_dir.parent))

    async with Client(ndp_server) as client:
        result = await client.call_tool(
            "stage_resource",
            {
                "dataset_identifier": "ds1",
                "server": "global",
                "output_dir": str(output_dir),
            },
        )

    data = _parse_result(result)
    assert data["error"]["code"] == "pelican_unavailable"
    assert data["error"]["details"]["transport"] == "osdf"


@pytest.mark.asyncio
async def test_ndp_stage_resource_blocks_oversized_osdf(monkeypatch: pytest.MonkeyPatch):
    """Advertised resource sizes should prevent accidental huge OSDF staging."""

    async def fake_details(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "id": "ds1",
            "name": "salton-sea-seismic-data",
            "title": "Salton Sea Seismic Data",
            "resources": [
                {
                    "name": "Salton Sea Seismic Waveforms",
                    "url": "osdf:///ndp/public/ucr_seis/Data_Salton",
                    "resSize": "1.4 GB",
                }
            ],
        }

    monkeypatch.setattr(ndp_module, "_dataset_details", fake_details)
    monkeypatch.setattr(ndp_module.shutil, "which", lambda name: "pelican")
    output_dir = ndp_module.Path.cwd() / "tmp" / "test-ndp-stage-large"
    output_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(output_dir.parent))

    async with Client(ndp_server) as client:
        result = await client.call_tool(
            "stage_resource",
            {
                "dataset_identifier": "ds1",
                "server": "global",
                "max_bytes": 1024,
                "output_dir": str(output_dir),
            },
        )

    data = _parse_result(result)
    assert data["error"]["code"] == "resource_too_large"
    assert data["error"]["details"]["size_bytes"] > 1024


@pytest.mark.asyncio
async def test_ndp_stage_resource_creates_explicit_output_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """Explicit output directories should be created before write validation."""

    async def fake_details(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "id": "ds-http",
            "name": "http-dataset",
            "title": "HTTP Dataset",
            "resources": [
                {
                    "name": "sample.txt",
                    "url": "https://example.test/sample.txt",
                    "size": 11,
                }
            ],
        }

    class FakeResponse:
        headers = {"content-length": "11"}

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        def iter_content(self, chunk_size: int):
            del chunk_size
            yield b"hello world"

    monkeypatch.setattr(ndp_module, "_dataset_details", fake_details)
    monkeypatch.setattr(ndp_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(ndp_module.requests, "get", lambda *args, **kwargs: FakeResponse())
    output_dir = tmp_path / "new-stage-dir"

    async with Client(ndp_server) as client:
        result = await client.call_tool(
            "stage_resource",
            {
                "dataset_identifier": "ds-http",
                "server": "global",
                "output_dir": str(output_dir),
            },
        )

    data = _parse_result(result)
    assert data["staged"] is True
    assert data["resource_name"] == "sample.txt"
    assert data["selected_resource_name"] == "sample.txt"
    assert data["selected_resource_url"] == "https://example.test/sample.txt"
    assert data["source_url"] == "https://example.test/sample.txt"
    assert output_dir.exists()
    assert (output_dir / "sample.txt").read_bytes() == b"hello world"


@pytest.mark.asyncio
async def test_ndp_stage_resource_uses_curl_webget_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """HTTP staging should prefer curl/webget semantics when curl is available."""

    async def fake_details(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "id": "ds-http",
            "name": "http-dataset",
            "title": "HTTP Dataset",
            "resources": [
                {
                    "name": "sample.txt",
                    "url": "https://example.test/sample.txt",
                    "size": 11,
                }
            ],
        }

    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        output_path = Path(command[command.index("--output") + 1])
        output_path.write_bytes(b"hello world")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(ndp_module, "_dataset_details", fake_details)
    monkeypatch.setattr(ndp_module.shutil, "which", lambda name: "/usr/bin/curl")
    monkeypatch.setattr(ndp_module.subprocess, "run", fake_run)
    output_dir = tmp_path / "curl-stage-dir"

    async with Client(ndp_server) as client:
        result = await client.call_tool(
            "stage_resource",
            {
                "dataset_identifier": "ds-http",
                "server": "global",
                "output_dir": str(output_dir),
            },
        )

    data = _parse_result(result)
    assert data["staged"] is True
    assert data["method"] == "curl"
    assert (output_dir / "sample.txt").read_bytes() == b"hello world"
    assert calls
    assert calls[0][:5] == [
        "/usr/bin/curl",
        "--location",
        "--fail",
        "--show-error",
        "--silent",
    ]
    assert "--max-filesize" in calls[0]
    assert calls[0][calls[0].index("--connect-timeout") + 1] == "8"
    assert calls[0][calls[0].index("--max-time") + 1] == "45"
    assert calls[0][calls[0].index("--retry") + 1] == "1"


@pytest.mark.asyncio
async def test_ndp_stage_resource_reuses_existing_catalog_artifact_despite_low_max_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """A previously staged exact resource should not be re-downloaded and size-blocked."""

    async def fake_details(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "id": "ds-http",
            "name": "http-dataset",
            "title": "HTTP Dataset",
            "resources": [
                {
                    "name": "MTA1.CI.LY_.30.csv",
                    "url": "https://example.test/MTA1.CI.LY_.30.csv",
                    "size": "50 MB",
                }
            ],
        }

    def fail_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError("existing staged resource should skip curl")

    output_dir = tmp_path / "cache-stage-dir"
    output_dir.mkdir()
    cached = output_dir / "MTA1.CI.LY_.30.csv"
    cached.write_text("time,east,north,up\n1,0.1,0.2,0.3\n", encoding="utf-8")

    monkeypatch.setattr(ndp_module, "_dataset_details", fake_details)
    monkeypatch.setattr(ndp_module.shutil, "which", lambda name: "/usr/bin/curl")
    monkeypatch.setattr(ndp_module.subprocess, "run", fail_run)

    async with Client(ndp_server) as client:
        result = await client.call_tool(
            "stage_resource",
            {
                "dataset_identifier": "ds-http",
                "server": "global",
                "resource_name": "MTA1.CI.LY_.30.csv",
                "output_dir": str(output_dir),
                "max_bytes": 2,
            },
        )

    data = _parse_result(result)
    assert data["staged"] is True
    assert data["cache_hit"] is True
    assert data["path"] == str(cached)
    assert data["resource_name"] == "MTA1.CI.LY_.30.csv"
    assert data["selected_resource_url"] == "https://example.test/MTA1.CI.LY_.30.csv"
    assert data["_meta"]["cache_hit"] is True


@pytest.mark.asyncio
async def test_ndp_stage_resource_accepts_direct_http_resource_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """Concrete NDP/EarthScope resource URLs should stage without CKAN lookup."""

    calls: list[list[str]] = []

    async def fail_details(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("direct resource URLs should not call dataset details")

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        output_path = Path(command[command.index("--output") + 1])
        output_path.write_bytes(b"time,east,north,up\n1,0.1,0.2,0.3\n")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(ndp_module, "_dataset_details", fail_details)
    monkeypatch.setattr(ndp_module.shutil, "which", lambda name: "/usr/bin/curl")
    monkeypatch.setattr(ndp_module.subprocess, "run", fake_run)
    output_dir = tmp_path / "direct-url-stage"
    url = "https://ds2.datacollaboratory.org/Earthscope_api_dec2024/raw_csv/P475.CI.LY_.00.csv"

    async with Client(ndp_server) as client:
        result = await client.call_tool(
            "stage_resource",
            {
                "dataset_identifier": url,
                "server": "global",
                "output_dir": str(output_dir),
            },
        )

    data = _parse_result(result)
    assert data["staged"] is True
    assert data["resource_name"] == "P475.CI.LY_.00.csv"
    assert data["selected_resource_url"] == url
    assert data["source_url"] == url
    assert data["_meta"]["source"] == "direct_url"
    assert (output_dir / "P475.CI.LY_.00.csv").read_text().startswith("time,east")
    assert calls


@pytest.mark.asyncio
async def test_ndp_stage_resource_reuses_existing_direct_url_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """Direct URL staging should return an exact cached artifact when present."""

    async def fail_details(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("direct resource URLs should not call dataset details")

    def fail_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError("existing staged resource should skip curl")

    monkeypatch.setattr(ndp_module, "_dataset_details", fail_details)
    monkeypatch.setattr(ndp_module.shutil, "which", lambda name: "/usr/bin/curl")
    monkeypatch.setattr(ndp_module.subprocess, "run", fail_run)
    output_dir = tmp_path / "direct-url-cache"
    output_dir.mkdir()
    cached = output_dir / "P475.CI.LY_.00.csv"
    cached.write_text("time,east,north,up\n1,0.1,0.2,0.3\n", encoding="utf-8")
    url = "https://ds2.datacollaboratory.org/Earthscope_api_dec2024/raw_csv/P475.CI.LY_.00.csv"

    async with Client(ndp_server) as client:
        result = await client.call_tool(
            "stage_resource",
            {
                "dataset_identifier": url,
                "server": "global",
                "resource_name": "P475.CI.LY_.00.csv",
                "output_dir": str(output_dir),
                "max_bytes": 2,
            },
        )

    data = _parse_result(result)
    assert data["staged"] is True
    assert data["cache_hit"] is True
    assert data["path"] == str(cached)
    assert data["selected_resource_url"] == url
    assert data["_meta"]["source"] == "direct_url"
    assert data["_meta"]["cache_hit"] is True


@pytest.mark.asyncio
async def test_ndp_stage_resource_surfaces_curl_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """curl timeout/failure should be structured and include utility provenance."""

    async def fake_details(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "id": "ds-http",
            "name": "http-dataset",
            "title": "HTTP Dataset",
            "resources": [
                {
                    "name": "sample.txt",
                    "url": "https://example.test/sample.txt",
                    "size": 11,
                }
            ],
        }

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(command, 28, stdout="", stderr="operation timed out")

    monkeypatch.setattr(ndp_module, "_dataset_details", fake_details)
    monkeypatch.setattr(ndp_module.shutil, "which", lambda name: "/usr/bin/curl")
    monkeypatch.setattr(ndp_module.subprocess, "run", fake_run)

    async with Client(ndp_server) as client:
        result = await client.call_tool(
            "stage_resource",
            {
                "dataset_identifier": "ds-http",
                "server": "global",
                "output_dir": str(tmp_path),
            },
        )

    data = _parse_result(result)
    assert data["error"]["code"] == "webget_failed"
    assert data["error"]["details"]["method"] == "curl"
    assert data["error"]["details"]["returncode"] == 28
    assert "operation timed out" in data["error"]["details"]["stderr"]


@pytest.mark.asyncio
async def test_ndp_query_arcgis_features_filters_bbox_and_writes_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """ArcGIS FeatureServer resources should be queryable as compact evidence."""

    calls: list[dict[str, Any]] = []

    class FakeResponse:
        url = "https://example.test/FeatureServer/0/query?f=json"

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "geometryType": "esriGeometryPoint",
                "fields": [{"name": "IncidentName"}, {"name": "DailyAcres"}, {"name": "Updated"}],
                "features": [
                    {
                        "attributes": {
                            "IncidentName": "TEST",
                            "DailyAcres": 12.5,
                            "Updated": 1780550400000,
                            "Start": 1780546800000,
                        },
                        "geometry": {"x": -117.1, "y": 32.8},
                    }
                ],
            }

    def fake_get(url: str, **kwargs: Any) -> FakeResponse:
        calls.append({"url": url, **kwargs})
        return FakeResponse()

    monkeypatch.setattr(ndp_module.requests, "get", fake_get)
    output = tmp_path / "features.json"

    async with Client(ndp_server) as client:
        result = await client.call_tool(
            "query_arcgis_features",
            {
                "feature_service_url": "https://example.test/FeatureServer",
                "layer_id": 0,
                "where": "DailyAcres > 0",
                "min_lon": -118,
                "min_lat": 32,
                "max_lon": -116,
                "max_lat": 34,
                "output_path": str(output),
            },
        )

    data = _parse_result(result)
    assert data["ok"] is True
    assert data["feature_count"] == 1
    assert data["features"][0]["geometry"] == {"x": -117.1, "y": 32.8}
    assert data["features"][0]["properties"]["Updated"] == 1780550400000
    assert data["features"][0]["properties"]["Updated_iso"] == "2026-06-04T05:20:00Z"
    assert data["features"][0]["properties"]["Start_iso"] == "2026-06-04T04:20:00Z"
    assert output.exists()
    persisted_properties = json.loads(output.read_text(encoding="utf-8"))["features"][0][
        "properties"
    ]
    assert persisted_properties["IncidentName"] == "TEST"
    assert persisted_properties["Updated_iso"] == "2026-06-04T05:20:00Z"
    assert calls[0]["url"] == "https://example.test/FeatureServer/0/query"
    assert calls[0]["params"]["where"] == "DailyAcres > 0"
    assert calls[0]["params"]["geometryType"] == "esriGeometryEnvelope"


@pytest.mark.asyncio
async def test_ndp_profile_csv_resource_reports_numeric_state_space(tmp_path: Path) -> None:
    """CSV profiling should return samples, columns, and numeric ranges."""

    csv_path = tmp_path / "weather.csv"
    csv_path.write_text(
        "Date,Hour (PST),Air Temp (C),Wind Speed (m/s),Station\n"
        "6/1/2026,0100,18.5,2.0,Fresno\n"
        "6/1/2026,0200,20.5,3.5,Fresno\n"
        "6/1/2026,0300,,4.5,Fresno\n",
        encoding="utf-8",
    )

    async with Client(ndp_server) as client:
        result = await client.call_tool(
            "profile_csv_resource",
            {"filepath": str(csv_path), "max_rows": 10},
        )

    data = _parse_result(result)
    assert data["ok"] is True
    assert data["columns"] == ["Date", "Hour (PST)", "Air Temp (C)", "Wind Speed (m/s)", "Station"]
    assert data["rows_examined"] == 3
    assert data["rows_profiled"] == 3
    assert data["rows_scanned"] == 3
    assert data["numeric_summary_rows"] == 3
    assert data["profile_limited"] is False
    assert data["missing_values"] == {
        "Date": 0,
        "Hour (PST)": 0,
        "Air Temp (C)": 1,
        "Wind Speed (m/s)": 0,
        "Station": 0,
    }
    assert data["missing_values_rows"] == 3
    assert data["missing_values_scope"] == "profiled_rows"
    assert data["numeric_summary"]["Air Temp (C)"]["count"] == 2
    assert data["numeric_summary"]["Air Temp (C)"]["max"] == 20.5
    assert data["numeric_summary"]["Wind Speed (m/s)"]["mean"] == pytest.approx(10.0 / 3.0)
    assert data["sample_rows"][0]["Station"] == "Fresno"


@pytest.mark.asyncio
async def test_ndp_profile_csv_resource_reports_numeric_summary_scope(tmp_path: Path) -> None:
    """CSV profile statistics should say how many retained rows they cover."""

    csv_path = tmp_path / "gnss.csv"
    csv_path.write_text(
        "time,east,north,up\n"
        "1,0.1,0.2,0.3\n"
        "2,0.4,0.5,0.6\n"
        "3,0.7,0.8,0.9\n",
        encoding="utf-8",
    )

    async with Client(ndp_server) as client:
        result = await client.call_tool(
            "profile_csv_resource",
            {"filepath": str(csv_path), "max_rows": 2},
        )

    data = _parse_result(result)
    assert data["ok"] is True
    assert data["rows_examined"] == 3
    assert data["rows_scanned"] == 3
    assert data["rows_profiled"] == 2
    assert data["numeric_summary_rows"] == 2
    assert data["profile_limited"] is True
    assert data["missing_values"] == {"time": 0, "east": 0, "north": 0, "up": 0}
    assert data["missing_values_rows"] == 2
    assert data["missing_values_scope"] == "profiled_rows"
    assert data["numeric_summary"]["east"]["mean"] == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_ndp_filter_earthscope_station_catalog_ranks_nearby_stations(
    tmp_path: Path,
) -> None:
    """EarthScope station metadata can be filtered by resolved geography."""

    csv_path = tmp_path / "earthscope_converted_data.csv"
    csv_path.write_text(
        "Site,Latitude,(deg),Longitude,(deg),EllipElev,(m),X,(m),Y,(m),Z,(m),Epoch,(yr),Net,Status,Last,yr.doy,Ant,Hght(m),Type,Dome\n"
        "P475,32.66639773,-117.24394071,-25.0511,-2460344,-4778294,3422858,2022.7616,NOTA,ACTIVE,2023.345,0.0083,TRM59800.80,SCIT\n"
        "WWMT,33.95531352,-116.65386073,796.5299,-2376091,-4733825,3542781,2022.7616,SCGN,ACTIVE,2023.345,0.0083,TRM57971.00,SCIT\n",
        encoding="utf-8",
    )

    async with Client(ndp_server) as client:
        result = await client.call_tool(
            "filter_earthscope_station_catalog",
            {
                "filepath": str(csv_path),
                "latitude": 32.7157,
                "longitude": -117.1611,
                "radius_km": 50,
                "limit": 5,
            },
        )

    data = _parse_result(result)
    assert data["ok"] is True
    assert data["within_radius_count"] == 1
    assert data["stations"][0]["station"] == "P475"
    assert data["stations"][0]["distance_km"] < 10
    assert "P475 EarthScope GNSS CSV" in data["stations"][0]["suggested_search_terms"]
    assert "candidate_raw_csv_url" not in data["stations"][0]
    assert data["stations"][0]["resource_discovery"]["status"] == "search_required"
    assert "P475.CI.LY" in data["stations"][0]["resource_discovery"]["search_terms"]
    assert data["resource_discovery"]["status"] == "search_required"
    assert data["resource_discovery"]["station_resource_queries"][0]["station"] == "P475"
    assert data["resource_discovery"]["station_resource_queries"][0]["preferred_calls"][0][
        "arguments"
    ] == {
        "resource_name": "P475",
        "resource_format": "CSV",
        "server": "global",
        "limit": 20,
    }
    assert len(data["resource_discovery"]["station_resource_queries"][0]["preferred_calls"]) == 1
    assert "search_terms" not in data["resource_discovery"]["station_resource_queries"][0][
        "preferred_calls"
    ][0]["arguments"]
    assert "station_resource_queries preferred_calls" in data["next_action"]
    assert "suggested_search_terms" not in data["next_action"]
    assert data["analysis_ready"] is False


@pytest.mark.asyncio
async def test_ndp_filter_earthscope_station_catalog_classifies_timeseries_csv(
    tmp_path: Path,
) -> None:
    """A displacement time series should be typed as non-catalog evidence."""

    csv_path = tmp_path / "WWMT.CI.LY_.40.csv"
    csv_path.write_text(
        "time,east,north,up,sigEE,sigNN,sigUU,qChannel\n"
        "2026-01-01T00:00:00Z,1.0,2.0,3.0,0.1,0.1,0.2,final\n",
        encoding="utf-8",
    )

    async with Client(ndp_server) as client:
        result = await client.call_tool(
            "filter_earthscope_station_catalog",
            {
                "filepath": str(csv_path),
                "latitude": 34.05,
                "longitude": -118.25,
                "radius_km": 75,
                "limit": 5,
            },
        )

    data = _parse_result(result)
    assert data["ok"] is True
    assert data["catalog_applicable"] is False
    assert data["resource_kind"] == "station_timeseries_csv"
    assert data["analysis_ready"] is True
    assert data["_meta"]["status"] == "not_applicable"
    assert "profiling or plotting" in data["next_action"]


@pytest.mark.asyncio
async def test_ndp_plot_csv_timeseries_creates_png_and_rejects_missing_columns(
    tmp_path: Path,
) -> None:
    """CSV plotting should create real PNGs and surface schema mistakes."""

    csv_path = tmp_path / "weather.csv"
    csv_path.write_text(
        "Date,Air Temp (C),Wind Speed (m/s)\n"
        "6/1/2026,18.5,2.0\n"
        "6/2/2026,24.0,4.0\n",
        encoding="utf-8",
    )
    output = tmp_path / "weather.png"
    nested_output = tmp_path / "plots" / "weather.png"

    async with Client(ndp_server) as client:
        result = await client.call_tool(
            "plot_csv_timeseries",
            {
                "filepath": str(csv_path),
                "x_column": "Date",
                "y_columns": ["Air Temp (C)", "Wind Speed (m/s)"],
                "output_path": str(output),
                "title": "Weather",
            },
        )
        nested = await client.call_tool(
            "plot_csv_timeseries",
            {
                "filepath": str(csv_path),
                "x_column": "Date",
                "y_columns": ["Air Temp (C)", "Wind Speed (m/s)"],
                "output_path": str(nested_output),
                "title": "Weather nested",
            },
        )
        missing = await client.call_tool(
            "plot_csv_timeseries",
            {
                "filepath": str(csv_path),
                "x_column": "Missing",
                "y_columns": "Air Temp (C)",
                "output_path": str(tmp_path / "missing.png"),
            },
        )

    data = _parse_result(result)
    assert data["ok"] is True
    assert data["rows_plotted"] == 2
    assert output.exists()
    assert output.read_bytes().startswith(b"\x89PNG")
    nested_data = _parse_result(nested)
    assert nested_data["ok"] is True
    assert nested_output.exists()
    assert nested_output.read_bytes().startswith(b"\x89PNG")
    missing_data = _parse_result(missing)
    assert missing_data["error"]["code"] == "csv_plot_unknown_columns"
    assert missing_data["error"]["details"]["missing_columns"] == ["Missing"]


@pytest.mark.asyncio
async def test_ndp_plot_csv_timeseries_keeps_staged_artifacts_together(
    tmp_path: Path,
) -> None:
    """Plots for staged .clio resources should stay beside the source CSV."""

    staged_dir = tmp_path / ".clio" / "artifacts" / "ndp-staging"
    staged_dir.mkdir(parents=True)
    csv_path = staged_dir / "station.csv"
    csv_path.write_text(
        "time,east,north,up\n"
        "1733184000000,-0.049,0.054,0.217\n"
        "1733184001000,-0.049,0.054,0.217\n",
        encoding="utf-8",
    )
    requested = tmp_path / "artifacts" / "ndp-staging" / "station_timeseries.png"

    async with Client(ndp_server) as client:
        result = await client.call_tool(
            "plot_csv_timeseries",
            {
                "filepath": str(csv_path),
                "x_column": "time",
                "y_columns": ["east", "north", "up"],
                "output_path": str(requested),
            },
        )

    data = _parse_result(result)
    expected = staged_dir / "station_plot.png"
    assert data["ok"] is True
    assert data["output_path"] == str(expected)
    assert data["requested_output_path"] == str(requested)
    assert data["output_path_corrected"] is True
    assert expected.exists()
    assert not requested.exists()


def test_csv_plot_x_axis_inference_covers_time_and_fallback_state_space() -> None:
    epoch_ms = ndp_module._infer_csv_plot_x_axis(
        ["1733184000000", "1733184001000", "1733184002000"]
    )
    assert epoch_ms["kind"] == "epoch_milliseconds"
    assert epoch_ms["label"] == "time (UTC)"
    assert epoch_ms["parse_success_ratio"] == 1.0
    assert epoch_ms["values"][0].year == 2024

    epoch_seconds = ndp_module._infer_csv_plot_x_axis(
        ["1733184000", "1733184001", "1733184002"]
    )
    assert epoch_seconds["kind"] == "epoch_seconds"
    assert epoch_seconds["label"] == "time (UTC)"

    iso = ndp_module._infer_csv_plot_x_axis(
        [
            "2026-06-01T00:00:00Z",
            "2026-06-02T00:00:00Z",
            "2026-06-03T00:00:00Z",
            "2026-06-04T00:00:00Z",
            "not-a-date",
        ]
    )
    assert iso["kind"] == "datetime"
    assert iso["parse_success_ratio"] == pytest.approx(4 / 5)

    common_date = ndp_module._infer_csv_plot_x_axis(["6/1/2026", "6/2/2026"])
    assert common_date["kind"] == "datetime"

    categorical = ndp_module._infer_csv_plot_x_axis(["station-a", "station-b"])
    assert categorical["kind"] == "categorical"
    assert categorical["values"] == [0, 1]
    assert categorical["labels"] == ["station-a", "station-b"]


@pytest.mark.asyncio
async def test_ndp_plot_csv_timeseries_formats_epoch_millisecond_axis(
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "gnss.csv"
    csv_path.write_text(
        "time,east,north,up\n"
        "1733184000000,-0.05,0.05,0.22\n"
        "1733184001000,-0.04,0.04,0.21\n"
        "1733184002000,-0.03,0.03,0.20\n",
        encoding="utf-8",
    )
    output = tmp_path / "gnss.png"

    async with Client(ndp_server) as client:
        result = await client.call_tool(
            "plot_csv_timeseries",
            {
                "filepath": str(csv_path),
                "x_column": "time",
                "y_columns": ["east", "north", "up"],
                "output_path": str(output),
                "title": "GNSS",
            },
        )

    data = _parse_result(result)
    assert data["ok"] is True
    assert data["x_axis"] == {
        "kind": "epoch_milliseconds",
        "label": "time (UTC)",
        "parse_success_ratio": 1.0,
    }
    assert data["rows_plotted"] == 3
    assert output.exists()
    assert output.read_bytes().startswith(b"\x89PNG")


def test_list_capabilities_reports_ndp_server():
    """Context capability summaries should identify the NDP server owner."""
    caps = gateway_module.list_capabilities()

    ndp_caps = [cap for cap in caps if cap["name"].startswith("ndp_")]
    assert ndp_caps
    assert {cap["server"] for cap in ndp_caps} == {"ndp"}
    assert {
        "ndp_query_arcgis_features",
        "ndp_profile_csv_resource",
        "ndp_plot_csv_timeseries",
    }.issubset({cap["name"] for cap in ndp_caps})

    sac_caps = [cap for cap in caps if cap["name"].startswith("sac_")]
    assert sac_caps
    assert {cap["server"] for cap in sac_caps} == {"sac"}
