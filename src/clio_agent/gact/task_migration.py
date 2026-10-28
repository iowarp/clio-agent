"""Data-only startup conversion of retained task formats; no old owners or replay."""

from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from collections.abc import Iterator
from typing import Any, cast

from clio_agent.arc import read_ahead
from clio_agent.arc.live import EVENTS_SCOPE
from clio_agent.arc.task_types import (
    AssignmentDetails,
    BackendReference,
    ContextBoundary,
    CoordinationGrant,
    CoreReference,
    MailItem,
    QuestionDefinition,
    QuestionReply,
    TaskAlias,
    TaskKind,
    TaskOwner,
    TaskProgress,
    TaskSnapshot,
    TaskSubscription,
    question_surface_resource,
)
from clio_agent.gact.context import scope_epoch
from clio_agent.gact.task_migration_inputs import (
    _legacy_mail_kind,
    _migrate_inputs,
    _migrate_intents,
    _unresolved,
    migrate_context_epochs,
)
from clio_agent.gact.task_migration_storage import (
    RECEIPT_EVENT,
    finish_legacy_operations,
    legacy_operation_task,
    read_legacy_operations,
)
from clio_agent.gact.task_publication import publish_task_notice, task_owner_recipient

_FORMATS = {
    "task.assignment": ("assignment", "task"),
    "task.native.observation": ("native", None),
    "task.mcp.observation": ("mcp", "record"),
    "task.question.observation": ("question", "question"),
}
_TERMINAL = {"completed", "failed", "cancelled", "interrupted", "answered", "expired"}
logger = logging.getLogger(__name__)


def _identity(family: str, sid: str, row: dict[str, Any]) -> str:
    identity = (
        row.get("key")
        if family == "mcp"
        else (row.get("task_id") if family == "assignment" else row.get("handle") or row.get("id"))
    )
    return json.dumps([family, sid, identity], sort_keys=True, separators=(",", ":"))


def _mapped(identity: str) -> str:
    return "task_migrated_" + hashlib.sha256(identity.encode()).hexdigest()


def _session_events(history: Any) -> Iterator[tuple[str, dict[str, Any]]]:
    """Read ownership from Core's segment envelope, rather than event content.

    The logs are read ahead a batch of sessions at a time (:mod:`clio_agent.arc.read_ahead`).
    """
    store = history.arc._segments
    with read_ahead.reading_ahead(store._store) as buffer:
        names = (
            read_ahead.listed(store, r".*__events(~[1-9][0-9]*)?") if buffer is not None else set()
        )
        for session_id in read_ahead.ahead_by_batch(
            store._store,
            "segments",
            history.arc._live._event_session_ids(),
            lambda sid: read_ahead.family_names(store, sid, EVENTS_SCOPE, names),
        ):
            for segment in history.arc._live.iter_session_event_segments(session_id):
                yield session_id, segment.content


