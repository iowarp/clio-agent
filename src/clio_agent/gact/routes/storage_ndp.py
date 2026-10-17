"""NDP dataset selection and reusable account device-flow cancellation."""

from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, HTTPException
from pydantic import Field

from clio_agent.gact.routes.storage_inputs import CompleteSignIn
from clio_agent.gact.storage.linked import link_folder
from clio_agent.gact.storage.models import (
    CreateSource,
    SourceConfiguration,
    SourceRecord,
    StorageModel,
)
from clio_agent.gact.storage.ndp_catalog import NDPSelection
from clio_agent.gact.storage.service import StorageService

Action = Callable[[Callable[[], Any]], Awaitable[Any]]


class NDPSearch(StorageModel):
    """Search catalog metadata without granting access or downloading resource contents."""

    query: str = Field(default="", max_length=1000)
    endpoint_id: str = Field(default="", max_length=120)
    only_groups: bool = False


class NDPAttach(StorageModel):
    """Select server-observed resource IDs, never client-supplied download URLs."""

    endpoint_id: str = Field(min_length=1, max_length=120)
    dataset_id: str = Field(min_length=1, max_length=512)
    resource_ids: list[str] = Field(min_length=1, max_length=1000)


def register_storage_ndp_routes(
    app: FastAPI,
    service: StorageService,
    run: Action,
    workspace_root: Callable[[str], Path],
    visible: Callable[[SourceRecord], dict[str, Any]],
) -> None:
    """Register NDP discovery/selection in the existing source control plane."""

    selections = threading.RLock()

    def account_token() -> str | None:
        record = service.auth.account_login(service.principal, service.store.clio_id, "ndp")
        return service.auth.token(record) if service.auth.connected(record) else None

    @app.get("/v1/storage/ndp/endpoints")
    async def endpoints() -> dict[str, Any]:
        return await run(
            lambda: {"endpoints": [item.model_dump() for item in service.ndp.endpoints()]}
        )

    @app.post("/v1/storage/ndp/search")
    async def search(body: NDPSearch) -> dict[str, Any]:
        def discover() -> dict[str, Any]:
            warning = ""
            try:
                token = account_token()
            except (PermissionError, ValueError):
                token = None
                warning = "NDP sign-in could not be refreshed. Public catalog browsing is still available; sign in again for protected data."
            result = service.ndp.search(
                body.query, body.endpoint_id, only_groups=body.only_groups, token=token
            )
            if warning:
                result["warnings"].append(warning)
            return result

        return await run(discover)

    @app.post("/v1/workspaces/{wid}/sources/ndp", status_code=201)
    async def attach(wid: str, body: NDPAttach) -> dict[str, Any]:
        root = workspace_root(wid)

        def select() -> dict[str, Any]:
            dataset = service.ndp.selected(body.endpoint_id, body.dataset_id, body.resource_ids)
            if dataset.endpoint.authenticated and not account_token():
                raise PermissionError(
                    "Sign in to NDP before attaching protected Endpoint resources"
                )
            for existing in service.sources(wid):
                if existing.source.provider == "ndp" and existing.connected:
                    prior = service.store.get(
                        "ndp-selection", existing.configuration.ndp_selection_id, NDPSelection
                    )
                    if prior.dataset == dataset and existing.linked_manifest_id:
                        return visible(existing)
            selection = NDPSelection(id="ndp_" + uuid.uuid4().hex, dataset=dataset)
            service.store.put("ndp-selection", selection.id, selection)
            record = service.create(
                wid,
                CreateSource(
                    provider="ndp",
                    root=dataset.id,
                    label=dataset.title,
                    mode="read_only",
                    configuration=SourceConfiguration(ndp_selection_id=selection.id),
                ),
                root,
            )
            # Materialize only the metadata manifest. Resource contents remain remote.
            link_folder(service, record)
            return visible(record)

        def serialized() -> dict[str, Any]:
            with selections:
                return select()

        return await run(serialized)

    @app.post("/v1/storage/accounts/{provider}/auth/cancel")
    async def cancel(provider: str, body: CompleteSignIn) -> dict[str, bool]:
        if provider not in {"ndp", "github", "globus", "google_drive"}:
            raise HTTPException(404, "Connected account not found")
        record = service.auth.account_login(service.principal, service.store.clio_id, provider)
        await run(lambda: service.auth.cancel(record, body.flow_id))
        return {"cancelled": True}

    @app.post("/v1/workspaces/{wid}/sources/{source_id}/auth/cancel")
    async def cancel_source(wid: str, source_id: str, body: CompleteSignIn) -> dict[str, bool]:
        workspace_root(wid)
        await run(lambda: service.auth.cancel(service.get(wid, source_id), body.flow_id))
        return {"cancelled": True}
