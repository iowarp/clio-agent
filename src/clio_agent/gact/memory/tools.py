"""The long-term memory tools the ``memory`` plugin claims.

``memory_search`` and ``memory_read`` serve every session that has memory. The three
writes -- ``create_memory``, ``update_memory``, ``forget_memory`` -- run only in a
root conversation or a fork of one, when the user asks to remember, correct or
forget something; they apply at once with ``source: user`` and skip review. Every
write goes through :class:`~clio_agent.gact.memory.service.MemoryService`.

``create_memory`` routes the fact with one model call on the session's model: given
the fact and the index of its category, the model names an existing document or a new
slug and description. The server writes the line; routing never merges or rewrites
other facts (that is the background pass's job).
"""

from __future__ import annotations

from typing import Any

from clio_agent.arc import memory_document as md
from clio_agent.errors import ClioError
from clio_agent.gact.memory.policy import MemoryPolicy, active_policy
from clio_agent.gact.memory.scope import USER_SCOPE, project_scope, record_name
from clio_agent.gact.memory.service import (
    Author,
    MemoryConflict,
    MemoryService,
    StoredDocument,
)
from clio_agent.gact.memory.usage import document_subject

_SEARCH_LIMIT = 20
_CONFLICT_RETRIES = 3


class MemoryToolError(ClioError):
    """A memory tool call was refused (typed reason the model can act on)."""

    def __init__(self, reason: str, message: str, **details: Any) -> None:
        self.reason = reason
        super().__init__(message, error_type=reason, details={"reason": reason, **details})


def _context() -> tuple[Any, Any, MemoryPolicy, MemoryService]:
    from clio_agent.gact.memory.runtime import memory_service  # noqa: PLC0415

    active = active_policy()
    if active is None:
        raise MemoryToolError("memory_needs_a_session", "memory tools run inside a session")
    app, session, policy = active
    if not policy.enabled:
        raise MemoryToolError(policy.reason, f"memory is off for this session ({policy.reason})")
    return app, session, policy, memory_service(app)


def _turn_id() -> str:
    from clio_agent.gact import context  # noqa: PLC0415

    return context.active_turn_id() or context.active_session_id()


def _note(app: Any, name: str, event: str) -> None:
    from clio_agent.gact.memory.runtime import memory_usage  # noqa: PLC0415

    memory_usage(app).note(_turn_id(), document_subject(name), event)  # type: ignore[arg-type]


def _author(session: Any, tool: str, reason: str) -> Author:
    return Author("tool_call", f"{_turn_id()}:{tool}", session=session.id, reason=reason[:500])


def _in_scope(policy: MemoryPolicy, name: str) -> None:
    if not any(name.startswith(scope + "/") for scope in policy.scopes):
        raise MemoryToolError(
            "memory_out_of_scope",
            f"{name} is not in this session's memory ({', '.join(policy.scopes)})",
            name=name,
        )


def _fact_wire(fact: md.Fact) -> dict[str, str]:
    return {"id": fact.id, "text": fact.text, "source": fact.source, "at": fact.at}


def memory_search(query: str, category: str = "", limit: int = 8) -> dict[str, Any]:
    """BM25 over the session's memory: names, descriptions, scores and matching facts."""
    _app, _session, policy, service = _context()
    if category and category not in md.CATEGORIES:
        raise MemoryToolError("memory_category_unknown", f"unknown category {category!r}")
    hits = service.search(
        policy.scopes,
        query,
        category=category or None,
        limit=max(1, min(int(limit), _SEARCH_LIMIT)),
    )
    return {
        "tool": "memory_search",
        "query": query,
        "hits": [
            {
                "name": hit.name,
                "category": hit.category,
                "description": hit.description,
                "score": round(hit.score, 4),
                "facts": [_fact_wire(fact) for fact in hit.facts],
            }
            for hit in hits
        ],
    }


