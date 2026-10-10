"""``/v1/search``: the resolved backend, a direct query and engine API keys."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.routes.search import register_search_routes
from clio_agent.providers.api_key_store import ProviderApiKeyStore
from clio_agent.search.backend import register_local_endpoint_resolver


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = FastAPI()
    register_search_routes(app)
    try:
        yield TestClient(app)
    finally:
        register_local_endpoint_resolver(None)


def test_search_off_reports_the_typed_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_SEARCH_BACKEND", "none")
    status = client.get("/v1/search/backend").json()
    assert status["backend"] == "none" and status["ready"] is False
    assert status["error"]["error"] == "search_not_configured"
    answer = client.post("/v1/search/query", json={"query": "hdf5"})
    assert answer.status_code == 503
    assert answer.json()["detail"]["error"] == "search_not_configured"
    assert client.post("/v1/search/query", json={}).status_code == 422


def test_the_default_backend_lists_its_engine_policy(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_SEARCH_SEARXNG_ENGINES", "wikipedia,baidu")
    status = client.get("/v1/search/backend").json()
    assert status["backend"] == "local_searxng"
    assert status["engines"] == ["wikipedia"] and status["dropped_engines"] == ["baidu"]


def test_engine_keys_go_to_the_credential_store_and_are_never_returned(
    client: TestClient,
) -> None:
    saved = client.put("/v1/search/engine-keys/braveapi", json={"api_key": "brave-key-1"})
    assert saved.status_code == 200 and "brave-key-1" not in saved.text
    assert ProviderApiKeyStore().load("search-engine:braveapi") == "brave-key-1"
    status = client.get("/v1/search/backend")
    assert status.json()["keyed_engines"]["braveapi"] is True
    assert "brave-key-1" not in status.text
    assert client.put("/v1/search/engine-keys/duckduckgo", json={"api_key": "x"}).status_code == 422
    assert client.delete("/v1/search/engine-keys/braveapi").json()["removed"] is True
    assert ProviderApiKeyStore().load("search-engine:braveapi") == ""
