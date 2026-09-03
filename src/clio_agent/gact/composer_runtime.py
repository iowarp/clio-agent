"""Runtime state wiring for message intents and workspace resources."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from clio_agent import conf
from clio_agent.gact.message_intents import MessageIntentStore
from clio_agent.gact.resource_custody import ResourceStore
from clio_agent.gact.resource_delivery import ResourceDeliveryStore
from clio_agent.gact.resource_processing import (
    DocumentProcessorClient,
    ResourceConverterFactory,
    ResourceProcessingStore,
)
from clio_agent.gact.types import Message

AppendMessage = Callable[[Any, str, Message], None]
logger = logging.getLogger(__name__)


def initialize_composer_state(app: Any, session_store_path: Path) -> None:
    """Install the durable composer stores.

    Pending-steer RECOVERY is deliberately not done here: it re-enqueues onto
    ``app.state.loop_inboxes``, which ``build_app`` creates later, so it runs
    from :func:`register_composer_routes` instead.
    """

    state_root = session_store_path.parent
    app.state.message_intents = MessageIntentStore(path=state_root / "message_intents.json")
    max_resource_bytes = conf.resolve(
        "resources.max_bytes",
        env="CLIO_RESOURCE_MAX_BYTES",
        default=250 * 1024 * 1024,
        cast=conf.as_int,
    )
    app.state.resource_store = ResourceStore(
        root=state_root / "resources",
        max_resource_bytes=max_resource_bytes,
    )
    app.state.resource_delivery_store = ResourceDeliveryStore(
        state_root / "resource_deliveries.json"
    )
    processor_url = conf.resolve(
        "resources.document_processor_url",
        env="CLIO_DOCUMENT_PROCESSOR_URL",
        default="",
        cast=conf.as_str,
    )
    app.state.resource_processing_store = ResourceProcessingStore(app.state.resource_store)
    app.state.resource_converter_factory = ResourceConverterFactory(
        [DocumentProcessorClient(processor_url, max_resource_bytes=max_resource_bytes)]
    )


def register_composer_routes(app: Any, deps: Any) -> None:
    """Register the message-intent, resource, and provider-catalog routes.

    The A2UI routes are NOT registered here: ``build_app`` already owns that
    registration (and ``runtime.rework_state.initialize_a2ui_store`` owns the
    store), so re-registering them would duplicate the surface.
    """

    from clio_agent.gact.routes.message_intents import (  # noqa: PLC0415
        register_message_intent_routes,
    )
    from clio_agent.gact.routes.provider_catalog import (  # noqa: PLC0415
        register_normalized_provider_catalog_routes,
    )
    from clio_agent.gact.routes.resources import register_resource_routes  # noqa: PLC0415

    register_message_intent_routes(app, deps)
    register_resource_routes(app, deps)
    register_normalized_provider_catalog_routes(app, deps)
    _install_composer_idle_hook(app, deps)
    # Runs here, not in initialize_composer_state: recovery re-enqueues onto
    # app.state.loop_inboxes, which build_app creates between the two calls.
    _recover_pending_steers(app, deps.append_session_message)


def session_autostart_suspended(app: Any, session_id: str) -> bool:
    """True when a session must not START work on its own.

    A cancelled session is the one such state today: ``/cancel`` is the user
    saying *stop*, and the durable composer planes (a residual steer, a queued
    head) would otherwise re-drive the agent the instant the cancelled turn's
    slot cleared — Esc restarting the very turn it stopped. The SESSION STATUS is
    the server truth here, not a cancel flag: the flag is already cleared by the
    time the idle hook runs, whereas the cancelled turn's finalize (and
    ``cancel_session_state`` before it) stamps ``status="cancelled"`` and it
    stays until the user explicitly sends again.
    """

    session = app.state.sessions.get(session_id)
    return str(getattr(session, "status", "") or "") == "cancelled"


def stop_session_composer_autostart(app: Any, session_id: str) -> dict[str, Any]:
    """Quiesce the composer's two turn producers for a cancelled session.

    The canonical-stop sibling of ``stop_session_loop`` / ``stop_session_goal``,
    called from ``cancel_session_state``. Cancelling stops a session from
    STARTING work; it does NOT delete the user's durable intent, so the pending
    steers and queued messages stay listed, editable and cancellable — they
    simply stop auto-promoting until the user sends again. The retained counts
    ride the cancellation payload so the suspension is a recorded fact, never a
    silent swallow.
    """

    from clio_agent.gact.events import Event  # noqa: PLC0415

    intents = getattr(app.state, "message_intents", None)
    if intents is None:
        return {"reason": "composer_state_unavailable", "suspended": False}
    summary = {
        "reason": "session_cancelled",
        "suspended": True,
        "retained_pending_steers": len(intents.list_pending(session_id)),
        "retained_queued_messages": len(intents.list_queued(session_id)),
    }
    if summary["retained_pending_steers"] or summary["retained_queued_messages"]:
        app.state.bus.publish(
            Event(
                type="composer.autostart_suspended",
                session_id=session_id,
                payload={"session_id": session_id, **summary},
            )
        )
    return summary


def _install_composer_idle_hook(app: Any, deps: Any) -> None:
    """Start the head queued message when a session goes idle.

    COMPOSES with the idle hook ``build_app`` installed earlier (the loop-inbox
    drain + SPOTTER clearance) rather than replacing it: the previous hook runs
    first and the queue promotion is appended after it. Overwriting the slot is
    what silently unregistered the drain.
    """

    from clio_agent.gact.loop_inbox import drain_inbox_and_notify_spotter  # noqa: PLC0415

    previous = app.state.turn_runner.idle_hook

    def on_session_idle(session_id: str) -> None:
        if previous is not None:
            previous(session_id)
        else:  # pragma: no cover - build_app always installs the drain first
            drain_inbox_and_notify_spotter(app, session_id)
        promote_queue_head(app, deps, session_id)

    app.state.turn_runner.set_idle_hook(on_session_idle)


def promote_queue_head(app: Any, deps: Any, session_id: str) -> None:
    """Attempt one auto-promotion of ``session_id``'s queue head.

    Re-driven from every point where the queue can become promotable: a turn
    going idle and any queued-message mutation. Never starts work on a busy or
    autostart-suspended session.
    """

    from clio_agent.gact.events import Event  # noqa: PLC0415
    from clio_agent.gact.message_intents import RevisionConflictError  # noqa: PLC0415
    from clio_agent.gact.message_submission import accept_message  # noqa: PLC0415
    from clio_agent.gact.types import PostMessageRequest  # noqa: PLC0415

    if app.state.turn_runner.busy(session_id):
        return
    if session_autostart_suspended(app, session_id):
        # Typed, not silent: the queue is intact and the user's next explicit
        # send lifts the suspension.
        queued = app.state.message_intents.list_queued(session_id)
        if queued:
            logger.info(
                "queued-message auto-promotion suspended session=%s reason=session_cancelled "
                "retained=%d",
                session_id,
                len(queued),
            )
        return

    def _accept(row: Any) -> Any:
        return accept_message(
            app,
            deps,
            session_id,
            PostMessageRequest(
                parts=row.parts,
                model=row.model,
                metadata=row.metadata,
                client_message_id=row.client_message_id,
                idempotency_key=row.idempotency_key or row.id,
                delivery="start",
                behavior=row.behavior,
            ),
        )

    # A racing edit/reorder bumps the head's revision between the read and the
    # promote. That is a stale READ, not a failure of intent: re-read the head
    # and retry ONCE rather than freezing the queue until an unrelated turn ends.
    for attempt in range(2):
        queued = app.state.message_intents.list_queued(session_id)
        if not queued:
            _clear_blocked_head(app, session_id)
            return
        head = queued[0]
        if _head_is_blocked(app, session_id, head):
            # Already reported terminal at this exact revision. Re-attempting it on
            # every idle transition and every queue mutation would fail identically
            # forever, re-emitting the same failure; the row is durable and the
            # client has been told which one to edit.
            return
        try:
            promoted = app.state.message_intents.promote_queued(
                session_id, head.id, head.revision, _accept
            )
        except RevisionConflictError:
            if attempt == 0:
                continue
            logger.warning(
                "queued-message auto-promotion lost the revision race twice session=%s message=%s",
                session_id,
                head.id,
            )
            _publish_promotion_failure(
                app, session_id, head.id, "queue_revision_conflict", cause={}, terminal=False
            )
            return
        except Exception as exc:  # noqa: BLE001 - retain the durable row and emit typed failure
            logger.exception(
                "queued-message auto-promotion failed session=%s message=%s",
                session_id,
                head.id,
            )
            cause = _typed_cause(exc)
            terminal = _is_terminal_cause(cause)
            _publish_promotion_failure(
                app,
                session_id,
                head.id,
                "queue_auto_promotion_failed",
                cause=cause,
                terminal=terminal,
            )
            if terminal:
                _mark_head_blocked(app, session_id, head, cause)
            return
        if promoted is None:
            return
        _clear_blocked_head(app, session_id)
        _deleted, (ack, status_code) = promoted
        app.state.bus.publish(
            Event(
                type="queued_message.promoted",
                session_id=session_id,
                payload={
                    "queued_message_id": head.id,
                    "acceptance": ack.model_dump(),
                    "status_code": status_code,
                    "automatic": True,
                },
            )
        )
        return


def _blocked_heads(app: Any) -> dict[str, dict[str, Any]]:
    """Per-session record of a queue head that failed terminally, keyed by revision."""

    rows = getattr(app.state, "queue_blocked_heads", None)
    if not isinstance(rows, dict):
        rows = {}
        app.state.queue_blocked_heads = rows
    return rows


def _head_is_blocked(app: Any, session_id: str, head: Any) -> bool:
    """True when THIS head, at THIS revision, already failed terminally."""

    row = _blocked_heads(app).get(session_id)
    return bool(
        row and row.get("queued_message_id") == head.id and row.get("revision") == head.revision
    )


def _clear_blocked_head(app: Any, session_id: str) -> None:
    """Forget a session's blocked head (it promoted, or the queue emptied)."""

    _blocked_heads(app).pop(session_id, None)


