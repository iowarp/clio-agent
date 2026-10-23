"""The memory snapshot a session starts with, pinned for its life.

At the first turn of a root conversation (or a fork of one) the server assembles:

* the full ``profile`` and ``preferences`` documents of the session's scopes;
* an index of every other document -- name, category and description, one line each,
  most recently read first -- capped at ``memory.index_max_tokens`` (2,000) with a
  count of the documents left out.

It is pinned in session metadata (:data:`SNAPSHOT`), the way plugin resolution is, and
delivered every turn as a CLIO addition with the fixed marker ``## Memory``. The
addition is recorded in the context frame like every other one, and because its text
never changes within the session the agent loop writes it once and re-records it after
a compaction removed it (memory ledger MD7). A fork copies its parent's snapshot, so
it sees memory as of the fork. A write in the session shows in its tool result; the
next session's snapshot includes it. Delegated children get no snapshot; they search.
"""

from __future__ import annotations

from typing import Any

from clio_agent.arc.memory_document import SINGLETON_CATEGORIES, MemoryDocument
from clio_agent.gact.memory.policy import session_policy, setting
from clio_agent.gact.memory.service import MemoryService, StoredDocument
from clio_agent.gact.memory.usage import UsageCounters, document_subject
from clio_agent.gact.session_kind import session_kind

#: Session metadata holding the pinned snapshot.
SNAPSHOT = "memory_snapshot"
#: The injection source and its fixed marker.
SOURCE = "memory"
MARKER = "## Memory"
_SCOPE_LABEL = {"user": "Account"}


def _tokens(text: str) -> int:
    return (len(text) + 3) // 4


def _scope_label(scope: str) -> str:
    return _SCOPE_LABEL.get(scope, "This workspace")


def _document_lines(stored: StoredDocument) -> list[str]:
    document: MemoryDocument = stored.document
    title = f"### {_scope_label(stored.name.scope)} {document.category} ({stored.name})"
    lines = [title]
    lines.extend(f"- {fact.text} (fact {fact.id})" for fact in document.live_facts)
    if not document.live_facts:
        lines.append("- (nothing yet)")
    return lines


def build_snapshot(
    service: MemoryService, usage: UsageCounters, scopes: tuple[str, ...]
) -> dict[str, Any]:
    """Assemble the snapshot of ``scopes``: ``{"text", "loaded", "indexed", "omitted"}``."""
    documents = service.documents(scopes)
    loaded = [stored for stored in documents if stored.name.category in SINGLETON_CATEGORIES]
    others = [stored for stored in documents if stored.name.category not in SINGLETON_CATEGORIES]
    others.sort(
        key=lambda stored: usage.get(document_subject(str(stored.name))).last_read, reverse=True
    )
    lines = [
        MARKER,
        "What you remember about the user, their projects and how they work. Search and read "
        "the indexed documents with memory_search and memory_read when they bear on the work.",
    ]
    for stored in loaded:
        lines.append("")
        lines.extend(_document_lines(stored))
    cap = int(setting("memory.index_max_tokens", 2000))
    index: list[str] = []
    used = 0
    for stored in others:
        line = f"- {stored.name} ({stored.name.category}): {stored.document.description}"
        if used + _tokens(line) > cap:
            break
        index.append(line)
        used += _tokens(line)
    omitted = len(others) - len(index)
    if index or omitted:
        lines.extend(["", f"### Index ({len(others)} documents)"])
        lines.extend(index)
        if omitted:
            lines.append(f"- {omitted} more not listed; find them with memory_search.")
    return {
        # Nothing remembered yet: no addition at all (the tools' fragment covers how to
        # start), rather than an empty section on every turn.
        "text": "\n".join(lines) if documents else "",
        "loaded": [str(stored.name) for stored in loaded],
        "indexed": [line.split(" ", 2)[1] for line in index],
        "omitted": omitted,
    }


def wants_snapshot(app: Any, session: Any) -> bool:
    """Whether ``session`` carries a snapshot: memory is on and it may write (root/fork)."""
    if session_kind(session) not in ("conversation", "branch"):
        return False
    policy = session_policy(app, session)
    return policy.enabled and policy.may_write


def session_snapshot(app: Any, session: Any) -> dict[str, Any] | None:
    """The session's pinned snapshot, assembling and pinning it on first use.

    Call off the event loop. ``None`` when the session carries no snapshot.
    """
    if not wants_snapshot(app, session):
        return None
    pinned = (session.metadata or {}).get(SNAPSHOT)
    if isinstance(pinned, dict) and "text" in pinned:
        return pinned
    from clio_agent.gact.memory.runtime import memory_service, memory_usage  # noqa: PLC0415

    service = memory_service(app)
    usage = memory_usage(app)
    snapshot = build_snapshot(service, usage, session_policy(app, session).scopes)
    app.state.sessions.update(session.id, metadata_patch={SNAPSHOT: snapshot})
    for name in snapshot["loaded"]:
        usage.note(f"snapshot:{session.id}", document_subject(name), "loaded")
    usage.finish(f"snapshot:{session.id}", completed=False)
    return snapshot


def memory_snapshot_text(app: Any, sid: str, session: Any, turn_id: str) -> str:
    """This turn's ``## Memory`` addition, or ``""`` when the session carries none.

    Runs in the turn prologue, off the loop. When memory cannot be read (History mode,
    clio-core unavailable) the turn still runs, and the typed reason is recorded as a
    ``memory.snapshot.unavailable`` semantic event on the turn.
    """
    from clio_agent.errors import ClioError  # noqa: PLC0415
    from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415

    try:
        current = app.state.sessions.get(sid) or session
        snapshot = session_snapshot(app, current)
    except ClioError as error:
        reason = str((getattr(error, "details", {}) or {}).get("reason") or error.error_type)
        _emit_semantic_event(
            app,
            sid,
            "memory.snapshot.unavailable",
            turn_id=turn_id,
            status="failed",
            summary="Long-term memory could not be loaded for this turn.",
            actor={"role": "runtime", "component": "memory"},
            payload={"reason": reason, "error": str(error)},
        )
        return ""
    return snapshot["text"] if snapshot else ""


__all__ = [
    "MARKER",
    "SNAPSHOT",
    "SOURCE",
    "build_snapshot",
    "memory_snapshot_text",
    "session_snapshot",
    "wants_snapshot",
]
