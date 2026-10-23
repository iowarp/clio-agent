"""Read-only NDP discovery with exact catalog and resource identities."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import Field, field_validator

from clio_agent.gact.storage import ndp_discovery_client as catalog_client
from clio_agent.gact.storage.models import StorageModel
from clio_agent.gact.storage.ndp_ckan import PUBLIC_CATALOG_URL, NDPCkanCatalog, catalog_description


class NDPEndpoint(StorageModel):
    """An explicitly configured catalog and credential destination, never a resource-supplied host."""

    id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(max_length=4096)
    server: Literal["local", "global"] = "local"
    authenticated: bool = False
    catalog_url: str = ""

    @field_validator("catalog_url")
    @classmethod
    def validate_catalog_url(cls, value: str) -> str:
        """An optional public CKAN catalog is operator-owned, never taken from dataset metadata."""
        return cls.validate_url(value) if value else ""

    def same_destination(self, other: NDPEndpoint) -> bool:
        """Keep existing source identity when only the public metadata transport improves."""
        return self.model_dump(exclude={"catalog_url"}) == other.model_dump(exclude={"catalog_url"})

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        """Keep catalog requests on a single configured origin without userinfo or query strings."""
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Choose an HTTP(S) Endpoint URL without credentials or query parameters"
            )
        return value.rstrip("/")


class NDPResource(StorageModel):
    """One stable catalog resource; URLs are kept privately in the selected-source snapshot."""

    id: str
    path: str
    name: str
    url: str
    format: str = ""
    size: int = 0
    revision: str = ""


class NDPDataset(StorageModel):
    """A dataset identity scoped to its catalog Endpoint."""

    id: str
    name: str
    title: str
    description: str = ""
    organization: str = ""
    endpoint: NDPEndpoint
    resources: list[NDPResource]
    access: Literal["viewer", "request_access"] | None = None


class NDPSelection(StorageModel):
    """Durable resource selection, fetched by the server rather than accepted as arbitrary URLs."""

    id: str
    dataset: NDPDataset


class NDPCatalog:
    """Adapt existing science discovery to saved-account and selected-source custody."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._results: dict[tuple[str, str], NDPDataset] = {}
        self._public_catalog = NDPCkanCatalog()

    @staticmethod
    def endpoints() -> list[NDPEndpoint]:
        """Load operator-owned endpoints; public discovery keeps the existing client's default."""
        raw = os.environ.get("CLIO_STORAGE_NDP_ENDPOINTS", "")
        if raw:
            try:
                endpoints = [NDPEndpoint.model_validate(item) for item in json.loads(raw)]
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    "CLIO_STORAGE_NDP_ENDPOINTS must contain an array of Endpoint settings"
                ) from exc
            if len({item.id for item in endpoints}) != len(endpoints) or len(endpoints) > 50:
                raise ValueError("NDP Endpoint IDs must be unique (maximum 50)")
            if any(
                item.authenticated and not item.url.startswith("https://") for item in endpoints
            ):
                raise ValueError("Authenticated NDP Endpoints require HTTPS")
            return endpoints
        client = catalog_client.NDPClient()
        return [
            NDPEndpoint(
                id="ndp",
                name="NDP catalog",
                url=client.base_url,
                server="global",
                catalog_url=PUBLIC_CATALOG_URL,
            )
        ]

    @staticmethod
    def access(
        endpoint: NDPEndpoint, token: str | None
    ) -> tuple[Literal["viewer", "request_access"] | None, str]:
        """Ask the Endpoint/AAI for its live effective role; never infer it from JWT claims."""
        if not token or not endpoint.authenticated:
            return None, ""
        try:
            with httpx.Client(timeout=15, follow_redirects=False) as client:
                response = client.get(
                    endpoint.url + "/user/info", headers={"Authorization": "Bearer " + token}
                )
            if response.status_code == 403:
                return "request_access", ""
            if not response.is_success:
                return (
                    None,
                    "NDP could not check Endpoint access. Catalog browsing is still available.",
                )
            role = response.json().get("effective_role")
            if role in {"viewer", "writer", "admin"}:
                return "viewer", ""
            if role == "none":
                return "request_access", ""
            return None, ""
        except (httpx.HTTPError, ValueError):
            return None, "NDP could not check Endpoint access. Catalog browsing is still available."

    def search(
        self,
        query: str,
        endpoint_id: str = "",
        *,
        only_groups: bool = False,
        token: str | None = None,
        organization: str = "",
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Search configured catalogs and preserve exact selections without downloading bytes."""
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("Use a nonnegative offset and a page size from 1 to 100")
        endpoints = [item for item in self.endpoints() if not endpoint_id or item.id == endpoint_id]
        if not endpoints:
            raise ValueError("Choose a configured NDP Endpoint")
        datasets: list[NDPDataset] = []
        warnings: list[str] = []
        total, has_more = 0, False
        for endpoint in endpoints:
            count = 0
            access, warning = self.access(endpoint, token)
            if warning:
                warnings.append(warning)
            if only_groups and access != "viewer":
                if access is None:
                    warnings.append(
                        "Only my groups requires a successful live Endpoint access check."
                    )
                continue
            try:
                client = catalog_client.NDPClient(base_url=endpoint.url)
                client.timeout = httpx.Timeout(15, connect=5)
                if endpoint.catalog_url:
                    records, count = self._public_catalog.search(
                        endpoint.catalog_url, query, organization, offset, limit
                    )
                    rows = [
                        catalog_client.Dataset(
                            **{
                                **record,
                                "notes": record.get("notes") or record.get("description"),
                                "owner_org": (record.get("organization") or {}).get("name")
                                or record.get("owner_org"),
                                # CKAN extras are a list; the picker does not need them.
                                "extras": None,
                            }
                        )
                        for record in records
                    ]
                elif organization:
                    rows = asyncio.run(
                        client.search_datasets_advanced(
                            owner_org=organization, server=endpoint.server
                        )
                    )
                    if query:
                        terms = re.findall(r"\w+", query.casefold())
                        rows = [
                            row
                            for row in rows
                            if all(
                                term
                                in " ".join(
                                    [
                                        row.name,
                                        row.title,
                                        row.notes or "",
                                        json.dumps(row.resources),
                                    ]
                                ).casefold()
                                for term in terms
                            )
                        ]
                else:
                    rows = asyncio.run(
                        client.search_datasets_simple(terms=[query], server=endpoint.server)
                    )
                if not endpoint.catalog_url:
                    count = min(len(rows), 500)
                    if len(rows) > 500:
                        warnings.append(
                            f"{endpoint.name}: this Endpoint exposes only the first 500 matching datasets. Refine the query or organization."
                        )
                    rows = rows[offset : min(offset + limit, 500)]
            except Exception as exc:
                cause = exc if isinstance(exc, httpx.HTTPStatusError) else exc.__cause__
                status = (
                    f" (HTTP {cause.response.status_code})"
                    if isinstance(cause, httpx.HTTPStatusError)
                    else ""
                )
                raise ValueError(
                    f"{endpoint.name} could not complete this search{status}. "
                    "Try again later or choose another Endpoint."
                ) from exc
            total += count
            has_more = has_more or offset + limit < count
            for item in rows:
                resources = []
                for index, resource in enumerate(item.resources[:1000]):
                    url = str(resource.get("url", ""))
                    identifier = str(resource.get("id") or hashlib.sha256(url.encode()).hexdigest())
                    name = str(
                        resource.get("name") or urlsplit(url).path.rsplit("/", 1)[-1] or "resource"
                    )
                    filename = re.sub(r"[^\w. -]", "_", name).strip(" .")[:100] or "resource"
                    if filename.split(".")[0].upper() in {
                        "CON",
                        "PRN",
                        "AUX",
                        "NUL",
                        *[f"{prefix}{n}" for prefix in ("COM", "LPT") for n in range(1, 10)],
                    }:
                        filename = "resource_" + filename
                    size = resource.get("size", 0)
                    resources.append(
                        NDPResource(
                            id=identifier,
                            path=f"{index + 1}-{filename}",
                            name=name,
                            url=url,
                            size=int(size) if str(size).isdigit() else 0,
                            format=str(resource.get("format") or ""),
                            revision=hashlib.sha256(
                                json.dumps(resource, sort_keys=True).encode()
                            ).hexdigest(),
                        )
                    )
                dataset = NDPDataset(
                    id=item.id,
                    name=item.name,
                    title=item.title,
                    description=catalog_description(item.notes or ""),
                    organization=item.owner_org or "",
                    endpoint=endpoint,
                    resources=resources,
                    access=access,
                )
                datasets.append(dataset)
        with self._lock:
            # Bound the private selection cache. Attaching reuses exactly what was shown.
            if len(self._results) + len(datasets) > 2000:
                self._results.clear()
            self._results.update({(item.endpoint.id, item.id): item for item in datasets})
        return {
            "datasets": [self.visible(item) for item in datasets],
            "warnings": list(dict.fromkeys(warnings)),
            "total": total,
            "next_offset": offset + limit if has_more else None,
        }

    def organizations(self, query: str = "", endpoint_id: str = "") -> dict[str, Any]:
        """List catalog organization identities without granting membership or fetching data."""
        endpoints = [item for item in self.endpoints() if not endpoint_id or item.id == endpoint_id]
        if not endpoints:
            raise ValueError("Choose a configured NDP Endpoint")
        organizations: list[dict[str, str]] = []
        for endpoint in endpoints:
            client = catalog_client.NDPClient(base_url=endpoint.url)
            client.timeout = httpx.Timeout(15, connect=5)
            try:
                names = (
                    self._public_catalog.organizations(endpoint.catalog_url)
                    if endpoint.catalog_url
                    else asyncio.run(
                        client.list_organizations(name_filter=query or None, server=endpoint.server)
                    )
                )
            except Exception as exc:
                raise ValueError(
                    f"{endpoint.name} could not list organizations. Try again later."
                ) from exc
            organizations.extend(
                {"id": name, "endpoint_id": endpoint.id, "endpoint_name": endpoint.name}
                for name in names[:1000]
                if not query or query.casefold() in name.casefold()
            )
        return {"organizations": organizations}

    @staticmethod
    def visible(dataset: NDPDataset) -> dict[str, Any]:
        """Expose catalog labels and stable IDs without leaking signed download URLs."""
        return {
            **dataset.model_dump(exclude={"endpoint", "resources"}),
            "endpoint_id": dataset.endpoint.id,
            "endpoint_name": dataset.endpoint.name,
            "resources": [item.model_dump(exclude={"url"}) for item in dataset.resources],
        }

    def selected(self, endpoint_id: str, dataset_id: str, resources: list[str]) -> NDPDataset:
        """Resolve selected IDs only from server-observed catalog data."""
        with self._lock:
            dataset = self._results.get((endpoint_id, dataset_id))
        if dataset is None:
            raise ValueError("Search for this NDP dataset again before attaching it")
        if not resources:
            raise ValueError(
                "Choose at least one resource ID from this NDP dataset's resource list"
            )
        if len(resources) != len(set(resources)):
            raise ValueError("Repeated NDP resource IDs; include each selected resource only once")
        unknown = set(resources) - {item.id for item in dataset.resources}
        if unknown:
            raise ValueError(
                f"Unknown resource IDs for dataset {dataset.id}: {', '.join(sorted(unknown)[:5])}. "
                "CLIO rejected this selection before contacting NDP. Search for the dataset "
                "again with connected_data_search_ndp and copy its returned resources[].id "
                "values exactly; names, paths and reconstructed IDs are not resource IDs."
            )
        return dataset.model_copy(
            update={"resources": [item for item in dataset.resources if item.id in resources]}
        )

    def refresh(self, dataset: NDPDataset) -> NDPDataset:
        """Refresh selected catalog identities without broadening the approved resource set."""
        if not any(current.same_destination(dataset.endpoint) for current in self.endpoints()):
            raise PermissionError("This NDP Endpoint changed; attach the dataset again")
        self.search(dataset.name, dataset.endpoint.id)
        updated = self.selected(
            dataset.endpoint.id, dataset.id, [item.id for item in dataset.resources]
        )
        paths = {item.id: item.path for item in dataset.resources}
        return updated.model_copy(
            update={
                "resources": [
                    item.model_copy(update={"path": paths[item.id]}) for item in updated.resources
                ]
            }
        )