def blocked_queue_head(app: Any, session_id: str) -> dict[str, Any] | None:
    """The queue head this session is frozen behind, if any (read-side helper)."""

    row = _blocked_heads(app).get(session_id)
    return dict(row) if row else None


def _typed_cause(exc: BaseException) -> dict[str, Any]:
    """Unwrap the typed error envelope acceptance raised, when there is one.

    The auto-promoter used to swallow the ``HTTPException`` acceptance raised
    into a generic ``queue_auto_promotion_failed``, so a client saw a bare
    "something went wrong" with a ``retry_on`` implying recovery -- while the
    manual ``POST .../promote`` door, which lets the exception through, showed the
    real reason. The two doors now agree: the typed cause rides the payload.
    """

    from fastapi import HTTPException  # noqa: PLC0415

    if not isinstance(exc, HTTPException):
        return {"status_code": 0, "error": type(exc).__name__, "message": str(exc)}
    detail: Mapping[str, Any] = exc.detail if isinstance(exc.detail, Mapping) else {}
    raw_error = detail.get("error")
    error: Mapping[str, Any] = raw_error if isinstance(raw_error, Mapping) else {}
    return {
        "status_code": int(exc.status_code),
        "error": str(error.get("error") or "http_error"),
        "message": str(error.get("message") or ""),
        "details": dict(error.get("details") or {}),
        "recoverable": bool(error.get("recoverable", False)),
    }


