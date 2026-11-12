"""Settings routes for search sources, keys and the removed SearXNG keys."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent import conf
from clio_agent.gact.routes.search import register_search_routes
from clio_agent.providers.api_key_store import ProviderApiKeyStore
from clio_agent.search.settings import key_ref
from clio_agent.tools.servers.web_limits import SEARCH_LIMITS


@pytest.fixture
def client() -> TestClient:
    SEARCH_LIMITS.reset()
    app = FastAPI()
    register_search_routes(app)
    return TestClient(app)


def test_view_lists_both_source_lists_in_order(client: TestClient) -> None:
    view = client.get("/v1/search/providers").json()
    assert view["enabled"] is True
    assert [row["id"] for row in view["web"]] == ["keenable", "exa", "firecrawl", "you", "parallel"]
    assert {row["id"] for row in view["literature"]} >= {"openalex", "crossref", "arxiv"}
    keenable = view["web"][0]
    assert keenable["approved"] is False and keenable["has_key"] is False
    assert keenable["free_use"].startswith("1,000")
    scholar = next(r for r in view["literature"] if r["id"] == "semantic_scholar")
    assert scholar["key_required"] is True


def test_patch_reorders_and_toggles(client: TestClient) -> None:
    view = client.patch(
        "/v1/search/providers",
        json={"web_order": ["exa", "keenable"], "web_disabled": ["parallel"], "auto_switch": True},
    ).json()
    assert [row["id"] for row in view["web"]][:2] == ["exa", "keenable"]
    assert next(r for r in view["web"] if r["id"] == "parallel")["enabled"] is False
    assert view["auto_switch"] is True


def test_patch_rejects_unknown_sources(client: TestClient) -> None:
    response = client.patch("/v1/search/providers", json={"web_order": ["searxng"]})
    assert response.status_code == 422
    assert "unknown source" in response.json()["detail"]


def test_saving_a_key_moves_the_source_first_and_clears_its_rest(client: TestClient) -> None:
    SEARCH_LIMITS.cool("you", 3600, "allowance_used")
    answer = client.put("/v1/search/providers/you/key", json={"api_key": "secret-value"}).json()
    assert answer["saved"] is True
    assert answer["settings"]["web"][0]["id"] == "you"
    assert answer["settings"]["web"][0]["has_key"] is True
    assert SEARCH_LIMITS.cooldown("you") is None
    assert ProviderApiKeyStore().load(key_ref("you")) == "secret-value"
    assert "secret-value" not in client.get("/v1/search/providers").text
    kept = client.put(
        "/v1/search/providers/exa/key", json={"api_key": "k2", "placement": "keep"}
    ).json()
    assert kept["settings"]["web"][0]["id"] == "you"
    assert client.delete("/v1/search/providers/you/key").json() == {
        "source": "you",
        "removed": True,
    }


def test_unknown_source_key_is_404(client: TestClient) -> None:
    assert client.put("/v1/search/providers/google/key", json={"api_key": "x"}).status_code == 404


def test_searxng_keys_are_refused_as_removed(tmp_path, monkeypatch) -> None:
    from clio_agent.errors import RemovedConfigKeyError
    from clio_agent.removed_config_keys import reject_removed_config_keys

    monkeypatch.setenv("CLIO_SEARCH_BACKEND", "local_searxng")
    conf.reload()
    with pytest.raises(RemovedConfigKeyError, match="SearXNG was removed"):
        reject_removed_config_keys()
