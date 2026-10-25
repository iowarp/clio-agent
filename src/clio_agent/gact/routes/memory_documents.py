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
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from clio_agent.arc import memory_document as md
from clio_agent.errors import ClioError
from clio_agent.gact.memory.scope import USER_SCOPE, parse_record_name, project_scope, record_name
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


class ManualDocumentCreate(BaseModel):
    """A memory document written directly by the user, without a model pass."""

    model_config = ConfigDict(extra="forbid")
    scope: str
    category: md.Category
    slug: str
    description: str = ""
    facts: list[str]


def _page_author(action: str) -> Author:
    return Author("user_edit", f"memory_page:{action}", session=PAGE_SESSION)


def _usage(app: FastAPI, name: str) -> dict[str, Any]:
    from clio_agent.gact.memory.runtime import memory_usage  # noqa: PLC0415
    from clio_agent.gact.memory.usage import document_subject  # noqa: PLC0415

    counted = memory_usage(app).get(document_subject(name))
    return {"read": counted.read, "loaded": counted.loaded, "last_read": counted.last_read}


def _manual_facts(
    facts: list[str], stamp: str, taken: set[str] | None = None
) -> tuple[md.Fact, ...]:
    """Create unique user-authored fact records for the manual form."""
    taken = set(taken or ())
    rows = []
    for text in facts:
        fact_id = md.new_fact_id(taken)
        taken.add(fact_id)
        rows.append(
            md.Fact(
                id=fact_id,
                text=text,
                source="user",
                session=PAGE_SESSION,
                at=stamp,
            )
        )
    return tuple(rows)


def _scope_fact_ids(service: MemoryService, scope: str) -> set[str]:
    """Avoid reusing fact IDs already live or forgotten in the target scope."""
    taken = {fact.id for stored in service.documents((scope,)) for fact in stored.document.facts}
    return taken | set(service.forgotten(scope))