def migrate_history(app: Any) -> None:
    """Persist stable mappings, content and proven acknowledgements without execution."""
    history = app.state.task_history
    app.state.task_migration_issues = []
    sessions = {session.id: session for session in app.state.sessions.list()}
    for session in sessions.values():
        if (
            session.status == "cancelled"
            and "composer_queue_paused" not in session.metadata
            and history.latest_assignment(session.id) is None
        ):
            app.state.sessions.update(session.id, metadata_patch={"composer_queue_paused": True})
    latest: dict[str, tuple[str, str, dict[str, Any], dict[str, Any]]] = {}
    mail: dict[tuple[str, str], dict[str, Any]] = {}
    withdrawals: set[tuple[str, str]] = set()
    subscriptions: dict[tuple[str, str], dict[str, Any]] = {}
    grants: dict[tuple[str, str], dict[str, Any]] = {}
    storage_rows = read_legacy_operations(app)
    storage_receipts: set[str] = set()
    for sid, event in _session_events(history):
        payload = event.get("payload") or {}
        if event.get("event_type") == RECEIPT_EVENT and payload.get("observation_id"):
            storage_receipts.add(payload["observation_id"])
            receipt = payload.get("record", {})
            if not receipt.get("association_confirmed") and receipt.get("content"):
                app.state.task_migration_issues.append(
                    CoreReference.model_validate(receipt["content"])
                )
        if "schema_version" in payload:
            continue
        event_type = event.get("event_type")
        if event_type in _FORMATS:
            family, key = _FORMATS[event_type]
            row = payload.get(key, {}) if key else payload
            if isinstance(row, dict) and sid:
                identity = _identity(family, sid, row)
                latest[identity] = (family, sid, row, payload)
        elif event_type in {"task.mail.accepted", "task.mail.updated"}:
            recipient = str(payload.get("target_session_id") or sid)
            identity = str(payload.get("message_id") or payload.get("identity") or "")
            if identity:
                mail_key = (recipient, identity)
                mail[mail_key] = {**mail.get(mail_key, {}), **payload}
        elif event_type == "task.mail.withdrawal":
            withdrawals.add(
                (
                    str(payload.get("target_session_id") or sid),
                    str(payload.get("message_id") or payload.get("identity") or ""),
                )
            )
        elif event_type == "task.intervention":
            recipient = str(payload.get("recipient_session_id") or sid)
            identity = str(payload.get("identity") or "")
            if identity and payload.get("actor") == "human":
                mail[(recipient, identity)] = {
                    "message_id": identity,
                    "target_session_id": recipient,
                    "source_session_id": payload.get("actor_session_id"),
                    "source_kind": "system",
                    "human_intervention": payload,
                    "text": "Human intervention on owned work:\n"
                    + json.dumps(payload, ensure_ascii=False),
                    "accepted_at": payload.get("recorded_at"),
                }
        elif event_type == "task.subscription":
            subscriptions[
                (str(payload.get("recipient_session_id") or sid), str(payload.get("handle") or ""))
            ] = payload
        elif event_type == "task.coordination_grant":
            grants[(sid, str(payload.get("identity") or ""))] = payload
    # Older metadata is imported only when no complete Core record superseded it.
    for sid, session in sessions.items():
        metadata = session.metadata
        for family, raw in (("assignment", metadata.get("agent_task")),):
            if isinstance(raw, dict) and raw.get("task_id"):
                identity = _identity(family, sid, raw)
                latest.setdefault(identity, (family, sid, raw, raw))
        for family, key in (("mcp", "mcp_tasks"), ("question", "user_questions")):
            rows = metadata.get(key, {})
            values = (
                rows.values() if isinstance(rows, dict) else rows if isinstance(rows, list) else ()
            )
            for raw in values:
                if isinstance(raw, dict):
                    identity = _identity(family, sid, raw)
                    latest.setdefault(identity, (family, sid, raw, raw))
        for raw in metadata.get("agent_mailbox", ()):
            if isinstance(raw, dict):
                identity = str(raw.get("message_id") or raw.get("identity") or "")
                mail.setdefault((str(raw.get("target_session_id") or sid), identity), raw)
    # Existing Core and ordinary legacy task records take precedence over storage.
    known = {
        str(row[name])
        for _, _, row, _ in latest.values()
        for name in ("task_id", "handle_id", "handle", "task_handle", "id")
        if row.get(name)
    }
    for operation in storage_rows:
        row = legacy_operation_task(operation, sessions)
        if (
            row is not None
            and row["handle"] not in known
            and not history.aliases(row["handle"])
            and history.task_snapshot(row["handle"]) is None
        ):
            sid = operation.original["owner_session_id"]
            latest[_identity("native", sid, row)] = ("native", sid, row, row)
            known.add(row["handle"])
    preferred: dict[str, list[str]] = defaultdict(list)
    for identity, (_family, _sid, row, _payload) in latest.items():
        candidate = str(row.get("handle") or row.get("handle_id") or row.get("task_handle") or "")
        if candidate:
            preferred[candidate].append(identity)
    handles = {
        identity: (candidate if candidate and len(preferred[candidate]) == 1 else _mapped(identity))
        for identity, (family, sid, row, payload) in latest.items()
        for candidate in [
            str(row.get("handle") or row.get("handle_id") or row.get("task_handle") or "")
        ]
    }
    assignments = {
        str(row.get("child_session_id") or sid): handles[identity]
        for identity, (family, sid, row, payload) in latest.items()
        if family == "assignment"
    }

    def anchor(sid: str) -> str:
        existing = history.latest_assignment(sid)
        if existing is not None:
            return existing.handle
        if sid in assignments:
            return assignments[sid]
        session = sessions.get(sid)
        if session is None:
            raise ValueError("Historical task has no retained owning session")
        handle = _mapped("session:" + sid)
        if history.task_snapshot(handle) is None:
            stamp = session.updated_at
            details = history.write_content(
                sid, AssignmentDetails(disposition="complete").model_dump(mode="json")
            )
            history.record_snapshot(
                TaskSnapshot(
                    handle=handle,
                    kind="Session",
                    owner=TaskOwner(
                        human_id=str(
                            session.metadata.get("owner_human_id") or app.state.human_principal
                        ),
                        session_id=sid,
                        assignment_id=handle,
                    ),
                    label=session.title,
                    invocation_id="legacy-session:" + sid,
                    tool="conversation",
                    created_at=session.created_at,
                    updated_at=stamp,
                    status="interrupted",
                    raw_status="historical_session",
                    terminal_at=stamp,
                    settlement="unknown",
                    details=details,
                    supported_actions=("observe", "wait", "message", "get_result"),
                ),
                observation_id="migration:" + handle,
            )
        return handle

    ordered = sorted(latest.items(), key=lambda pair: pair[1][0] != "assignment")
    for identity, (family, sid, row, payload) in ordered:
        try:
            handle = handles[identity]
            owner_sid = str(row.get("child_session_id") or sid) if family == "assignment" else sid
            session = sessions.get(owner_sid)
            if session is None:
                _unresolved(app, identity, "owning_session_unavailable", row)
                continue
            if history.task_snapshot(handle) is None:
                description = row.get("description") or (
                    row.get("prompt") if family == "question" else None
                )
                created = str(
                    row.get("created_at") or payload.get("recorded_at") or session.created_at
                )
                updated = str(row.get("updated_at") or payload.get("recorded_at") or created)
                raw = str(row.get("raw_status") or row.get("status") or "unknown")
                kind = (
                    str(row.get("assignment_kind") or "Subagent")
                    if family == "assignment"
                    else "Question"
                    if family == "question"
                    else str(row.get("kind") or "MCP")
                )
                if kind not in {
                    "Shell",
                    "Download",
                    "Indexing",
                    "MCP",
                    "Session",
                    "Subagent",
                    "Question",
                }:
                    kind = "MCP"
                kind = cast(TaskKind, kind)
                parent_sid = (
                    str(row.get("parent_session_id") or "") if family == "assignment" else owner_sid
                )
                try:
                    parent = (
                        anchor(parent_sid)
                        if parent_sid and (family != "assignment" or kind == "Subagent")
                        else None
                    )
                except ValueError:
                    _unresolved(app, identity, "parent_session_unavailable", row)
                    continue
                owner = TaskOwner(
                    human_id=str(
                        session.metadata.get("owner_human_id") or app.state.human_principal
                    ),
                    session_id=owner_sid,
                    assignment_id=handle if family == "assignment" else parent or "",
                    parent_handle=parent,
                )
                retained = history.write_content(
                    owner_sid,
                    {
                        "format": family,
                        "source_identity": identity,
                        "observation_id": payload.get("observation_id"),
                        "record": row,
                        "delivery": "unknown",
                    },
                )
                details = retained
                resource = None
                if family == "assignment":
                    checkpoint = row.get("context_checkpoint") or {}
                    boundary = (
                        ContextBoundary(
                            session_id=owner_sid,
                            scope=checkpoint["scope"],
                            logical_time=int(checkpoint.get("logical_time", 0)),
                        )
                        if checkpoint.get("scope")
                        else None
                    )
                    details = history.write_content(
                        owner_sid,
                        AssignmentDetails(
                            context=boundary,
                            predecessor=row.get("supersedes_task_id") or None,
                            context_epoch=row.get("context_epoch")
                            or (scope_epoch(boundary.scope) if boundary else ""),
                            context_epoch_after=row.get("context_epoch_after") or None,
                        ).model_dump(mode="json"),
                    )
                elif family == "question":
                    definition = QuestionDefinition(
                        prompt=str(row.get("prompt") or "Question"),
                        kind=row.get("kind", "freeform"),
                        options=row.get("options", ()),
                        allow_freeform=bool(row.get("allow_freeform", True)),
                        expires_at=row.get("expires_at") or None,
                        source=row.get("source", "ask_user"),
                        metadata=row.get("metadata", {}),
                    )
                    details = history.write_content(owner_sid, definition.model_dump(mode="json"))
                    surface = definition.metadata.get("a2ui_surface_id")
                    if isinstance(surface, str) and surface:
                        resource = question_surface_resource(owner_sid, surface)
                backend = row.get("backend") or {}
                backend_key = row.get("key") or {}
                placement = str(row.get("placement") or "local")
                reference: BackendReference | None = BackendReference(
                    owner="conversation"
                    if family == "assignment"
                    else "question"
                    if family == "question"
                    else "native"
                    if family == "native"
                    else "external_mcp",
                    server=str(
                        backend_key.get("server_id") or backend.get("server_id") or "historical"
                    ),
                    auth_scope=str(backend.get("auth_scope") or ""),
                    session=str(
                        backend_key.get("backend_session_id")
                        or backend.get("backend_session_id")
                        or ""
                    ),
                    task_id=str(
                        backend_key.get("task_id") or row.get("task_id") or row.get("id") or handle
                    ),
                    transport="historical_relay"
                    if placement == "relay"
                    else str(row.get("transport") or "unknown"),
                    placement=placement,
                )
                if "legacy_storage_operation" in row:
                    # Provider job and public task IDs are not native MCP identities.
                    reference = None
                    resource = row.get("source_id") or None
                terminal = raw in _TERMINAL
                status = (
                    str(row.get("effective_status") or raw) if terminal else "task_state_uncertain"
                )
                if (
                    terminal
                    and raw == "completed"
                    and isinstance(row.get("result"), dict)
                    and row["result"].get("isError")
                ):
                    status = "failed"
                if family == "question":
                    status = (
                        "completed"
                        if raw == "answered"
                        else "input_required"
                        if raw == "pending"
                        else "cancelled"
                        if raw == "cancelled"
                        else "failed"
                    )
                elif family == "assignment" and not terminal and placement != "relay":
                    status = "active"
                value = row.get("result")
                if family == "question" and raw == "answered":
                    value = QuestionReply(
                        answer=str(row.get("answer") or ""),
                        selected_options=row.get("selected_options", ()),
                        metadata=row.get("answer_metadata", {}),
                    ).model_dump(mode="json")
                result = history.write_content(owner_sid, value) if value is not None else None
                tool = str(
                    row.get("tool")
                    or (
                        "conversation"
                        if family == "assignment"
                        else "ask_user"
                        if family == "question"
                        else "historical"
                    )
                )
                task = TaskSnapshot.read_history(
                    {
                        "handle": handle,
                        "kind": kind,
                        "owner": owner,
                        "description": description,
                        "description_missing": description is None,
                        "label": str(row.get("run_label") or row.get("tool") or session.title),
                        "invocation_id": str(
                            row.get("invocation_id")
                            or row.get("parent_turn_id")
                            or "migration:" + handle
                        ),
                        "tool": tool,
                        "created_at": created,
                        "updated_at": updated,
                        "backend": reference,
                        "raw_status": raw,
                        "status": status,
                        "activity": "yielded"
                        if family == "assignment" and not terminal
                        else "waiting_for_input"
                        if raw == "pending"
                        else "",
                        "terminal_at": updated if terminal else None,
                        "settlement": "historical_unknown",
                        "details": details,
                        "result": result,
                        "backend_observation": retained,
                        "progress": TaskProgress(
                            message="Historical execution placement is unavailable."
                            if family == "assignment" and placement != "local"
                            else ""
                        ),
                        "resource_id": resource,
                        "cancellation_requested_at": updated
                        if row.get("cancel_requested")
                        else None,
                        "connection_freshness": "unavailable",
                        "attention_episode": "migration:" + handle
                        if status == "task_state_uncertain"
                        else None,
                        "supported_actions": ("observe", "wait", "get_result")
                        if family == "assignment" and placement != "local"
                        else ("observe", "wait", "get_result", "message")
                        if family == "assignment"
                        else ("observe", "wait", "cancel", "answer")
                        if raw == "pending"
                        else ("observe", "wait", "get_result"),
                    }
                )
                history.record_snapshot(task, observation_id="migration:" + handle)
            task = history.task_snapshot(handle)
            aliases = {
                str(row[name])
                for name in ("task_id", "handle_id", "handle", "task_handle", "id")
                if row.get(name)
            }
            for alias in aliases:
                history.record_alias(
                    TaskAlias(alias=alias, handle=handle, source_identity=identity)
                )
            if row.get("notify_pending") and task.terminal_at is not None:
                publish_task_notice(history, task)
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            _unresolved(app, identity, "invalid_historical_record:" + type(error).__name__, row)
    for (recipient, identity), row in mail.items():
        try:
            if not identity or recipient not in sessions:
                _unresolved(
                    app, "mail:" + recipient + ":" + identity, "mailbox_identity_incomplete", row
                )
                continue
            if history.mail_item(recipient, identity) is None:
                content = history.write_content(
                    recipient,
                    {
                        "text": row.get("text", ""),
                        "legacy": row,
                        "message_id": identity,
                        "message": row.get("message"),
                        "metadata": row.get("metadata") or {},
                        "reply_to": row.get("reply_to") or None,
                        "delivery": "unknown",
                    },
                )
                kind = _legacy_mail_kind(row)
                ambiguous = bool(
                    row.get("delivered_at")
                    or row.get("claimed_at")
                    or row.get("consumed_at")
                    or row.get("state") in {"claimed", "consumed"}
                )
                missing_sender = kind == "peer" and not row.get("source_session_id")
                history.record_mail(
                    MailItem(
                        id=identity,
                        recipient_session=recipient,
                        sender_session=row.get("source_session_id") or None,
                        sender_human=str(
                            row.get("actor_human")
                            or sessions[recipient].metadata.get("owner_human_id")
                            or app.state.human_principal
                        )
                        if kind in {"human", "control"}
                        else None,
                        kind=kind,
                        content=content,
                        delivery="held" if ambiguous or missing_sender else "eligible",
                        held_reason="historical_sender_unavailable"
                        if missing_sender
                        else "historical_delivery_unknown"
                        if ambiguous
                        else "",
                        handle=history.latest_assignment(recipient).handle
                        if history.latest_assignment(recipient)
                        else anchor(recipient),
                        created_at=str(
                            row.get("accepted_at")
                            or row.get("recorded_at")
                            or sessions[recipient].created_at
                        ),
                    )
                )
            if (
                (recipient, identity) in withdrawals
                and not history.mail_withdrawn(recipient, identity)
                and identity not in history.acknowledged_notifications(recipient)
            ):
                history.withdraw_mail(recipient, identity)
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            _unresolved(
                app,
                "mail:" + recipient + ":" + identity,
                "invalid_historical_record:" + type(error).__name__,
                row,
            )
    for (sid, _), row in subscriptions.items():
        handle = history.resolve_handle(str(row.get("handle") or ""))
        if handle is not None and not any(
            item.recipient_session == sid for item in history.subscriptions(handle)
        ):
            history.record_subscription(
                TaskSubscription(
                    handle=handle,
                    recipient_session=sid,
                    enabled=bool(row.get("active", row.get("enabled", True))),
                    updated_at=str(row.get("recorded_at") or sessions[sid].updated_at),
                )
            )
    for (sid, identity), row in grants.items():
        try:
            recipient = str(row.get("recipient") or sid)
            target = row.get("target_scope") or {}
            human = str(row.get("granting_human") or "")
            if (
                not identity
                or not human
                or recipient not in sessions
                or target.get("session_id") not in sessions
            ):
                continue  # the original event remains readable; incomplete data grants no rights
            if any(grant.id == identity for grant in history.coordination_grants(recipient)):
                continue
            stamp = str(row.get("recorded_at") or sessions[recipient].updated_at)
            exact = history.resolve_handle(str(target.get("handle") or ""))
            if target.get("handle") and exact is None:
                continue  # an ambiguous exact grant cannot become a session grant
            history.record_grant(
                CoordinationGrant(
                    id=identity,
                    granting_human=human,
                    recipient_session=recipient,
                    target_session=target["session_id"],
                    target_handle=exact,
                    target_scope="tree" if target.get("descendants") else "session",
                    rights=tuple(row.get("rights", ())),
                    created_at=stamp,
                    revoked_at=stamp if row.get("revoked") else None,
                ),
                observation_id="migration-grant:" + identity,
            )
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            _unresolved(
                app,
                "grant:" + sid + ":" + identity,
                "invalid_historical_record:" + type(error).__name__,
                row,
            )
    # Relationships must be restored before terminal publication. Unknown historical
    # owner delivery is retained as held mail, rather than guessed delivered or
    # automatically repeated. Actual retained input proofs below can acknowledge it.
    for identity, (_, _, row, _) in ordered:
        task = history.task_snapshot(handles[identity])
        if task is None:
            continue
        if task.status == "task_state_uncertain":
            publish_task_notice(
                history, task, episode=task.attention_episode or "migration:" + task.handle
            )
            continue
        if task.terminal_at is None:
            continue
        if not row.get("notify_pending") and not any(
            item.enabled for item in history.subscriptions(task.handle)
        ):
            continue
        publish_task_notice(history, task)
        owner_recipient = task_owner_recipient(history, task)
        notice = f"outcome:{task.handle}:{owner_recipient}"
        item = history.mail_item(owner_recipient, notice) if owner_recipient is not None else None
        if item is not None and not row.get("notify_pending") and item.delivery == "eligible":
            history.update_mail(
                item.model_copy(
                    update={
                        "delivery": "held",
                        "held_reason": "historical_delivery_unknown",
                        "revision": item.revision + 1,
                    }
                ),
                expected_revision=item.revision,
            )
    finish_legacy_operations(app, storage_rows, storage_receipts)
    _migrate_intents(app, sessions, anchor)
    _migrate_inputs(app, anchor)
    migrate_context_epochs(app)