def memory_read(name: str) -> dict[str, Any]:
    """The full document, superseded facts marked."""
    app, _session, policy, service = _context()
    _in_scope(policy, name)
    stored = service.get(name)
    if stored is None:
        raise MemoryToolError("memory_not_found", f"no memory document {name}", name=name)
    _note(app, name, "read")
    return {
        "tool": "memory_read",
        "name": name,
        "description": stored.document.description,
        "text": stored.text,
        "facts": [{**_fact_wire(fact), "live": fact.live} for fact in stored.document.facts],
    }


def _write_scope(session: Any, policy: MemoryPolicy, scope: str) -> str:
    if scope in ("", USER_SCOPE):
        target = USER_SCOPE
    elif scope == "project":
        try:
            target = project_scope(str(session.workspace_id or ""))
        except md.MemoryDocumentError:
            raise MemoryToolError(
                "memory_no_project", "this conversation is not in a workspace with project memory"
            ) from None
    else:
        raise MemoryToolError("memory_scope_unknown", "scope is 'user' or 'project'")
    if target not in policy.scopes:
        raise MemoryToolError("memory_out_of_scope", f"{target} memory is off in this workspace")
    return target


def _route(fact: str, subject: str, category: str, index: list[StoredDocument]) -> tuple[str, str]:
    """``(slug, description)`` of the document the fact belongs in (one model call)."""
    from clio_agent.gact.memory.routing import route_fact  # noqa: PLC0415

    return route_fact(
        fact=fact,
        subject=subject,
        category=category,
        documents=[(stored.name.slug, stored.document.description) for stored in index],
    )


def _already_held(current: StoredDocument | None, fact: str) -> dict[str, Any] | None:
    """The result for a fact the document already states (one model call), else None."""
    from clio_agent.gact.memory.routing import held_fact  # noqa: PLC0415

    if current is None:
        return None
    live = current.document.live_facts
    same = held_fact(fact=fact, facts=[(held.id, held.text) for held in live])
    held = current.document.fact(same) if same else None
    if held is None:
        return None
    return {
        "tool": "create_memory",
        "fact": _fact_wire(held),
        "memory": _notice("already_remembered", current, held.id),
    }


def _new_fact(session: Any, text: str, taken: set[str]) -> md.Fact:
    return md.Fact(
        id=md.new_fact_id(taken),
        text=" ".join(text.split()),
        source="user",
        session=session.id,
        at=md.today(),
    )


def _all_fact_ids(service: MemoryService, scope: str) -> set[str]:
    taken = {fact.id for stored in service.documents((scope,)) for fact in stored.document.facts}
    return taken | set(service.forgotten(scope))


def _notice(action: str, stored: StoredDocument, fact_id: str = "") -> dict[str, Any]:
    return {
        "action": action,
        "name": str(stored.name),
        "category": stored.name.category,
        "description": stored.document.description,
        "fact_id": fact_id,
    }


def create_memory(
    subject: str, fact: str, category: str, scope: str = "user", reason: str = ""
) -> dict[str, Any]:
    """Remember a fact the user asked to remember."""
    _app, session, policy, service = _context()
    if not policy.may_write:
        raise MemoryToolError("memory_write_root_only", "only the main conversation writes memory")
    if category not in md.CATEGORIES:
        raise MemoryToolError("memory_category_unknown", f"unknown category {category!r}")
    if not " ".join(fact.split()):
        raise MemoryToolError("memory_fact_empty", "the fact is empty")
    target = _write_scope(session, policy, scope)
    if category in md.SINGLETON_CATEGORIES:
        service.ensure_singleton(target, category)
        slug, description = category, ""
    else:
        index = [s for s in service.documents((target,)) if s.name.category == category]
        slug, description = _route(fact, subject, category, index)
    address = record_name(target, category, slug)
    if (known := _already_held(service.get(address), fact)) is not None:
        return known
    for _attempt in range(_CONFLICT_RETRIES):
        current = service.get(address)
        new = _new_fact(session, fact, _all_fact_ids(service, target))
        base = (
            current.document
            if current
            else md.MemoryDocument(
                name=slug,
                description=" ".join((description or subject or slug).split()),
                category=category,  # type: ignore[arg-type]
                updated=md.now_stamp(),
            )
        )
        document = md.with_fact(base, new, updated=md.now_stamp())
        try:
            stored = service.write(
                address,
                document,
                expected_sha=current.sha if current else None,
                author=_author(session, "create_memory", reason),
            )
        except MemoryConflict:
            continue
        return {
            "tool": "create_memory",
            "fact": _fact_wire(new),
            "memory": _notice("created", stored, new.id),
        }
    raise MemoryToolError(
        "memory_conflict", f"{address} kept changing; try again", name=str(address)
    )