def register_memory_document_routes(app: FastAPI) -> None:
    """The memory page's API under ``/v1/memory``."""

    @app.get("/v1/memory/creation-runs")
    async def creation_runs(
        limit: int = Query(default=50, ge=1, le=100), cursor: str | None = Query(default=None)
    ) -> dict[str, Any]:
        from clio_agent.gact.memory.creation_runs import (  # noqa: PLC0415
            curator_run_outcome,
            failed_memory_tool_count,
            legacy_run_rows,
            load_run_rows,
            proposal_summaries,
            public_run,
            update_run,
        )

        def load(service: MemoryService) -> tuple[list[dict[str, Any]], str | None]:
            legacy = legacy_run_rows(app.state.sessions.list(), service.store)
            return load_run_rows(service.store, limit=limit, cursor=cursor, extra_rows=legacy)

        try:
            rows, next_cursor = await run_memory(app, load)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

        def read_owners() -> list[tuple[Any, Any, int]]:
            observations = []
            for row in rows:
                handle = str(row.get("handle") or "")
                curator_session = str(row.get("curator_session") or "")
                task = app.state.task_history.task_snapshot(handle) if handle else None
                if (
                    task is None
                    or task.kind != "Session"
                    or task.owner.session_id != curator_session
                ):
                    observations.append((None, None, 0))
                else:
                    messages = app.state.messages.get(curator_session, [])
                    observations.append(
                        (
                            task,
                            app.state.sessions.get(curator_session),
                            failed_memory_tool_count(messages),
                        )
                    )
            return observations

        observations = await app.state.task_history.io.run(read_owners)

        def refresh_page(
            service: MemoryService,
            selected_rows: list[dict[str, Any]] = rows,
            selected_observations: list[tuple[Any, Any, int]] = observations,
        ) -> list[dict[str, Any]]:
            summaries = proposal_summaries(
                service,
                {
                    (str(row.get("origin") or ""), str(row.get("handle") or ""))
                    for row in selected_rows
                },
            )
            projected_rows = []
            for selected_row, (selected_task, selected_curator, failed_tool_count) in zip(
                selected_rows, selected_observations, strict=True
            ):
                origin = str(selected_row.get("origin") or "")
                handle = str(selected_row.get("handle") or "")
                counts, targets = summaries[(origin, handle)]
                patch: dict[str, Any] = {"proposal_counts": counts}
                if origin != "manual":
                    patch["targets"] = targets
                if (
                    selected_task is None
                    and selected_row.get("handle")
                    and selected_row.get("state") == "running"
                ):
                    patch["state"] = "unknown"
                    patch["reason"] = "curator_task_unavailable"
                if selected_task is not None:
                    terminal = selected_task.terminal_at is not None
                    if terminal:
                        patch["state"], patch["reason"] = curator_run_outcome(
                            str(selected_task.status), counts, failed_tool_count
                        )
                    else:
                        patch["state"] = "running"
                        patch["reason"] = ""
                    if terminal:
                        patch["completed_at"] = str(selected_task.terminal_at)
                if selected_curator is not None:
                    patch["workspace_id"] = str(selected_curator.workspace_id or "")
                    actual_model = (selected_curator.metadata or {}).get("memory_creation_model")
                    if isinstance(actual_model, dict) and actual_model.get("model_id"):
                        patch["models"] = {"curator": actual_model}
                    patch["_curator_usage"] = _session_usage(selected_curator)
                stored = update_run(service.store, str(selected_row.get("id") or ""), patch)
                if stored is not None:
                    projected = stored
                else:
                    projected = {**selected_row, **patch}
                    if isinstance(selected_row.get("models"), dict) and isinstance(
                        patch.get("models"), dict
                    ):
                        projected["models"] = {**selected_row["models"], **patch["models"]}
                projected_rows.append(public_run(projected))
            return projected_rows

        public_rows = await run_memory(app, refresh_page)

        return {
            "runs": public_rows,
            "next_cursor": next_cursor,
            "background": getattr(app.state, "memory_background", None),
        }

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

    @app.post("/v1/memory/documents")
    async def create_document(body: ManualDocumentCreate) -> dict[str, Any]:
        if not body.facts or any(
            not isinstance(fact, str) or not fact.strip() for fact in body.facts
        ):
            raise HTTPException(422, "At least one nonempty fact is required.")
        normalized = [" ".join(fact.split()) for fact in body.facts]
        if len({fact.casefold() for fact in normalized}) != len(normalized):
            raise HTTPException(422, "Facts must be unique within a new document.")
        try:
            slug = body.category if body.category in md.SINGLETON_CATEGORIES else body.slug
            address = record_name(body.scope, body.category, slug)
        except md.MemoryDocumentError as error:
            raise memory_http_error(error) from error
        if address.scope.startswith("project/") and address.scope.removeprefix("project/") not in {
            workspace.id for workspace in app.state.workspaces.list()
        }:
            raise memory_http_error(_scope_error(body.scope))

        def work(service: MemoryService) -> dict[str, Any]:
            stamp = md.today()
            run_id = "mcr_manual_" + uuid4().hex
            started = md.now_stamp()
            from clio_agent.gact.memory.creation_runs import begin_run, update_run  # noqa: PLC0415

            begin_run(
                service.store,
                run_id=run_id,
                origin="manual",
                workspace_id=(
                    address.scope.removeprefix("project/")
                    if address.scope.startswith("project/")
                    else ""
                ),
                started_at=started,
            )
            try:
                current = service.get(address)
                if body.category in md.SINGLETON_CATEGORIES:
                    from clio_agent.gact.memory.service import MemoryConflict  # noqa: PLC0415

                    for _attempt in range(3):
                        current = service.get(address)
                        held = (
                            {fact.text.casefold() for fact in current.document.live_facts}
                            if current
                            else set()
                        )
                        additions = [fact for fact in normalized if fact.casefold() not in held]
                        if not additions:
                            update_run(
                                service.store,
                                run_id,
                                {
                                    "state": "completed",
                                    "reason": "already_present",
                                    "completed_at": md.now_stamp(),
                                    "targets": [str(address)],
                                    "_extract_usage": _known_zero_usage(),
                                    "_curator_usage": _known_zero_usage(),
                                },
                            )
                            return stored_to_wire(current) if current is not None else {}
                        base = (
                            current.document
                            if current is not None
                            else md.MemoryDocument(
                                name=address.slug,
                                description=" ".join(body.description.split())
                                or f"User {body.category}.",
                                category=body.category,
                                updated=md.now_stamp(),
                            )
                        )
                        document = base
                        for fact in _manual_facts(
                            additions, stamp, _scope_fact_ids(service, address.scope)
                        ):
                            document = md.with_fact(document, fact, updated=md.now_stamp())
                        try:
                            stored = service.write(
                                address,
                                document,
                                expected_sha=current.sha if current else None,
                                author=_page_author("create"),
                            )
                            break
                        except MemoryConflict:
                            if _attempt == 2:
                                raise
                    else:
                        raise RuntimeError("singleton memory update did not settle")
                else:
                    if current is not None:
                        from clio_agent.gact.memory.service import MemoryConflict  # noqa: PLC0415

                        raise MemoryConflict(str(address), expected=None, current=current.sha)
                    document = md.MemoryDocument(
                        name=address.slug,
                        description=" ".join(body.description.split()),
                        category=body.category,
                        updated=md.now_stamp(),
                        facts=_manual_facts(
                            normalized, stamp, _scope_fact_ids(service, address.scope)
                        ),
                    )
                    stored = service.write(
                        address,
                        document,
                        expected_sha=None,
                        author=_page_author("create"),
                    )
            except ClioError as error:
                update_run(
                    service.store,
                    run_id,
                    {
                        "state": "failed",
                        "reason": error.error_type,
                        "completed_at": md.now_stamp(),
                        "_extract_usage": _known_zero_usage(),
                        "_curator_usage": _known_zero_usage(),
                    },
                )
                raise
            update_run(
                service.store,
                run_id,
                {
                    "state": "completed",
                    "reason": "created_by_user",
                    "completed_at": started,
                    "targets": [str(address)],
                    "_extract_usage": _known_zero_usage(),
                    "_curator_usage": _known_zero_usage(),
                },
            )
            return stored_to_wire(stored)

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
                "background": getattr(app.state, "memory_background", None),
            }

        return await run_memory(app, work)


