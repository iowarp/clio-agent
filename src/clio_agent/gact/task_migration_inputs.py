"""Data-only historical mailbox and input conversion for startup migration."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from typing import Any

from clio_agent.arc.task_types import (
    AssignmentDetails,
    ContextBoundary,
    CoreReference,
    MailItem,
    MailKind,
    RecipientInput,
)
from clio_agent.gact.task_history import _encoded
from clio_agent.gact.task_publication import task_owner_recipient

logger = logging.getLogger(__name__)


def migrate_context_epochs(app: Any) -> None:
    """Persist a legacy fixed context's existing epoch without replay or history copying."""
    from clio_agent.gact.context import scope_epoch

    history = app.state.task_history
    for handle in history.task_handles(kind="Session") | history.task_handles(kind="Subagent"):
        task = history.task_snapshot(handle)
        if task is None or task.details is None:
            continue
        try:
            raw = history.read_content(task.details)
            if not isinstance(raw, dict) or raw.get("context_epoch"):
                continue
            details = AssignmentDetails.model_validate(raw)
            epoch = scope_epoch(details.context.scope) if details.context else ""
            if not epoch:
                continue
            content = history.write_content(
                task.owner.session_id,
                details.model_copy(update={"context_epoch": epoch}).model_dump(mode="json"),
            )
            history.record_snapshot(
                task.model_copy(update={"details": content}),
                observation_id="migration:context-epoch:" + handle + ":" + task.details.sha256,
            )
        except ValueError:
            _unresolved(
                app, handle, "context_epoch_unavailable", task.details.model_dump(mode="json")
            )


def _unresolved(app: Any, identity: str, reason: str, record: Any) -> CoreReference:
    """Retain unmappable data in Core without inventing an owner or execution."""
    reference = app.state.task_history.write_content(
        "_task_migration", {"source_identity": identity, "reason": reason, "record": record}
    )
    issues = app.state.task_migration_issues
    if reference not in issues:
        issues.append(reference)
    logger.warning(
        "Historical task data needs inspection reason=%s reference=%s", reason, reference.content_id
    )
    return reference


def _legacy_mail_kind(row: dict[str, Any]) -> MailKind:
    """Preserve trusted historical attribution without elevating peer content."""
    if row.get("human_intervention"):
        return "control"
    source = row.get("source_kind")
    if source == "human":
        return "human"
    if source != "system":
        return "peer"
    metadata = row.get("metadata") or {}
    return (
        "goal"
        if metadata.get("goal_id") or metadata.get("goal_redrive")
        else "loop"
        if metadata.get("loop_id")
        else "schedule"
        if metadata.get("scheduled")
        else "approval"
        if metadata.get("approval")
        else "hook"
    )