def _find_fact(service: MemoryService, policy: MemoryPolicy, fact_id: str) -> StoredDocument:
    for stored in service.documents(policy.scopes):
        fact = stored.document.fact(fact_id)
        if fact is not None:
            return stored
    raise MemoryToolError("memory_fact_not_found", f"no fact {fact_id} in this session's memory")


def update_memory(fact_id: str, fact: str, reason: str = "") -> dict[str, Any]:
    """Supersede a fact with a corrected one (the old one stays, struck through)."""
    _app, session, policy, service = _context()
    if not policy.may_write:
        raise MemoryToolError("memory_write_root_only", "only the main conversation writes memory")
    for _attempt in range(_CONFLICT_RETRIES):
        stored = _find_fact(service, policy, fact_id)
        old = stored.document.fact(fact_id)
        if old is None or not old.live:
            raise MemoryToolError("memory_fact_superseded", f"fact {fact_id} was already replaced")
        new = _new_fact(session, fact, _all_fact_ids(service, stored.name.scope))
        document = md.superseding(stored.document, fact_id, new, updated=md.now_stamp())
        try:
            written = service.write(
                stored.name,
                document,
                expected_sha=stored.sha,
                author=_author(session, "update_memory", reason),
            )
        except MemoryConflict:
            continue
        return {
            "tool": "update_memory",
            "fact": _fact_wire(new),
            "previous_fact": _fact_wire(old),
            "superseded": fact_id,
            "memory": _notice("updated", written, new.id),
        }
    raise MemoryToolError("memory_conflict", f"fact {fact_id} kept changing; try again")


def forget_memory(fact_id: str = "", name: str = "", reason: str = "") -> dict[str, Any]:
    """Remove a fact (its line is deleted) or a whole document, and never re-learn it."""
    _app, session, policy, service = _context()
    if not policy.may_write:
        raise MemoryToolError("memory_write_root_only", "only the main conversation writes memory")
    if bool(fact_id) == bool(name):
        raise MemoryToolError("memory_forget_target", "give exactly one of fact_id or name")
    author = _author(session, "forget_memory", reason)
    if name:
        _in_scope(policy, name)
        stored = service.get(name)
        if stored is None:
            raise MemoryToolError("memory_not_found", f"no memory document {name}", name=name)
        service.remove(stored.name, expected_sha=stored.sha, author=author)
        forgotten = tuple(fact.id for fact in stored.document.facts)
        service.forget(stored.name.scope, forgotten, author=author)
        return {
            "tool": "forget_memory",
            "forgotten": list(forgotten),
            "forgotten_facts": [_fact_wire(fact) for fact in stored.document.live_facts],
            "document_removed": True,
            "memory": _notice("forgotten", stored),
        }
    for _attempt in range(_CONFLICT_RETRIES):
        stored = _find_fact(service, policy, fact_id)
        old = stored.document.fact(fact_id)
        try:
            written, removed = service.forget_fact(
                stored, fact_id, expected_sha=stored.sha, author=author
            )
        except MemoryConflict:
            continue
        return {
            "tool": "forget_memory",
            **({"fact": _fact_wire(old)} if old is not None else {}),
            "forgotten": list(removed),
            "document_removed": written is None,
            "memory": _notice("forgotten", written or stored, fact_id),
        }
    raise MemoryToolError("memory_conflict", f"fact {fact_id} kept changing; try again")


__all__ = [
    "MemoryToolError",
    "create_memory",
    "forget_memory",
    "memory_read",
    "memory_search",
    "update_memory",
]
