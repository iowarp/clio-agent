"""Memory export and import: a CLIO archive, or a list pasted from another assistant."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from clio_agent.errors import ClioError
from clio_agent.gact.memory.service import MemoryService
from clio_agent.gact.routes.memory_documents import memory_http_error, run_memory, scopes_for

_MAX_ARCHIVE = 50 * 1024 * 1024

ASSISTANT_PROMPT = (
    "I'm moving to another assistant and want to bring what you know about me. List "
    "everything you remember about me: who I am, how I like you to work, my projects and "
    "ongoing work, the people I work with, and the machines, sites and accounts I use. "
    "Write one fact per line, grouped under a short heading per topic. Write only the "
    "facts, with no commentary."
)


class AssistantImport(BaseModel):
    """Text the user pasted back from another assistant."""

    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=200_000)
    scope: str = "user"


def register_memory_transfer_routes(app: FastAPI) -> None:
    """``/v1/memory/export`` and ``/v1/memory/import``."""

    @app.get("/v1/memory/export")
    async def export_memory(
        scope: str = Query(default="all"), name: str = Query(default="")
    ) -> Response:
        """Every document of ``scope``, or only the document ``name``."""
        from clio_agent.gact.memory.archive import export_archive  # noqa: PLC0415

        def work(service: MemoryService) -> bytes:
            if name:
                stored = service.require(name)
                return export_archive(service, (stored.name.scope,), only=str(stored.name))
            return export_archive(service, scopes_for(app, scope))

        data = await run_memory(app, work)
        label = (name or scope).replace("/", "-") or "all"
        return Response(
            content=data,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="clio-memory-{label}.zip"'},
        )

    @app.post("/v1/memory/import")
    async def import_memory(request: Request) -> dict[str, Any]:
        from clio_agent.gact.memory.archive import import_archive  # noqa: PLC0415

        data = await request.body()
        if not data:
            raise HTTPException(422, detail="Send the exported .zip as the request body")
        if len(data) > _MAX_ARCHIVE:
            raise HTTPException(413, detail="The archive is too large")
        import_id = "import_" + uuid.uuid4().hex[:12]

        def work(service: MemoryService) -> dict[str, Any]:
            return import_archive(service, data, import_id=import_id)

        return await run_memory(app, work)

    @app.get("/v1/memory/import/assistant-prompt")
    async def assistant_prompt() -> dict[str, str]:
        return {"prompt": ASSISTANT_PROMPT}

    @app.post("/v1/memory/import/assistant")
    async def import_from_assistant(body: AssistantImport) -> dict[str, Any]:
        from clio_agent.gact.memory.background import import_pasted_text  # noqa: PLC0415

        import_id = "import_" + uuid.uuid4().hex[:12]
        try:
            return await asyncio.to_thread(
                import_pasted_text, app, body.text, scope=body.scope, import_id=import_id
            )
        except ClioError as error:
            raise memory_http_error(error) from error


__all__ = ["ASSISTANT_PROMPT", "register_memory_transfer_routes"]
