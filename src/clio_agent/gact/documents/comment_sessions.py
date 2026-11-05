"""Comment sessions on artifacts: labels, drafts, threads, and one agent turn per send.

A select session is one send (addendum A, section 1). Its marks are labelled ``S1``, ``S2``
and onward, continuing per artifact version across sessions so a thread's label never
changes. Unsent marks stay as a draft on that version. Sending turns every mark into a
thread (``ArtifactReview``); the marks with "Send to agent" on go to the agent as one turn
whose model text is rendered by ``review_render`` and whose image parts are its overview and
crops, sent only when the model accepts images (ledger FB24). The person's own transcript
message shows their comments and one ``artifact_review`` part per thread.
"""

from __future__ import annotations

import base64
import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent.gact.artifacts.records import ArtifactRecord, ArtifactVersion
from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.documents.anchors import MarkTarget
from clio_agent.gact.documents.artifact_bytes import ArtifactBytesError, artifact_bytes_path
from clio_agent.gact.documents.models import (
    ArtifactReview,
    CommentMark,
    CommentSessionRequest,
)
from clio_agent.gact.documents.profiles import document_format
from clio_agent.gact.documents.review_render import MarkInput, render_session
from clio_agent.gact.documents.store import get_document_store
from clio_agent.gact.parts import Part

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.routes.deps import GactDeps

__all__ = [
    "CommentSessionError",
    "SentSession",
    "dispatch_threads",
    "label_number",
    "mark_target",
    "next_label",
    "save_drafts",
    "send_session",
    "version_threads",
]

logger = logging.getLogger(__name__)
_LABEL = re.compile(r"^S([1-9][0-9]{0,3})$")
_RENDITION_PROFILES = {
    "ooxml-word",
    "ooxml-sheet",
    "ooxml-slides",
    "odf-text",
    "odf-sheet",
    "odf-slides",
}


