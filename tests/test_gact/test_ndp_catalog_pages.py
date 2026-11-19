"""Real HTTP catalog paging avoids the Endpoint proxy's unbounded search."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.routes.storage_ndp import register_storage_ndp_routes
from clio_agent.gact.storage.ndp_catalog import NDPCatalog
from clio_agent.gact.storage.ndp_ckan import PUBLIC_CATALOG_URL, catalog_description
from clio_agent.gact.storage.ndp_selection import select_source
from clio_agent.gact.storage.service import StorageService


@pytest.fixture
def ckan(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, dict[str, list[str]]]]]:
    """Serve actual CKAN-shaped pages, record exact requests, and reject the old proxy route."""
    calls: list[tuple[str, dict[str, list[str]]]] = []
    records = [
        {
            "id": f"dataset-{i}",
            "name": f"sea-{i}",
            "title": f"Sea observations {i}",
            "notes": "GNSS measurements",
            "owner_org": "org-uuid",
            "organization": {"name": "earthscope"},
            "extras": [{"key": "private-looking-metadata", "value": "not needed by picker"}],
            "resources": [
                {
                    "id": f"resource-{i}",
                    "name": f"stations-{i}.csv",
                    "format": "CSV" if i < 2 else "",
                    "description": f"Hourly station readings, part {i}.",
                    "url": f"https://data.test/{i}.csv",
                }
            ],
        }
        for i in range(3)
    ]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            """Keep captured qualification output concise."""

        def do_GET(self) -> None:
            """A page is sliced at the upstream service, before serialization."""
            assert not self.headers.get("Authorization")
            url = urlsplit(self.path)
            params = parse_qs(url.query)
            calls.append((url.path, params))
            if url.path.endswith("/organization_list"):
                result: Any = ["earthscope", "gnosis"]
            else:
                assert url.path == "/catalog/api/3/action/package_search", "Old Endpoint route used"
                if params["q"] == ["outage*"]:
                    self.send_response(503)
                    self.end_headers()
                    self.wfile.write(b"internal-backend-secret")
                    return
                start, size = int(params["start"][0]), int(params["rows"][0])
                matching = records
                query = params["q"][0]
                if query != "*:*":
                    terms = [term.rstrip("*") for term in query.split(" AND ")]
                    matching = [
                        r for r in matching if all(t in json.dumps(r).lower() for t in terms)
                    ]
                for clause in params["fq"][0].split(" AND ") if "fq" in params else ():
                    key, _, value = clause.partition(":")
                    wanted = json.loads(value)
                    if key == "organization":
                        matching = [r for r in matching if r["organization"]["name"] == wanted]
                    else:
                        assert key == "res_format", clause
                        matching = [
                            r
                            for r in matching
                            if any(item["format"] == wanted for item in r["resources"])
                        ]
                result = {"count": len(matching), "results": matching[start : start + size]}
            body = json.dumps({"success": True, "result": result}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv(
        "CLIO_STORAGE_NDP_ENDPOINTS",
        json.dumps(
            [
                {
                    "id": "ndp",
                    "name": "NDP catalog",
                    "url": base + "/endpoint",
                    "catalog_url": base + "/catalog",
                    "server": "global",
                }
            ]
        ),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_blank_search_pages_upstream_and_preserves_selection(ckan: list[Any]) -> None:
    catalog = NDPCatalog()
    first = catalog.search("", limit=2, token="must-not-be-forwarded")
    assert [row["id"] for row in first["datasets"]] == ["dataset-0", "dataset-1"]
    assert first["total"] == 3 and first["next_offset"] == 2
    assert ckan[0][1]["q"] == ["*:*"] and ckan[0][1]["rows"] == ["2"]
    second = catalog.search("", limit=2, offset=2)
    assert [row["id"] for row in second["datasets"]] == ["dataset-2"]
    assert second["next_offset"] is None and ckan[1][1]["start"] == ["2"]
    for i in range(3):
        selected = catalog.selected("ndp", f"dataset-{i}", [f"resource-{i}"])
        assert selected.resources[0].url == f"https://data.test/{i}.csv"
        assert selected.organization == "earthscope"
        assert "url" not in catalog.visible(selected)["resources"][0]


def test_literal_search_organization_filter_and_cached_publishers(ckan: list[Any]) -> None:
    catalog = NDPCatalog()
    result = catalog.search("sea csv", organization="earthscope")
    assert len(result["datasets"]) == 3
    assert ckan[0][1]["q"] == ["sea* AND csv*"]
    assert ckan[0][1]["fq"] == ['organization:"earthscope"']
    assert catalog.organizations("earth")["organizations"][0]["id"] == "earthscope"
    assert catalog.organizations("gnos")["organizations"][0]["id"] == "gnosis"
    assert len([path for path, _ in ckan if path.endswith("organization_list")]) == 1


def test_outage_is_not_retried_or_exposed_as_an_empty_page(ckan: list[Any]) -> None:
    with pytest.raises(ValueError, match=r"HTTP 503") as error:
        NDPCatalog().search("outage")
    assert "internal-backend-secret" not in str(error.value)
    assert len(ckan) == 1


def test_existing_selection_refresh_survives_public_catalog_transport_upgrade(
    tmp_path: Path,
    ckan: list[Any],
) -> None:
    catalog = NDPCatalog()
    catalog.search("sea-0")
    selected = catalog.selected("ndp", "dataset-0", ["resource-0"])
    selected.endpoint.catalog_url = ""  # Stored selections predate the paginated adapter.
    service = StorageService(tmp_path / "ledger", tmp_path / "credentials.json")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    before = select_source(service, "ws", selected, workspace)
    with service.adapter(before) as adapter:
        assert adapter.entries()[0].locator == "resource-0"
    refreshed = catalog.refresh(selected)
    assert refreshed.resources[0].id == "resource-0"
    assert refreshed.resources[0].path == selected.resources[0].path
    assert select_source(service, "ws", refreshed, workspace).source.id == before.source.id
    selected.endpoint.url = "https://different.test"
    with pytest.raises(PermissionError, match="Endpoint changed"):
        catalog.refresh(selected)


def test_search_http_contract_passes_page_and_retains_resource_ids(
    tmp_path: Path, ckan: list[Any]
) -> None:
    service = StorageService(tmp_path / "sources", tmp_path / "credentials.json")
    app = FastAPI()

    async def run(action: Any) -> Any:
        return await asyncio.to_thread(action)

    register_storage_ndp_routes(app, service, run, lambda _: tmp_path, lambda row: {})
    with TestClient(app) as client:
        first = client.post("/v1/storage/ndp/search", json={"query": "", "limit": 2})
        assert first.status_code == 200, first.text
        assert first.json()["next_offset"] == 2
        last = client.post("/v1/storage/ndp/search", json={"query": "", "limit": 2, "offset": 2})
        assert last.status_code == 200 and last.json()["datasets"][0]["id"] == "dataset-2"
        assert client.post("/v1/storage/ndp/search", json={"offset": -1}).status_code == 422


def test_default_catalog_uses_public_pagination_without_changing_endpoint_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLIO_STORAGE_NDP_ENDPOINTS", raising=False)
    endpoint = NDPCatalog.endpoints()[0]
    assert endpoint.id == "ndp" and endpoint.catalog_url == PUBLIC_CATALOG_URL
    assert not endpoint.authenticated


def test_catalog_descriptions_show_text_instead_of_raw_html() -> None:
    assert (
        catalog_description(
            "<p>Flood <b>observations</b> &amp; forecasts.</p><script>untrusted()</script>"
            "<style>.hidden { color:red }</style><p>Under 10 m.</p>"
        )
        == "Flood observations & forecasts. Under 10 m."
    )
    assert (
        catalog_description("Measurements: 0 < x < 10; units m².")
        == "Measurements: 0 < x < 10; units m²."
    )


def test_resource_format_filter_and_resource_descriptions(ckan: list[Any]) -> None:
    """Field filters and each resource's own words, which only the MCP server gave."""
    catalog = NDPCatalog()
    result = catalog.search("", organization="earthscope", resource_format="CSV")
    assert ckan[0][1]["fq"] == ['organization:"earthscope" AND res_format:"CSV"']
    assert [row["id"] for row in result["datasets"]] == ["dataset-0", "dataset-1"]
    resource = result["datasets"][0]["resources"][0]
    assert resource["description"] == "Hourly station readings, part 0." and "url" not in resource
    only_format = catalog.search("", resource_format='CSV" OR *:*')
    assert ckan[-1][1]["fq"] == ['res_format:"CSV\\" OR *:*"']  # a value, never syntax
    assert only_format["datasets"] == []
