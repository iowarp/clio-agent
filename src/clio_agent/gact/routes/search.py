"""Web search routes: the resolved backend, a direct query, and engine API keys.

``GET /v1/search/backend`` reports which ``search.backend`` serves the ``web_search``
tool and whether it can serve now (the typed error and its fix when not).
``POST /v1/search/query`` runs one search through the same backend abstraction.
``PUT``/``DELETE /v1/search/engine-keys/{engine}`` keep an API-key engine's key in
CLIO's credential store; a key is never returned, logged or written to SearXNG's
settings.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from clio_agent.gact.routes._body import json_body
from clio_agent.gact.search_bootstrap import register_local_endpoint
from clio_agent.providers.api_key_store import ProviderApiKeyStore
from clio_agent.search.backend import SearchNotConfiguredError, resolve_search_backend
from clio_agent.search.settings import (
    API_KEY_ENGINES,
    SearchConfigurationError,
    engine_credential_ref,
    load_search_settings,
)


def _engine(name: str) -> str:
    engine = name.strip()
    if engine not in API_KEY_ENGINES:
        raise HTTPException(
            status_code=422,
            detail=f"{engine!r} takes no API key; keyed engines: {', '.join(API_KEY_ENGINES)}",
        )
    return engine


def register_search_routes(app: FastAPI) -> None:
    """Register the web search routes and point the backend at this CLIO's SearXNG."""

    register_local_endpoint(app)

    @app.get("/v1/search/backend")
    async def search_backend() -> dict[str, Any]:
        try:
            settings = load_search_settings()
        except SearchConfigurationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        backend = resolve_search_backend(settings)
        problem = await asyncio.to_thread(backend.problem)
        return {
            "backend": backend.name,
            "ready": problem is None,
            "error": problem.as_tool_result() if problem is not None else None,
            "engines": list(settings.engines),
            "dropped_engines": list(settings.dropped_engines),
            "notes": list(settings.notes),
            "keyed_engines": {
                name: bool(ProviderApiKeyStore().load(engine_credential_ref(name)))
                for name in API_KEY_ENGINES
            },
        }

    @app.post("/v1/search/query")
    async def search_query(request: Request) -> dict[str, Any]:
        body = await json_body(request, route="POST /v1/search/query")
        query = str(body.get("query") or "").strip()
        if not query:
            raise HTTPException(status_code=422, detail="query is required")
        count = body.get("count")
        try:
            backend = resolve_search_backend()
            return await asyncio.to_thread(
                backend.search, query, int(count) if count is not None else None
            )
        except SearchConfigurationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except SearchNotConfiguredError as exc:
            raise HTTPException(status_code=503, detail=exc.as_tool_result()) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.put("/v1/search/engine-keys/{engine}")
    async def save_engine_key(engine: str, request: Request) -> dict[str, Any]:
        name = _engine(engine)
        body = await json_body(request, route="PUT /v1/search/engine-keys/{engine}")
        key = str(body.get("api_key") or "").strip()
        if not key:
            raise HTTPException(status_code=422, detail="api_key is required")
        await asyncio.to_thread(ProviderApiKeyStore().save, engine_credential_ref(name), key)
        return {"engine": name, "saved": True}

    @app.delete("/v1/search/engine-keys/{engine}")
    async def clear_engine_key(engine: str) -> dict[str, Any]:
        name = _engine(engine)
        removed = await asyncio.to_thread(ProviderApiKeyStore().clear, engine_credential_ref(name))
        return {"engine": name, "removed": removed}