def proposal_wire(proposal: Any) -> dict[str, Any]:
    """The JSON shape of a proposal for the inbox."""
    from dataclasses import asdict  # noqa: PLC0415

    return {**asdict(proposal), "needs_review": proposal.needs_review}


def _session_usage(session: Any) -> dict[str, Any]:
    """Project only model usage present in the curator session's existing rollup."""
    known = bool(getattr(session, "cost_known", False))
    by_model = getattr(session, "usage_by_model", None) or {}
    tokens_known = bool(by_model) or bool(
        getattr(session, "tokens_input", 0) or getattr(session, "tokens_output", 0)
    )
    return {
        "input_tokens": max(0, int(getattr(session, "tokens_input", 0) or 0))
        if tokens_known
        else None,
        "output_tokens": max(0, int(getattr(session, "tokens_output", 0) or 0))
        if tokens_known
        else None,
        "input_known": tokens_known,
        "output_known": tokens_known,
        "cost_usd": float(getattr(session, "cost_usd", 0) or 0) if known else None,
        "cost_known": known,
    }


def _known_zero_usage() -> dict[str, Any]:
    """No model call was made for a manual memory write."""
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "input_known": True,
        "output_known": True,
        "cost_usd": 0.0,
        "cost_known": True,
    }


__all__ = [
    "memory_http_error",
    "proposal_wire",
    "register_memory_document_routes",
    "run_memory",
    "scopes_for",
]