class CommentSessionError(RuntimeError):
    """A send or draft the server refuses; ``code`` is typed and ``status`` its HTTP status."""

    def __init__(self, status: int, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details


@dataclass
class SentSession:
    """The threads one send made, and the message that carried the queued ones."""

    session_key: str
    threads: list[ArtifactReview] = field(default_factory=list)
    message_id: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def label_number(label: str) -> int:
    """``7`` for ``S7``; ``0`` for anything that is not a mark label."""

    match = _LABEL.match(label or "")
    return int(match.group(1)) if match else 0


def version_threads(
    app: "FastAPI", record: ArtifactRecord, artifact_id: str
) -> list[ArtifactReview]:
    """Every thread and draft made on one artifact version, oldest first."""

    rows = get_document_store(app).list_reviews(record.workspace_id, record.name)
    return [row for row in rows if row.artifact_id == artifact_id]


def next_label(rows: Sequence[ArtifactReview]) -> str:
    """The label the next new mark on this version takes (after every label ever used)."""

    used = [label_number(row.label) for row in rows]
    return f"S{max(used, default=0) + 1}"


def _allocate(
    rows: Sequence[ArtifactReview], marks: Sequence[CommentMark], session_id: str
) -> list[str]:
    """One label per mark: a mark keeps its label when it is this session's draft or unused."""

    taken = {label_number(row.label): row for row in rows if label_number(row.label)}
    counter = max(taken, default=0)
    labels: list[str] = []
    chosen: set[int] = set()
    for mark in marks:
        number = label_number(mark.label)
        owner = taken.get(number)
        mine = owner is not None and owner.session_id == session_id and owner.status == "draft"
        if number and number not in chosen and (owner is None or mine):
            chosen.add(number)
            counter = max(counter, number)
            labels.append(f"S{number}")
            continue
        counter += 1
        while counter in chosen:
            counter += 1
        chosen.add(counter)
        labels.append(f"S{counter}")
    return labels


def mark_target(
    app: "FastAPI", record: ArtifactRecord, version: ArtifactVersion, session_id: str
) -> MarkTarget:
    """The version to resolve marks against, with its PDF rendition when it is shown as one.

    Raises:
        CommentSessionError: the version's bytes or its rendition are unavailable.
    """

    try:
        path = artifact_bytes_path(app, record, version)
    except ArtifactBytesError as exc:
        raise CommentSessionError(409, exc.code, str(exc)) from exc
    profile = document_format(record.name).profile
    view = path
    if profile in _RENDITION_PROFILES:
        from clio_agent.gact.documents.renditions import (  # noqa: PLC0415
            RenditionError,
            find_pdf_rendition,
            render_pdf,
        )

        try:
            rendition = find_pdf_rendition(app, record, version) or render_pdf(
                app, session_id, record, version
            )
            view = artifact_bytes_path(app, rendition.record, rendition.version)
        except (RenditionError, ArtifactBytesError) as exc:
            # Native comments resolve against the file; marks on the rendition say why not.
            logger.info(
                "comment target has no rendition artifact=%s reason=%s", version.artifact_id, exc
            )
    return MarkTarget(
        app=app,
        record=record,
        version=version,
        path=path,
        profile=profile,
        view_path=view,
        session_id=session_id,
    )


def _artifact(app: "FastAPI", artifact_id: str) -> tuple[ArtifactRecord, ArtifactVersion]:
    found = get_registry(app).get_by_artifact_id(artifact_id)
    if found is None:
        raise CommentSessionError(404, "not_found", f"artifact not found: {artifact_id}")
    return found


def _check_identity(
    app: "FastAPI",
    record: ArtifactRecord,
    version: ArtifactVersion,
    *,
    expected_version: int,
    expected_sha256: str,
    allow_historical: bool,
) -> None:
    if version.version != expected_version or (version.sha256 or "") != expected_sha256:
        raise CommentSessionError(
            409,
            "stale_artifact_anchor",
            "the comments name a different version of this artifact",
            expected_version=expected_version,
            actual_version=version.version,
        )
    current = get_registry(app).get(record.workspace_id, record.name)
    if (
        not allow_historical
        and current is not None
        and current.head is not None
        and current.head.artifact_id != version.artifact_id
    ):
        raise CommentSessionError(
            409,
            "stale_artifact_anchor",
            "the artifact has a newer version; mark it again there or send these as history",
            selected_artifact_id=version.artifact_id,
            head_artifact_id=current.head.artifact_id,
            head_version=current.head.version,
        )


def _thread(
    session_id: str,
    record: ArtifactRecord,
    version: ArtifactVersion,
    mark: CommentMark,
    label: str,
    *,
    status: str,
    session_key: str = "",
    idempotency_key: str = "",
    row_id: str = "",
) -> ArtifactReview:
    now = _now()
    return ArtifactReview(
        id=row_id or f"docreview_{uuid.uuid4().hex}",
        session_id=session_id,
        workspace_id=record.workspace_id,
        artifact_id=version.artifact_id,
        artifact_name=record.name,
        artifact_version=version.version,
        artifact_sha256=version.sha256 or "",
        anchor=mark.anchor,
        text=mark.comment,
        status=status,  # type: ignore[arg-type]
        idempotency_key=idempotency_key,
        created_at=now,
        updated_at=now,
        label=label,
        session_key=session_key,
        send_to_agent=mark.send_to_agent,
    )


def save_drafts(
    app: "FastAPI",
    session_id: str,
    artifact_id: str,
    *,
    expected_version: int,
    expected_sha256: str,
    marks: Sequence[CommentMark],
) -> list[ArtifactReview]:
    """Keep this session's unsent marks as the draft on one artifact version.

    Marks keep their labels; a draft mark no longer in ``marks`` is discarded (its label is
    not reused). Returns the session's drafts on the version after the save.
    """

    record, version = _artifact(app, artifact_id)
    _check_identity(
        app,
        record,
        version,
        expected_version=expected_version,
        expected_sha256=expected_sha256,
        allow_historical=True,
    )
    store = get_document_store(app)
    rows = version_threads(app, record, version.artifact_id)
    mine = {
        row.label: row for row in rows if row.status == "draft" and row.session_id == session_id
    }
    labels = _allocate(rows, marks, session_id)
    kept: list[ArtifactReview] = []
    for mark, label in zip(marks, labels, strict=True):
        existing = mine.pop(label, None)
        if existing is None:
            kept.append(
                store.create_review(
                    _thread(session_id, record, version, mark, label, status="draft")
                )
            )
            continue
        kept.append(
            store.update_review(
                existing.id,
                anchor=mark.anchor,
                text=mark.comment,
                send_to_agent=mark.send_to_agent,
                updated_at=_now(),
            )
        )
    for dropped in mine.values():
        store.update_review(dropped.id, status="discarded", updated_at=_now())
    return kept


def send_session(
    app: "FastAPI",
    deps: "GactDeps",
    session_id: str,
    request: CommentSessionRequest,
) -> SentSession:
    """Turn one session's marks into threads and send the agent-bound ones as one turn.

    A replay with the same ``idempotency_key`` returns the threads it made the first time
    without sending again.

    Raises:
        CommentSessionError: an unknown session or artifact, an artifact outside the
            session's workspace, or a stale version (``stale_artifact_anchor``).
    """

    session = app.state.sessions.get(session_id)
    if session is None:
        raise CommentSessionError(404, "not_found", f"session not found: {session_id}")
    record, version = _artifact(app, request.artifact_id)
    if record.workspace_id != session.workspace_id:
        raise CommentSessionError(
            403, "artifact_workspace_mismatch", "artifact is outside this session"
        )
    _check_identity(
        app,
        record,
        version,
        expected_version=request.expected_version,
        expected_sha256=request.expected_sha256,
        allow_historical=request.allow_historical,
    )
    store = get_document_store(app)
    rows = version_threads(app, record, version.artifact_id)
    replay = [row for row in rows if row.idempotency_key.startswith(request.idempotency_key + ":")]
    if replay:
        key = replay[0].session_key
        return SentSession(key, replay, replay[0].message_id)
    labels = _allocate(rows, request.marks, session_id)
    drafts = {
        row.label: row for row in rows if row.status == "draft" and row.session_id == session_id
    }
    session_key = f"cs_{uuid.uuid4().hex[:16]}"
    threads: list[ArtifactReview] = []
    for mark, label in zip(request.marks, labels, strict=True):
        status = "queued" if mark.send_to_agent else "human-note"
        key = f"{request.idempotency_key}:{label}"
        draft = drafts.pop(label, None)
        if draft is not None:
            thread = store.update_review(
                draft.id,
                anchor=mark.anchor,
                text=mark.comment,
                send_to_agent=mark.send_to_agent,
                status=status,
                session_key=session_key,
                idempotency_key=key,
                updated_at=_now(),
            )
        else:
            thread = store.create_review(
                _thread(
                    session_id,
                    record,
                    version,
                    mark,
                    label,
                    status=status,
                    session_key=session_key,
                    idempotency_key=key,
                )
            )
        emit_thread(app, thread, "document.review.created")
        threads.append(thread)
    sent = SentSession(session_key, threads)
    queued = [thread for thread in threads if thread.status == "queued"]
    if queued:
        dispatched = dispatch_threads(app, deps, session_id, record, version, queued, session_key)
        by_id = {thread.id: thread for thread in dispatched}
        sent.threads = [by_id.get(thread.id, thread) for thread in threads]
        sent.message_id = next((t.message_id for t in dispatched if t.message_id), "")
    return sent


def _scratch_folder(app: "FastAPI", session_id: str, workspace_id: str, session_key: str) -> Path:
    from clio_agent.runtime.session_scratch import session_scratch_dir  # noqa: PLC0415

    folder = session_scratch_dir(session_id)
    if folder is None:
        folder = get_document_store(app).documents_folder(workspace_id)
    return Path(folder) / "comments" / session_key


def review_part(thread: ArtifactReview) -> Part:
    """The ``artifact_review`` part the UI and the thread list read for one thread."""

    return Part(
        id=f"part_{uuid.uuid4().hex}",
        type="artifact_review",
        review_id=thread.id,
        artifact_id=thread.artifact_id,
        artifact_version=thread.artifact_version,
        artifact_sha256=thread.artifact_sha256,
        review_text=thread.text,
        anchor=thread.anchor.model_dump(mode="json"),
        metadata={
            "artifact_name": thread.artifact_name,
            "workspace_id": thread.workspace_id,
            "native": thread.native,
            "label": thread.label,
            "session_key": thread.session_key,
        },
    )


def _transcript_text(
    record: ArtifactRecord, version: ArtifactVersion, threads: Sequence[ArtifactReview]
) -> str:
    lines = [f"Comments on {record.name} (version {version.version}):"]
    for thread in threads:
        lines.append(f"{thread.label}: {thread.text.strip() or '(no comment)'}")
    return "\n".join(lines)


def dispatch_threads(
    app: "FastAPI",
    deps: "GactDeps",
    session_id: str,
    record: ArtifactRecord,
    version: ArtifactVersion,
    threads: Sequence[ArtifactReview],
    session_key: str,
) -> list[ArtifactReview]:
    """Send queued threads to the agent as one turn; each thread records the outcome."""

    from clio_agent.gact.message_submission import (  # noqa: PLC0415
        accept_message,
        request_takes_images,
    )
    from clio_agent.gact.types import PostMessageRequest  # noqa: PLC0415

    store = get_document_store(app)
    text = _transcript_text(record, version, threads)
    try:
        target = mark_target(app, record, version, session_id)
        probe = PostMessageRequest(parts=[Part(type="text", text=text)])
        images = request_takes_images(app, app.state.sessions.get(session_id), probe)
        rendered = render_session(
            target,
            [MarkInput(t.label, t.anchor, t.text) for t in threads],
            images=images,
            folder=_scratch_folder(app, session_id, record.workspace_id, session_key),
        )
        parts = [Part(id=f"comment-text-{session_key}", type="text", text=text)]
        parts.extend(review_part(thread) for thread in threads)
        parts.extend(
            Part(
                id=f"comment-image-{session_key}-{index}",
                type="image",
                data=base64.b64encode(image.png).decode("ascii"),
                media_type="image/png",
                metadata={"comment_session": session_key, "caption": image.caption},
            )
            for index, image in enumerate(rendered.images, start=1)
        )
        response, _status = accept_message(
            app,
            deps,
            session_id,
            PostMessageRequest(parts=parts, client_message_id=f"comments_{session_key}"),
            internal_model_text=rendered.text,
            server_metadata={"comment_session_id": session_key},
        )
    except Exception as exc:  # noqa: BLE001 - forwards: onto every thread, as why its send failed
        failed = [
            store.update_review(t.id, status="failed", error=str(exc)[:2000], updated_at=_now())
            for t in threads
        ]
        for thread in failed:
            emit_thread(app, thread, "document.review.dispatched")
        return failed
    done = [
        store.update_review(
            t.id,
            status="dispatched",
            message_id=response.message_id,
            error="",
            updated_at=_now(),
        )
        for t in threads
    ]
    for thread in done:
        emit_thread(app, thread, "document.review.dispatched")
    return done


def dispatch_native(app: "FastAPI", deps: "GactDeps", reviews: Sequence[ArtifactReview]) -> None:
    """Send newly ingested ``@clio`` comments, one session per artifact version."""

    store = get_document_store(app)
    groups: dict[tuple[str, str], list[ArtifactReview]] = {}
    for review in reviews:
        if review.status == "queued":
            groups.setdefault((review.session_id, review.artifact_id), []).append(review)
    for (session_id, artifact_id), queued in groups.items():
        found = get_registry(app).get_by_artifact_id(artifact_id)
        if found is None:
            continue
        record, version = found
        rows = version_threads(app, record, artifact_id)
        session_key = f"cs_{uuid.uuid4().hex[:16]}"
        number = label_number(next_label([r for r in rows if r.id not in {q.id for q in queued}]))
        labelled = []
        for offset, review in enumerate(queued):
            labelled.append(
                store.update_review(
                    review.id,
                    label=f"S{number + offset}",
                    session_key=session_key,
                    updated_at=_now(),
                )
            )
        dispatch_threads(app, deps, session_id, record, version, labelled, session_key)


def emit_thread(app: "FastAPI", thread: ArtifactReview, event_type: str) -> None:
    """Record a thread's change as a semantic event, off the event loop."""

    from clio_agent.gact.off_loop import schedule_off_loop  # noqa: PLC0415

    schedule_off_loop(
        lambda: _emit_now(app, thread, event_type), label=f"document.review:{event_type}"
    )


def _emit_now(app: "FastAPI", thread: ArtifactReview, event_type: str) -> None:
    from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415

    _emit_semantic_event(
        app,
        thread.session_id,
        event_type,
        status="failed" if thread.status == "failed" else "completed",
        summary=f"Comment {thread.label or thread.id} on {thread.artifact_name} {thread.status}.",
        actor={"role": "user"},
        subject={
            "review_id": thread.id,
            "artifact_id": thread.artifact_id,
            "artifact_name": thread.artifact_name,
        },
        payload={
            "review_id": thread.id,
            "artifact_id": thread.artifact_id,
            "artifact_version": thread.artifact_version,
            "artifact_sha256": thread.artifact_sha256,
            "status": thread.status,
            "native": thread.native,
            "message_id": thread.message_id,
            "label": thread.label,
            "session_key": thread.session_key,
        },
        detail_level="semantic",
    )
