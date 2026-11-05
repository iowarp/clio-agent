"""Store the agent's answer on every comment thread its turn was asked about (ledger FB26).

The agent needs no tool to reply: when the turn a comment session started finishes, its
final message becomes the reply on every thread of the session, and the version the turn
made of that artifact is linked from each thread (comments plan, "Replies"). A session
delivered as a steer into a running turn is answered by that turn. A turn that failed
records the failure on the thread instead of a reply. A turn that made no new version
links none.

The new version is read from the assistant message's ``resource_link`` parts. An agent
that reran a script or edited a file through the shell leaves no such link and the artifact
head unchanged, so each commented file's path is reconciled first: changed bytes on disk
become the next version (ledger FB28).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from clio_agent.gact.artifacts.records import Mechanism
from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.documents.models import ArtifactReview
from clio_agent.gact.documents.store import get_document_store

if TYPE_CHECKING:
    from fastapi import FastAPI

__all__ = ["on_turn_failed", "on_turn_finalized", "reconcile_thread_file", "replies_step"]

logger = logging.getLogger(__name__)
_ARTIFACT_URI = re.compile(r"^artifact://(?P<ws>[^/]+)/(?P<name>.+)@v(?P<version>\d+)$")
_REPLY_LIMIT = 16_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _belongs(app: "FastAPI", sid: str, message_id: str, turn_id: str) -> bool:
    if not message_id:
        return False
    if message_id == turn_id:
        return True
    history = getattr(app.state, "task_history", None)
    if history is None:
        return False
    committed = history.committed_notification_input(sid, message_id)
    return committed is not None and committed.turn_id == turn_id


def _reply_text(message: Any) -> str:
    texts = [str(part.text) for part in message.parts if part.type == "text" and part.text]
    return "\n\n".join(texts).strip()[:_REPLY_LIMIT]


def _linked_version(app: "FastAPI", message: Any, thread: ArtifactReview) -> str:
    registry = get_registry(app)
    for part in message.parts:
        if part.type != "resource_link":
            continue
        artifact_id = str((part.metadata or {}).get("artifact_id") or "")
        if artifact_id:
            found = registry.get_by_artifact_id(artifact_id)
            if found is not None and found[0].name == thread.artifact_name:
                return artifact_id
        match = _ARTIFACT_URI.match(part.uri or "")
        if match and match.group("name") == thread.artifact_name:
            record = registry.get(match.group("ws"), match.group("name"))
            number = int(match.group("version"))
            for version in record.versions if record is not None else []:
                if version.version == number:
                    return version.artifact_id
    return ""


def reconcile_thread_file(
    app: "FastAPI", sid: str, thread: ArtifactReview, turn_id: str = ""
) -> str:
    """Record the commented file's on-disk bytes as a new version when they changed.

    Returns the head version's artifact id after reconciling (``""`` when the artifact or
    its file is unknown).
    """

    from clio_agent.gact.artifacts.versions import reconcile_designated_path  # noqa: PLC0415

    registry = get_registry(app)
    record = registry.get(thread.workspace_id, thread.artifact_name)
    if record is None or record.head is None:
        return ""
    path = record.head.path
    if not path:
        return record.head.artifact_id
    outcome = reconcile_designated_path(
        app,
        sid,
        name=thread.artifact_name,
        workspace_id=thread.workspace_id,
        path=path,
        mechanism=Mechanism.CHANGE_FEED,
        turn_id=turn_id,
        session_id=sid,
    )
    if outcome is not None:
        return outcome.version.artifact_id
    refreshed = registry.get(thread.workspace_id, thread.artifact_name)
    return (
        refreshed.head.artifact_id if refreshed is not None and refreshed.head is not None else ""
    )


def on_turn_finalized(
    app: "FastAPI", sid: str, turn_id: str, assistant_msg: Any, *, failed: bool = False
) -> int:
    """Answer the threads this turn was asked about; returns how many it answered."""

    session = app.state.sessions.get(sid)
    if session is None:
        return 0
    store = get_document_store(app)
    waiting = [
        thread
        for thread in store.session_threads(session.workspace_id, sid)
        if thread.status == "dispatched" and not thread.reply_text and not thread.reply_error
    ]
    answered = [t for t in waiting if _belongs(app, sid, t.message_id, turn_id)]
    if not answered:
        return 0
    if failed:
        error = assistant_msg.error_info.message if assistant_msg.error_info is not None else ""
        for thread in answered:
            store.update_review(
                thread.id,
                reply_error=error or "the agent's turn failed",
                reply_message_id=assistant_msg.id,
                updated_at=_now(),
            )
        return len(answered)
    reply = _reply_text(assistant_msg)
    heads: dict[str, str] = {}
    for thread in answered:
        linked = _linked_version(app, assistant_msg, thread)
        if not linked:
            if thread.artifact_name not in heads:
                heads[thread.artifact_name] = reconcile_thread_file(app, sid, thread, turn_id)
            head = heads[thread.artifact_name]
            found = get_registry(app).get_by_artifact_id(head) if head else None
            newer = found is not None and found[1].created_at > thread.created_at
            linked = head if newer and head != thread.artifact_id else ""
        store.update_review(
            thread.id,
            reply_text=reply or "(the agent's final message had no text)",
            reply_message_id=assistant_msg.id,
            reply_version_artifact_id=linked,
            updated_at=_now(),
        )
    logger.info("comment replies stored session=%s turn=%s threads=%d", sid, turn_id, len(answered))
    return len(answered)


def on_turn_failed(app: "FastAPI", sid: str, turn_id: str, assistant_msg: Any) -> None:
    """Record a failed turn on the threads it was asked about, never raising into the
    failed-finalize settle; a failure to record is logged with its reason."""

    try:
        on_turn_finalized(app, sid, turn_id, assistant_msg, failed=True)
    except Exception:  # noqa: BLE001 - the settle must finish; the reason is logged
        logger.exception(
            "comment reply not recorded reason=comment_reply_failed session=%s turn=%s",
            sid,
            turn_id,
        )


def replies_step(state: Any, assistant_msg: Any) -> Callable[[], int]:
    """The finalize publication step that answers this turn's comment threads."""

    return lambda: on_turn_finalized(state.app, state.sid, state.turn_id, assistant_msg)
