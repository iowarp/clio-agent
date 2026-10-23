"""Long-term memory over HTTP: documents, facts, history, search and the inbox.

The memory page is the place the user sees and edits everything the agent can read:
every document, every version, every pending proposal. Edits made here are user
edits: an inline correction supersedes the fact with ``source: user``, a deletion
forgets it. Every write goes through the one memory service and names the hash of the
document the page showed, so a stale page gets ``memory_conflict`` (HTTP 409).
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from clio_agent.arc import memory_document as md
from clio_agent.errors import ClioError
from clio_agent.gact.memory.scope import USER_SCOPE, parse_record_name, project_scope
from clio_agent.gact.memory.service import Author, MemoryService, stored_to_wire
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

PAGE_SESSION = "memory_page"
_STATUS = {
    "memory_conflict": 409,
    "memory_not_found": 404,
    "memory_document_invalid": 422,
    "memory_unavailable": 503,
    "memory_proposal_not_pending": 409,
    "memory_proposal_stale": 409,
    "search_unavailable": 503,
    "memory_scope_invalid": 400,
}


def memory_http_error(error: ClioError) -> HTTPException:
    """The typed envelope of a memory error."""
    code = str(getattr(error, "error_type", "") or "memory_error")
    status = _STATUS.get(code, 422)
    return HTTPException(
        status_code=status,
        detail=ErrorEnvelope(
            error=ErrorInfo(
                error=code,
                message=str(error),
                details=dict(getattr(error, "details", {}) or {}),
                recoverable=status < 500,
            )
        ).model_dump(exclude_none=True),
    )


async def run_memory(app: FastAPI, work: Any, *args: Any, **kwargs: Any) -> Any:
    """Run ``work(service, ...)`` off the loop, mapping memory errors to HTTP."""
    from clio_agent.gact.memory.runtime import memory_service  # noqa: PLC0415

    def call() -> Any:
        return work(memory_service(app), *args, **kwargs)

    try:
        return await asyncio.to_thread(call)
    except ClioError as error:
        raise memory_http_error(error) from error


def scopes_for(app: FastAPI, scope: str) -> tuple[str, ...]:
    """``all`` (account plus every workspace with memory), ``user`` or ``project/<id>``.

    Raises:
        ClioError: ``memory_scope_invalid`` for anything else (never read as ``all``).
    """
    if scope == "user":
        return (USER_SCOPE,)
    if scope.startswith("project/"):
        try:
            return (project_scope(scope.removeprefix("project/")),)
        except md.MemoryDocumentError as error:
            raise _scope_error(scope) from error
    if scope != "all":
        raise _scope_error(scope)
    workspaces = [w.id for w in app.state.workspaces.list()]
    projects = []
    for workspace_id in workspaces:
        try:
            projects.append(project_scope(workspace_id))
        except md.MemoryDocumentError:
            continue  # ws_global holds no project memory
    return (USER_SCOPE, *projects)


def _scope_error(scope: str) -> ClioError:
    return ClioError(
        f"unknown memory scope {scope!r}: use all, user or project/<workspace id>",
        error_type="memory_scope_invalid",
        details={"scope": scope},
    )


class FactEdit(BaseModel):
    """An inline correction from the memory page."""

    model_config = ConfigDict(extra="forbid")
    fact: str
    expected_sha: str


class Restore(BaseModel):
    """Restore one version."""

    model_config = ConfigDict(extra="forbid")
    version: int


class Accept(BaseModel):
    """Accept a proposal, optionally with its facts edited first."""

    model_config = ConfigDict(extra="forbid")
    facts: list[str] | None = None


def _page_author(action: str) -> Author:
    return Author("user_edit", f"memory_page:{action}", session=PAGE_SESSION)


def _usage(app: FastAPI, name: str) -> dict[str, Any]:
    from clio_agent.gact.memory.runtime import memory_usage  # noqa: PLC0415
    from clio_agent.gact.memory.usage import document_subject  # noqa: PLC0415

    counted = memory_usage(app).get(document_subject(name))
    return {"read": counted.read, "loaded": counted.loaded, "last_read": counted.last_read}


def register_memory_document_routes(app: FastAPI) -> None:
    """The memory page's API under ``/v1/memory``."""

    @app.get("/v1/memory/documents")
    async def list_documents(scope: str = Query(default="all")) -> dict[str, Any]:
        def work(service: MemoryService) -> dict[str, Any]:
            documents = service.documents(scopes_for(app, scope))
            return {
                "documents": [
                    {**stored_to_wire(s), "usage": _usage(app, str(s.name))} for s in documents
                ]
            }

        return await run_memory(app, work)

    @app.get("/v1/memory/documents/{name:path}/versions")
    async def document_versions(name: str) -> dict[str, Any]:
        def work(service: MemoryService) -> dict[str, Any]:
            parse_record_name(name)
            return {
                "name": name,
                "versions": [
                    {"n": v.n, "text": v.text, "author": v.author, "at": v.at}
                    for v in service.versions(name)
                ],
            }

        return await run_memory(app, work)

    @app.post("/v1/memory/documents/{name:path}/restore")
    async def restore_document(name: str, body: Restore) -> dict[str, Any]:
        def work(service: MemoryService) -> dict[str, Any]:
            stored = service.restore(name, body.version, author=_page_author("restore"))
            return stored_to_wire(stored)

        return await run_memory(app, work)

    @app.post("/v1/memory/documents/{name:path}/facts/{fact_id}")
    async def edit_fact(name: str, fact_id: str, body: FactEdit) -> dict[str, Any]:
        def work(service: MemoryService) -> dict[str, Any]:
            stored = service.require(name)
            if stored.sha != body.expected_sha:
                from clio_agent.gact.memory.service import MemoryConflict  # noqa: PLC0415

                raise MemoryConflict(name, expected=body.expected_sha, current=stored.sha)
            taken = {f.id for f in stored.document.facts} | set(
                service.forgotten(stored.name.scope)
            )
            replacement = md.Fact(
                id=md.new_fact_id(taken),
                text=" ".join(body.fact.split()),
                source="user",
                session=PAGE_SESSION,
                at=md.today(),
            )
            document = md.superseding(stored.document, fact_id, replacement, updated=md.now_stamp())
            written = service.write(
                stored.name, document, expected_sha=stored.sha, author=_page_author("edit")
            )
            return stored_to_wire(written)

        return await run_memory(app, work)

    @app.delete("/v1/memory/documents/{name:path}/facts/{fact_id}")
    async def delete_fact(name: str, fact_id: str, expected_sha: str) -> dict[str, Any]:
        def work(service: MemoryService) -> dict[str, Any]:
            stored = service.require(name)
            written, removed = service.forget_fact(
                stored, fact_id, expected_sha=expected_sha, author=_page_author("forget")
            )
            if written is None:  # its last fact: the document is gone
                return {"name": name, "removed": True, "forgotten": list(removed)}
            return {**stored_to_wire(written), "forgotten": list(removed)}

        return await run_memory(app, work)

    @app.get("/v1/memory/documents/{name:path}")
    async def get_document(name: str) -> dict[str, Any]:
        def work(service: MemoryService) -> dict[str, Any]:
            stored = service.require(name)
            return {**stored_to_wire(stored), "usage": _usage(app, name)}

        return await run_memory(app, work)

    @app.delete("/v1/memory/documents/{name:path}")
    async def delete_document(name: str, expected_sha: str) -> dict[str, Any]:
        def work(service: MemoryService) -> dict[str, Any]:
            stored = service.require(name)
            author = _page_author("forget")
            service.remove(stored.name, expected_sha=expected_sha, author=author)
            service.forget(
                stored.name.scope, tuple(f.id for f in stored.document.facts), author=author
            )
            return {"name": name, "removed": True}

        return await run_memory(app, work)

    @app.delete("/v1/memory/documents")
    async def delete_all_documents(scope: str = Query(), confirm: bool = False) -> dict[str, Any]:
        """Remove every document of ``scope`` (``confirm=true``); each keeps its history."""
        if not confirm:
            raise HTTPException(422, detail="Pass confirm=true to delete all memory in scope")

        def work(service: MemoryService) -> dict[str, Any]:
            removed = []
            for stored in service.documents(scopes_for(app, scope)):
                service.remove(stored.name, expected_sha=stored.sha, author=_page_author("clear"))
                removed.append(str(stored.name))
            return {"removed": removed}

        return await run_memory(app, work)

    @app.get("/v1/memory/search")
    async def search_memory(
        q: str = Query(min_length=1, max_length=500), scope: str = Query(default="all")
    ) -> dict[str, Any]:
        def work(service: MemoryService) -> dict[str, Any]:
            hits = service.search(scopes_for(app, scope), q, limit=20)
            return {
                "query": q,
                "hits": [
                    {
                        "name": hit.name,
                        "category": hit.category,
                        "description": hit.description,
                        "score": hit.score,
                        "facts": [{"id": f.id, "text": f.text} for f in hit.facts],
                    }
                    for hit in hits
                ],
            }

        return await run_memory(app, work)

    @app.get("/v1/memory/proposals")
    async def list_proposals(status: str = Query(default="pending")) -> dict[str, Any]:
        from clio_agent.gact.memory.proposals import ProposalStore  # noqa: PLC0415

        def work(service: MemoryService) -> dict[str, Any]:
            proposals = ProposalStore(service).all()
            if status != "all":
                proposals = [p for p in proposals if p.status == status]
            return {"proposals": [proposal_wire(p) for p in proposals]}

        return await run_memory(app, work)

    @app.post("/v1/memory/proposals/{proposal_id}/accept")
    async def accept_proposal(proposal_id: str, body: Accept) -> dict[str, Any]:
        from clio_agent.gact.memory.proposals import ProposalStore  # noqa: PLC0415

        def work(service: MemoryService) -> dict[str, Any]:
            accepted = ProposalStore(service).accept(
                proposal_id, decided_by="user", edited_facts=body.facts
            )
            return proposal_wire(accepted)

        return await run_memory(app, work)

    @app.post("/v1/memory/proposals/{proposal_id}/reject")
    async def reject_proposal(proposal_id: str) -> dict[str, Any]:
        from clio_agent.gact.memory.proposals import ProposalStore  # noqa: PLC0415

        def work(service: MemoryService) -> dict[str, Any]:
            return proposal_wire(ProposalStore(service).reject(proposal_id, decided_by="user"))

        return await run_memory(app, work)

    @app.get("/v1/memory/summary")
    async def memory_summary() -> dict[str, Any]:
        from clio_agent.gact.memory.policy import memory_enabled  # noqa: PLC0415
        from clio_agent.gact.memory.proposals import ProposalStore  # noqa: PLC0415

        def work(service: MemoryService) -> dict[str, Any]:
            pending = ProposalStore(service).pending()
            return {
                "enabled": memory_enabled(),
                "inbox": len(pending),
                "documents": len(service.documents(scopes_for(app, "all"))),
            }

        return await run_memory(app, work)


def proposal_wire(proposal: Any) -> dict[str, Any]:
    """The JSON shape of a proposal for the inbox."""
    from dataclasses import asdict  # noqa: PLC0415

    return {**asdict(proposal), "needs_review": proposal.needs_review}


__all__ = [
    "memory_http_error",
    "proposal_wire",
    "register_memory_document_routes",
    "run_memory",
    "scopes_for",
]
