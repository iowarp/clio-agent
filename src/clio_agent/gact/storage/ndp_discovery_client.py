"""Read-only NDP discovery HTTP client, independent of MCP component installation.

Uses the /search and /organization API also consumed by CLIO Kit's NDP server.
The application needs discovery without installing or importing an MCP server.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from pydantic import BaseModel, Field


class Dataset(BaseModel):
    """Catalog fields used to preserve an exact resource selection."""

    id: str
    name: str
    title: str
    notes: str | None = None
    owner_org: str | None = None
    resources: list[dict[str, Any]] = Field(default_factory=list)


class NDPClient:
    """Read one bounded discovery response without retrying or forwarding credentials."""

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (
            base_url or os.getenv("NDP_API_URL") or "http://155.101.6.191:8003"
        ).rstrip("/")
        self.timeout = httpx.Timeout(15, connect=5)

    async def _request(
        self, method: str, path: str, *, params: Any = None, body: dict[str, Any] | None = None
    ) -> list[Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.request(method, self.base_url + path, params=params, json=body)
            response.raise_for_status()
            result = response.json()
        if not isinstance(result, list):
            raise ValueError("NDP discovery returned an invalid response; expected a list")
        return result

    async def list_organizations(
        self, name_filter: str | None = None, server: str = "global"
    ) -> list[str]:
        """List publisher names in the selected catalog."""
        params = {"server": server, **({"name": name_filter} if name_filter else {})}
        names = await self._request("GET", "/organization", params=params)
        if not all(isinstance(name, str) for name in names):
            raise ValueError("NDP discovery returned invalid organization names")
        return names

    async def search_datasets_simple(
        self, terms: list[str], server: str = "global"
    ) -> list[Dataset]:
        """Search using the API's repeated terms query parameter."""
        rows = await self._request("GET", "/search", params={"server": server, "terms": terms})
        return [Dataset.model_validate(row) for row in rows]

    async def search_datasets_advanced(
        self, owner_org: str, server: str = "global"
    ) -> list[Dataset]:
        """Search the selected publisher through the API's field filter."""
        rows = await self._request(
            "POST", "/search", body={"server": server, "owner_org": owner_org}
        )
        return [Dataset.model_validate(row) for row in rows]
