"""Workspace-scoped knowledge graph, search and immutable note reads."""

from __future__ import annotations

import asyncio
import subprocess
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from clio_agent.gact.storage.knowledge_service import KnowledgeService
from clio_agent.gact.storage.names import plain_error


class OpenNote(BaseModel):
    """A selected source-relative note and its optional expected content revision."""

    model_config = ConfigDict(extra="forbid")
    path: str
    revision: str = ""


def register_knowledge_routes(app: FastAPI) -> None:
    """Attach knowledge operations to the existing source and resource owners."""
    service = KnowledgeService(
        app.state.connected_storage, getattr(app.state, "resource_store", None)
    )
    app.state.knowledge = service

    async def run(action: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(action)
        except KeyError as exc:
            raise HTTPException(404, "Knowledge source or note was not found") from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(409, "Headless Sync timed out; check private client setup") from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(409, plain_error(exc)) from exc

    @app.get("/v1/workspaces/{wid}/sources/{source_id}/knowledge")
    async def snapshot(
        wid: str,
        source_id: str,
        query: str = "",
        focus: str = "",
        offset: int = Query(0, ge=0),
        limit: int = Query(200, ge=1, le=500),
    ) -> dict[str, Any]:
        """Refresh changed notes and return a bounded graph/list from one index."""
        return await run(
            lambda: service.index.snapshot(
                service.record(wid, source_id), query=query, focus=focus, offset=offset, limit=limit
            ).model_dump(mode="json")
        )

    @app.post("/v1/workspaces/{wid}/sources/{source_id}/knowledge/open")
    async def open_note(wid: str, source_id: str, body: OpenNote) -> dict[str, Any]:
        """Read selected note content with a revision-bound workspace resource."""
        return await run(lambda: service.open(wid, source_id, body.path, body.revision))

    @app.get("/v1/workspaces/{wid}/sources/{source_id}/knowledge/graph")
    async def graph(
        wid: str, source_id: str, query: str = "", focus: str = "", attachments: bool = False
    ) -> dict[str, Any]:
        """Return whole-source note topology independently of file pagination."""
        return await run(
            lambda: service.index.graph(
                service.record(wid, source_id), query=query, focus=focus, attachments=attachments
            ).model_dump(mode="json")
        )

    @app.get("/v1/workspaces/{wid}/sources/{source_id}/knowledge/sync")
    async def sync_status(wid: str, source_id: str) -> dict[str, Any]:
        """Report sync separately from indexing and note access."""
        return await run(
            lambda: service.storage.obsidian_sync.status(service.record(wid, source_id))
        )

    @app.post("/v1/workspaces/{wid}/sources/{source_id}/knowledge/sync/{action}")
    async def sync_action(wid: str, source_id: str, action: str) -> dict[str, Any]:
        """Start or stop the official client on an explicitly configured sync copy."""

        def change() -> dict[str, Any]:
            record = service.record(wid, source_id)
            if action == "start":
                return service.storage.obsidian_sync.start(record)
            if action != "stop":
                raise KeyError(action)
            service.storage.obsidian_sync.stop(source_id)
            return service.storage.obsidian_sync.status(record)

        return await run(change)
