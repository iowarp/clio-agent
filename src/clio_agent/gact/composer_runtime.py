"""Runtime state wiring for message intents and workspace resources."""

from __future__ import annotations

import logging
from collections.abc import Callable
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


def initialize_composer_state(
    app: Any,
    session_store_path: Path,
    append_message: AppendMessage,
) -> None:
    """Install durable composer stores and recover accepted pending steers."""

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
    converter_factory = ResourceConverterFactory([DocumentProcessorClient(processor_url)])
    converter_factory.discover_entry_points()
    app.state.resource_converter_factory = converter_factory
    _recover_pending_steers(app, append_message)


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


def _install_composer_idle_hook(app: Any, deps: Any) -> None:
    """Drain residual steers first, then start the head queued message when idle.

    This REPLACES the idle hook ``build_app`` installed earlier, and calls the
    same ``drain_inbox_and_notify_spotter`` body it used — the queue promotion is
    appended after it, never instead of it.
    """

    from clio_agent.gact.events import Event  # noqa: PLC0415
    from clio_agent.gact.loop_inbox import drain_inbox_and_notify_spotter  # noqa: PLC0415
    from clio_agent.gact.message_submission import accept_message  # noqa: PLC0415
    from clio_agent.gact.types import PostMessageRequest  # noqa: PLC0415

    def on_session_idle(session_id: str) -> None:
        drain_inbox_and_notify_spotter(app, session_id)
        if app.state.turn_runner.busy(session_id):
            return
        queued = app.state.message_intents.list_queued(session_id)
        if not queued:
            return
        head = queued[0]
        try:
            promoted = app.state.message_intents.promote_queued(
                session_id,
                head.id,
                head.revision,
                lambda row: accept_message(
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
                ),
            )
        except Exception:  # noqa: BLE001 - retain the durable row and emit typed failure
            logger.exception(
                "queued-message auto-promotion failed session=%s message=%s",
                session_id,
                head.id,
            )
            app.state.bus.publish(
                Event(
                    type="queued_message.promotion_failed",
                    session_id=session_id,
                    payload={
                        "queued_message_id": head.id,
                        "error": "queue_auto_promotion_failed",
                        "recoverable": True,
                    },
                )
            )
            return
        if promoted is None:
            return
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

    app.state.turn_runner.set_idle_hook(on_session_idle)


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
    return {
        "max_bytes": int(store.max_resource_bytes),
        "converters": factory.capabilities() if factory is not None else [],
    }


def delete_workspace_resources(app: Any, workspace_id: str) -> None:
    """Delete workspace-owned resource bytes and provider-delivery records."""

    for store_name in ("resource_store", "resource_delivery_store"):
        store = getattr(app.state, store_name, None)
        if store is not None:
            store.delete_workspace(workspace_id)


def _recover_pending_steers(app: Any, append_message: AppendMessage) -> None:
    """Restore accepted pending user identities after a process interruption."""

    for pending in app.state.message_intents.list_all_pending():
        if app.state.sessions.get(pending.session_id) is None:
            continue
        messages = app.state.messages.get(pending.session_id, [])
        if any(message.id == pending.message_id for message in messages):
            continue
        metadata = {**pending.metadata, "pending_steer": True}
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
