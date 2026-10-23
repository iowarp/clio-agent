"""Discovery failures must not become empty catalogs or implicit retries."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from clio_agent.gact.storage import ndp_discovery_client


@pytest.mark.parametrize("response", [{"error": "unavailable"}, [42]])
def test_invalid_organization_response_is_not_an_empty_catalog(
    monkeypatch: pytest.MonkeyPatch, response: Any
) -> None:
    """Reject malformed success payloads without inventing an absence of data."""
    requests: list[httpx.Request] = []

    def serve(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=response)

    original = httpx.AsyncClient

    def client(*, timeout: httpx.Timeout) -> httpx.AsyncClient:
        return original(timeout=timeout, transport=httpx.MockTransport(serve))

    monkeypatch.setattr(ndp_discovery_client.httpx, "AsyncClient", client)
    with pytest.raises(ValueError, match="invalid"):
        asyncio.run(ndp_discovery_client.NDPClient("https://catalog.test").list_organizations())
    assert len(requests) == 1
    assert not requests[0].headers.get("Authorization")