def _migrate_intents(app: Any, sessions: dict[str, Any], anchor: Callable[[str], str]) -> None:
    """Read the retired JSON format as data; retain future intent and uncertain delivery."""
    from clio_agent.gact.types import Message, Part, PostMessageRequest

    path = getattr(app.state, "legacy_message_intents_path", None)
    if path is None or not path.exists():
        return
    raw = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except ValueError:
        _unresolved(app, "message-intents", "invalid_historical_json", raw)
        return
    if not isinstance(payload, dict):
        _unresolved(app, "message-intents", "invalid_historical_intents", payload)
        return
    history = app.state.task_history
    for family in ("pending", "queued"):
        rows = payload.get(family, [])
        if not isinstance(rows, list):
            _unresolved(app, "message-intents:" + family, "invalid_historical_family", rows)
            continue
        for position, row in enumerate(rows):
            try:
                if not isinstance(row, dict):
                    _unresolved(
                        app, f"intent:{family}:{position}", "invalid_historical_intent", row
                    )
                    continue
                sid = str(row.get("session_id") or "")
                identity = str(
                    (row.get("message_id") if family == "pending" else row.get("id")) or ""
                )
                if not identity or sid not in sessions:
                    _unresolved(
                        app,
                        f"intent:{family}:{sid}:{identity}",
                        "intent_recipient_unavailable",
                        row,
                    )
                    continue
                if history.mail_item(sid, identity) is not None:
                    if (
                        family == "pending"
                        and row.get("state") == "cancelled"
                        and not history.mail_withdrawn(sid, identity)
                        and identity not in history.acknowledged_notifications(sid)
                    ):
                        history.withdraw_mail(sid, identity)
                    elif (
                        family == "pending"
                        and row.get("state") in {"claimed", "consumed"}
                        and identity not in history.acknowledged_notifications(sid)
                    ):
                        existing = history.require_mail(sid, identity)
                        body = history.read_content(existing.content)
                        if (
                            isinstance(body, dict)
                            and body.get("legacy") is not None
                            and body.get("legacy_intent_state") != row["state"]
                        ):
                            content = history.write_content(
                                sid, {**body, "legacy_intent_state": row["state"]}
                            )
                            history.update_mail(
                                existing.model_copy(
                                    update={
                                        "content": content,
                                        "delivery": "held",
                                        "held_reason": "historical_delivery_unknown",
                                        "revision": existing.revision + 1,
                                    }
                                ),
                                expected_revision=existing.revision,
                            )
                    continue  # a confirmed replacement record/revision always wins
                session = sessions[sid]
                stamp = str(row.get("accepted_at") or row.get("created_at") or session.created_at)
                human = str(session.metadata.get("owner_human_id") or app.state.human_principal)
                if family == "queued":
                    request = PostMessageRequest.model_validate(
                        {
                            key: row[key]
                            for key in (
                                "text",
                                "parts",
                                "metadata",
                                "client_message_id",
                                "idempotency_key",
                                "behavior",
                                "model",
                            )
                            if key in row
                        }
                    )
                    content = history.write_content(
                        sid,
                        {
                            "request": request.model_dump(mode="json"),
                            "updated_at": row.get("updated_at") or stamp,
                            "legacy": row,
                        },
                    )
                    item = MailItem(
                        id=identity,
                        recipient_session=sid,
                        sender_human=human,
                        kind="composer",
                        content=content,
                        created_at=stamp,
                        delivery="queued",
                        revision=int(row.get("revision") or 1),
                        queued_order=position,
                    )
                else:
                    state = str(row.get("state") or "pending")
                    if state not in {"pending", "claimed", "consumed", "cancelled"}:
                        _unresolved(app, f"intent:{sid}:{identity}", "unknown_feedback_state", row)
                        continue
                    message = Message(
                        id=identity,
                        session_id=sid,
                        turn_id="",
                        role="user",
                        created_at=stamp,
                        updated_at=stamp,
                        metadata=row.get("metadata") or {},
                        parts=[Part.model_validate(part) for part in row.get("parts", [])]
                        or [
                            Part(
                                id=identity + ":text", type="text", text=str(row.get("text") or "")
                            )
                        ],
                    )
                    content = history.write_content(
                        sid,
                        {
                            "text": row.get("text") or "",
                            "message_id": identity,
                            "message": message.model_dump(mode="json"),
                            "legacy": row,
                            "delivery": "unknown",
                            "legacy_intent_state": state,
                        },
                    )
                    item = MailItem(
                        id=identity,
                        recipient_session=sid,
                        sender_human=human,
                        kind="human",
                        content=content,
                        created_at=stamp,
                        handle=anchor(sid),
                        delivery="held" if state in {"claimed", "consumed"} else "eligible",
                        held_reason="historical_delivery_unknown"
                        if state in {"claimed", "consumed"}
                        else "",
                    )
                history.record_mail(item)
                if family == "pending" and row.get("state") == "cancelled":
                    history.withdraw_mail(sid, identity)
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                _unresolved(
                    app,
                    f"intent:{family}:{position}",
                    "invalid_historical_intent:" + type(error).__name__,
                    row,
                )


