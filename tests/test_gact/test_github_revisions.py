"""Revision discovery is bounded, cached, authenticated and creates no source."""

import json
from pathlib import Path
from typing import Any

import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.routes.connected_storage import register_connected_storage_routes


@pytest.fixture
def revisions_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Expose real CLIO routes with only GitHub's HTTP responses substituted."""
    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path))
    app = FastAPI()
    register_connected_storage_routes(app)
    return TestClient(app)


def response(url: str, body: Any, status: int = 200) -> requests.Response:
    """Create a normal requests response for the cache and permission paths."""
    result = requests.Response()
    result.url, result.status_code = url, status
    result._content = json.dumps(body).encode()
    return result


def test_revisions_cache_pages_and_reuse_the_saved_account(
    revisions_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    service = revisions_client.app.state.connected_storage
    monkeypatch.setattr(service.auth, "connected", lambda account: True)
    monkeypatch.setattr(service.auth, "token", lambda account: "private-account-token")

    def get(url: str, **kwargs: Any) -> requests.Response:
        calls.append(url)
        assert kwargs["headers"]["Authorization"] == "Bearer private-account-token"
        assert kwargs["allow_redirects"] is False
        if url.endswith("/branches"):
            body = (
                [{"name": f"feature/{index}", "commit": {"sha": "a" * 40}} for index in range(100)]
                if kwargs["params"]["page"] == 1
                else []
            )
        elif url.endswith("/tags"):
            body = [{"name": "v1", "commit": {"sha": "b" * 40}}]
        elif url.endswith("/commits"):
            body = [{"sha": "c" * 40, "commit": {"message": "Fix data\nMore details"}}]
        else:
            body = {"default_branch": "main"}
        return response(url, body)

    monkeypatch.setattr("requests.get", get)
    endpoint = "/v1/storage/github/revisions"
    body = {"url": "https://github.com/example/private/tree/main/data", "kind": "branch"}
    first = revisions_client.post(endpoint, json=body)
    assert first.status_code == 200, first.text
    assert first.json()["default_branch"] == "main"
    assert first.json()["next_page"] == 2
    assert first.json()["revisions"][0]["value"] == "feature/0"
    assert revisions_client.post(endpoint, json=body).json() == first.json()
    assert len(calls) == 2
    assert revisions_client.post(endpoint, json={**body, "page": 2}).json()["next_page"] is None
    assert (
        revisions_client.post(endpoint, json={**body, "kind": "tag"}).json()["revisions"][0][
            "value"
        ]
        == "v1"
    )
    assert (
        revisions_client.post(endpoint, json={**body, "kind": "commit"}).json()["revisions"][0][
            "label"
        ]
        == "Fix data"
    )
    assert len(calls) == 5
    assert service.store.list("source", dict) == []


def test_invalid_urls_and_pages_never_contact_github(
    revisions_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def get(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Invalid input must not cause network access")

    monkeypatch.setattr("requests.get", get)
    endpoint = "/v1/storage/github/revisions"
    assert (
        revisions_client.post(endpoint, json={"url": "https://example.org/owner/repo"}).status_code
        == 409
    )
    assert (
        revisions_client.post(
            endpoint, json={"url": "https://github.com/owner/repo", "page": 0}
        ).status_code
        == 422
    )


def test_rate_limit_gate_is_shared_with_file_access(
    revisions_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def get(url: str, **kwargs: Any) -> requests.Response:
        calls.append(url)
        result = response(url, {"message": "API rate limit exceeded"}, 403)
        result.headers["retry-after"] = "300"
        return result

    monkeypatch.setattr("requests.get", get)
    result = revisions_client.post(
        "/v1/storage/github/revisions", json={"url": "https://github.com/owner/repo"}
    )
    assert result.status_code == 409
    assert "temporarily limiting" in result.json()["detail"]
    with pytest.raises(ValueError, match="temporarily limiting"):
        with revisions_client.app.state.connected_storage.github.filesystem(
            "owner", "repo", "main"
        ):
            pytest.fail("Rate limited access must not create a client")
    assert len(calls) == 1


def test_unavailable_private_repository_has_actionable_error(
    revisions_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("requests.get", lambda url, **kwargs: response(url, {}, 404))
    result = revisions_client.post(
        "/v1/storage/github/revisions", json={"url": "https://github.com/owner/private"}
    )
    assert result.status_code == 403
    assert "sign in with access" in result.json()["detail"]
