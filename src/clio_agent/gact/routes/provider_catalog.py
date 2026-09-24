"""Normalized provider/model catalog route for the React client."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException

from clio_agent.gact.provider_catalog_snapshot import UnknownCatalogProviderError, read_catalog
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps


def register_normalized_provider_catalog_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register the capability-evidence provider catalog."""

    del deps

    @app.get("/v1/provider-catalog")
    async def provider_catalog(refresh: bool = False, provider: str = "") -> dict[str, object]:
        """Serve the provider catalog snapshot.

        ``refresh=true`` re-probes every provider, or only ``provider`` when it is
        given (that provider's entry is merged into the snapshot). A provider whose
        sign-in or explicit check just completed is re-discovered on the next read
        without any flag.
        """

        try:
            return await read_catalog(app, refresh=refresh, provider_id=provider.strip())
        except UnknownCatalogProviderError as exc:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"unknown provider: {exc.args[0]}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            ) from exc


__all__ = ["register_normalized_provider_catalog_routes"]