def _migrate_inputs(app: Any, anchor: Callable[[str], str]) -> None:
    """Import acknowledgement only from an actual retained recipient-input segment."""
    history = app.state.task_history
    from clio_agent.gact.session_records import PageQuery  # noqa: PLC0415

    for session in app.state.sessions.select(PageQuery(archived=None)):  # a migration
        for scope in history.arc.list_segment_scopes(session.id):
            if scope.startswith("_"):
                continue
            for segment in history.arc.list_segments(session.id, scope, include_tombstoned=True):
                if segment.kind != "recipient_input" or "schema_version" in segment.content:
                    continue
                proof = segment.content
                ids = tuple(proof.get("notification_ids", ()))
                if not ids:
                    continue
                assignment = history.latest_assignment(session.id)
                if assignment is None:
                    anchor(session.id)  # an actual legacy input justifies this historical anchor
                    assignment = history.latest_assignment(session.id)
                if assignment is None:
                    _unresolved(
                        app, "input:" + str(segment.id), "input_assignment_unavailable", proof
                    )
                    continue
                reference = CoreReference(
                    session_id=session.id,
                    scope=scope,
                    content_id=str(segment.id),
                    sha256=hashlib.sha256(_encoded(proof)).hexdigest(),
                )
                identity = "migration-input:" + str(segment.id)
                existing = history.recipient_input(session.id, identity)
                if existing is not None:
                    if existing.legacy_proof != reference:
                        _unresolved(app, identity, "legacy_input_proof_changed", proof)
                    continue
                mapped: list[str] = []
                for notice in ids:
                    legacy_handle = (
                        notice.removeprefix("task-result:")
                        if notice.startswith("task-result:")
                        else notice.removeprefix(f"task-subscription:{session.id}:")
                        if notice.startswith(f"task-subscription:{session.id}:")
                        else None
                    )
                    if legacy_handle is None:
                        mapped.append(notice.removeprefix("mail:"))
                        continue
                    handle = history.resolve_handle(legacy_handle)
                    task = history.task_snapshot(handle) if handle is not None else None
                    if task is None or task.terminal_at is None:
                        continue
                    owner = task_owner_recipient(history, task)
                    canonical = (
                        f"outcome:{handle}:{session.id}"
                        if owner == session.id
                        else f"outcome:{handle}:subscriber:{session.id}:1"
                    )
                    if history.mail_item(session.id, canonical) is None:
                        # This receipt acknowledges only the recipient proved by
                        # this old input. It must not publish notices to others.
                        content = task.result or history.write_content(
                            task.owner.session_id,
                            {
                                "handle": handle,
                                "status": task.status,
                                "historical_result_reference": "unavailable",
                            },
                        )
                        history.record_mail(
                            MailItem(
                                id=canonical,
                                recipient_session=session.id,
                                kind="question_answer"
                                if task.kind == "Question" and task.status == "completed"
                                else "task_result",
                                content=content,
                                handle=handle,
                                created_at=task.terminal_at,
                                delivery="held",
                                held_reason="historical_input_proof",
                            )
                        )
                    mapped.append(canonical)
                pending = {item.id: item for item in history.pending_mail(session.id)}
                selected = tuple(dict.fromkeys(notice for notice in mapped if notice in pending))
                record = RecipientInput(
                    id=identity,
                    session_id=session.id,
                    assignment_id=assignment.handle,
                    iteration_id="migration:" + str(segment.id),
                    boundary=ContextBoundary(
                        session_id=session.id, scope=scope, logical_time=segment.logical_time
                    ),
                    committed_at=str(proof.get("recorded_at") or session.updated_at),
                    notification_ids=selected,
                    content=tuple(pending[identity].content for identity in selected),
                    legacy_proof=reference,
                    legacy_notification_ids=ids,
                    turn_id=str(segment.turn_id or ""),
                )
                history.record_input(record)