def _is_terminal_cause(cause: Mapping[str, Any]) -> bool:
    """A 4xx cause will not fix itself; re-driving it forever is a silent freeze."""

    status = int(cause.get("status_code") or 0)
    return 400 <= status < 500


def _mark_head_blocked(app: Any, session_id: str, head: Any, cause: Mapping[str, Any]) -> None:
    """Emit the distinct terminal reason naming the row the client must edit.

    Nothing is deleted: the queued row stays durable, listed and editable. What
    changes is the story the client is told -- the head will NOT recover on its
    own, and the queue behind it is frozen until this row is edited or removed.
    """

    from clio_agent.gact.events import Event  # noqa: PLC0415

    logger.warning(
        "queued-message head blocked reason=queue_head_blocked session=%s message=%s cause=%s",
        session_id,
        head.id,
        cause.get("error"),
    )
    _blocked_heads(app)[session_id] = {
        "queued_message_id": head.id,
        "revision": head.revision,
        "cause": dict(cause),
    }
    app.state.bus.publish(
        Event(
            type="queued_message.head_blocked",
            session_id=session_id,
            payload={
                "queued_message_id": head.id,
                "reason": "queue_head_blocked",
                "cause": dict(cause),
                "recoverable": False,
                "blocks_queue": True,
                "recovery_actions": ["edit_queued_message", "delete_queued_message"],
            },
        )
    )


