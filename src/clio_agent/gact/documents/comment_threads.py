"""The thread list of an artifact: re-anchored onto its newest version when it is listed.

When the agent makes a new version, each open thread is re-anchored by its resolver
(comments plan, "New versions"): a text anchor moves by its text and context, an element by
its selector and text. A region does not move on its own; it stays on the version it was made
on and is shown beside the same rectangle on the new one. Re-anchoring runs when the threads
are listed and the head is newer than a thread's last re-anchor, and its outcome is stored, so
it runs once per thread and version (ledger FB27). Nothing is carried to a new version without
being marked moved or outdated.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

from clio_agent.gact.artifacts.records import ArtifactRecord, ArtifactVersion
from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.documents.anchors import resolver_for
from clio_agent.gact.documents.comment_sessions import (
    CommentSessionError,
    mark_target,
    next_label,
    version_threads,
)
from clio_agent.gact.documents.models import ArtifactReview
from clio_agent.gact.documents.store import get_document_store

if TYPE_CHECKING:
    from fastapi import FastAPI

__all__ = ["ThreadList", "list_threads", "thread_crop"]

_OPEN = {"queued", "dispatched", "human-note", "failed"}
_REANCHOR_LOCK = threading.Lock()


@dataclass
class ThreadList:
    """An artifact's threads, its newest version, and the label a new mark on the viewed
    version takes."""

    record: ArtifactRecord
    head: ArtifactVersion
    next_label: str
    threads: list[ArtifactReview] = field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _reanchor(
    app: "FastAPI", record: ArtifactRecord, head: ArtifactVersion, session_id: str
) -> None:
    store = get_document_store(app)
    registry = get_registry(app)
    with _REANCHOR_LOCK:
        rows = store.list_reviews(record.workspace_id, record.name)
        stale = [
            row
            for row in rows
            if row.status in _OPEN
            and row.artifact_id != head.artifact_id
            and row.current_artifact_id != head.artifact_id
        ]
        if not stale:
            return
        new_target = mark_target(app, record, head, session_id)
        for row in stale:
            found = registry.get_by_artifact_id(row.artifact_id)
            resolver = resolver_for(new_target.profile, row.anchor.profile)
            if found is None or resolver is None:
                store.update_review(
                    row.id,
                    anchor_state="outdated",
                    current_anchor=None,
                    current_artifact_id=head.artifact_id,
                    updated_at=_now(),
                )
                continue
            old_target = mark_target(app, found[0], found[1], session_id)
            state, anchor = resolver.reanchor(old_target, new_target, row.anchor)
            store.update_review(
                row.id,
                anchor_state=state,
                current_anchor=anchor if state == "moved" else None,
                current_artifact_id=head.artifact_id,
                updated_at=_now(),
            )


def list_threads(app: "FastAPI", artifact_id: str, *, session_id: str = "") -> ThreadList:
    """Every thread on the artifact ``artifact_id`` is a version of, re-anchored to its head.

    With ``session_id``, the commented file is reconciled first, so bytes the agent changed
    through the shell become the head the threads are carried to (ledger FB28).

    Raises:
        CommentSessionError: the artifact is unknown.
    """

    registry = get_registry(app)
    found = registry.get_by_artifact_id(artifact_id)
    if found is None:
        raise CommentSessionError(404, "not_found", f"artifact not found: {artifact_id}")
    record, viewed = found
    if session_id and record.head is not None and record.head.path:
        from clio_agent.gact.artifacts.records import Mechanism  # noqa: PLC0415
        from clio_agent.gact.artifacts.versions import reconcile_designated_path  # noqa: PLC0415

        reconcile_designated_path(
            app,
            session_id,
            name=record.name,
            workspace_id=record.workspace_id,
            path=record.head.path,
            mechanism=Mechanism.CHANGE_FEED,
            session_id=session_id,
        )
        record = registry.get(record.workspace_id, record.name) or record
    head = record.head or viewed
    if head.artifact_id != viewed.artifact_id or any(
        row.artifact_id != head.artifact_id
        for row in get_document_store(app).list_reviews(record.workspace_id, record.name)
    ):
        _reanchor(app, record, head, session_id)
    rows = get_document_store(app).list_reviews(record.workspace_id, record.name)
    shown = [row for row in rows if row.status != "discarded"]
    return ThreadList(
        record=record,
        head=head,
        next_label=next_label(version_threads(app, record, viewed.artifact_id)),
        threads=shown,
    )


def thread_crop(
    app: "FastAPI", thread_id: str, side: Literal["before", "after"], *, session_id: str = ""
) -> bytes | None:
    """A region or element thread's crop on its own version (``before``) or on the newest
    version (``after``), for the before and after view."""

    store = get_document_store(app)
    thread = store.get_review(thread_id)
    if thread is None:
        raise CommentSessionError(404, "not_found", f"thread not found: {thread_id}")
    registry = get_registry(app)
    if side == "before":
        found = registry.get_by_artifact_id(thread.artifact_id)
    else:
        logical = registry.get(thread.workspace_id, thread.artifact_name)
        found = (logical, logical.head) if logical is not None and logical.head else None
    if found is None:
        return None
    record, version = found
    target = mark_target(app, record, version, session_id or thread.session_id)
    resolver = resolver_for(target.profile, thread.anchor.profile)
    if resolver is None:
        return None
    return resolver.resolve(target, thread.anchor).crop