def _publish_promotion_failure(
    app: Any,
    session_id: str,
    message_id: str,
    error: str,
    *,
    cause: Mapping[str, Any],
    terminal: bool,
) -> None:
    """Emit the typed promotion failure. The row stays durable either way.

    ``retry_on`` names the concrete events that re-drive a RECOVERABLE failure. A
    terminal (4xx) cause carries no ``retry_on`` at all, because promising a
    retry that can only fail again is exactly the head-of-line freeze this
    reports.
    """

    from clio_agent.gact.events import Event  # noqa: PLC0415

    payload: dict[str, Any] = {
        "queued_message_id": message_id,
        "error": error,
        "cause": dict(cause),
        "recoverable": not terminal,
    }
    if terminal:
        payload["blocks_queue"] = True
        payload["recovery_actions"] = ["edit_queued_message", "delete_queued_message"]
    else:
        payload["retry_on"] = ["session_idle", "queue_mutation", "manual_promote"]
    app.state.bus.publish(
        Event(
            type="queued_message.promotion_failed",
            session_id=session_id,
            payload=payload,
        )
    )


def resource_capabilities(app: Any) -> dict[str, Any]:
    """Report the workspace-resource service's live limits and converter registry.

    A client must know the upload ceiling BEFORE it starts a resumable upload,
    and which converters are configured before it expects a structured view.
    Both are read off the stores this module built rather than restated, so the
    advertised contract cannot drift from what the service actually enforces.
    """

    store = getattr(app.state, "resource_store", None)
    if store is None:
        return {}
    factory = getattr(app.state, "resource_converter_factory", None)
    # A quarantined index is a REAL capability statement: the service is up but
    # its history is gone, and a client that sees an empty resource list is
    # entitled to know why rather than concluding nothing was ever uploaded.
    degradations = [
        row
        for row in (
            getattr(store, "load_degradation", None),
            getattr(getattr(app.state, "resource_delivery_store", None), "load_degradation", None),
        )
        if row
    ]
    return {
        "enabled": True,
        "max_bytes": int(store.max_resource_bytes),
        "converters": factory.capabilities() if factory is not None else [],
        "degradations": degradations,
    }


def delete_workspace_resources(app: Any, workspace_id: str) -> None:
    """Delete workspace-owned resource bytes and provider-delivery records."""

    for store_name in ("resource_store", "resource_delivery_store"):
        store = getattr(app.state, store_name, None)
        if store is not None:
            store.delete_workspace(workspace_id)


def _recover_pending_steers(app: Any, append_message: AppendMessage) -> None:
    """Restore accepted pending user identities after a process interruption.

    Two halves, and the second was missing. The transcript row is restored so the
    client sees the identity its ``202`` promised, AND the delivery intent is
    re-enqueued onto the (in-memory, therefore empty-at-boot) ``LoopInbox`` — a
    ``PendingSteer`` that is durable but has no inbox event is stranded: no drain
    and no idle re-drive will ever look at it, so the user's accepted message is
    silently never delivered. The store already resets ``claimed`` to ``pending``
    on load, so a steer interrupted mid-claim is re-drivable too.
    """

    from clio_agent.gact.loop_inbox import enqueue_user_steer  # noqa: PLC0415

    for pending in app.state.message_intents.list_all_pending():
        if app.state.sessions.get(pending.session_id) is None:
            continue
        metadata = {**pending.metadata, "pending_steer": True}
        messages = app.state.messages.get(pending.session_id, [])
        if not any(message.id == pending.message_id for message in messages):
            append_message(
                app,
                pending.session_id,
                Message(
                    id=pending.message_id,
                    session_id=pending.session_id,
                    role="user",
                    created_at=pending.accepted_at,
                    updated_at=pending.accepted_at,
                    parts=list(pending.parts),
                    metadata=metadata,
                ),
            )
        # Always re-enqueue: the transcript row may have survived in the durable
        # ledger while the in-memory inbox did not.
        enqueue_user_steer(
            app,
            pending.session_id,
            pending.text,
            metadata,
            steer_message_id=pending.message_id,
            steer_created_at=pending.accepted_at,
            steer_parts=list(pending.parts),
        )
        logger.info(
            "recovered pending steer session=%s message=%s reason=restart_recovery",
            pending.session_id,
            pending.message_id,
        )
