"""Runtime state wiring for message intents and workspace resources."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from clio_agent import conf, paths
from clio_agent.gact.resource_custody import ResourceStore
from clio_agent.gact.resource_delivery import ResourceDeliveryStore
from clio_agent.gact.resource_processing import (
    DocumentProcessorClient,
    ResourceConverterFactory,
    ResourceProcessingStore,
)

logger = logging.getLogger(__name__)


def initialize_composer_state(app: Any, session_store_path: Path) -> None:
    """Install the durable composer stores.

    Pending feedback is recovered into Core during application startup, after
    the confirmed writer and conversation operations are available.
    """

    state_root = session_store_path.parent
    app.state.legacy_message_intents_path = state_root / "message_intents.json"
    max_resource_bytes = conf.resolve(
        "resources.max_bytes",
        env="CLIO_RESOURCE_MAX_BYTES",
        default=0,
        cast=conf.as_int,
    )
    app.state.resource_store = ResourceStore(
        root=(
            paths.user_data_dir() / "resources"
            if state_root == paths.server_state_dir()
            else state_root / "resources"
        ),
        max_resource_bytes=max_resource_bytes,
    )
    app.state.resource_delivery_store = ResourceDeliveryStore(
        state_root / "resource_deliveries.json"
    )
    local_processor_url = conf.resolve(
        "resources.document_processor_url",
        env="CLIO_DOCUMENT_PROCESSOR_URL",
        default="",
        cast=conf.as_str,
    )
    processor_url = local_processor_url
    app.state.resource_processing_store = ResourceProcessingStore(app.state.resource_store)
    app.state.resource_converter_factory = ResourceConverterFactory(
        [DocumentProcessorClient(processor_url, max_resource_bytes=max_resource_bytes)]
    )


def register_composer_routes(app: Any, deps: Any) -> None:
    """Register retained resource and provider-catalog routes.

    The A2UI routes are NOT registered here: ``build_app`` already owns that
    registration (and ``runtime.rework_state.initialize_a2ui_store`` owns the
    store), so re-registering them would duplicate the surface.
    """

    from clio_agent.gact.routes.mailbox import register_mailbox_routes
    from clio_agent.gact.routes.provider_catalog import (  # noqa: PLC0415
        register_normalized_provider_catalog_routes,
    )
    from clio_agent.gact.routes.resources import register_resource_routes  # noqa: PLC0415

    register_resource_routes(app, deps)
    register_normalized_provider_catalog_routes(app, deps)
    register_mailbox_routes(app)


def resource_capabilities(app: Any) -> dict[str, Any]:
    """Expose the actual retained resource limits, converters and degradation."""
    store = getattr(app.state, "resource_store", None)
    if store is None:
        return {}
    factory = getattr(app.state, "resource_converter_factory", None)
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
    """Remove workspace-owned resources through their existing stores."""
    for name in ("resource_store", "resource_delivery_store"):
        store = getattr(app.state, name, None)
        if store is not None:
            store.delete_workspace(workspace_id)


def session_autostart_suspended(app: Any, session_id: str) -> bool:
    """Future composer pause is separate from accepted mail and task wakes."""
    session = app.state.sessions.get(session_id)
    return bool(session is not None and session.metadata.get("composer_queue_paused", False))


def stop_session_composer_autostart(app: Any, session_id: str) -> dict[str, Any]:
    """Persist future-message suspension without withdrawing submitted feedback."""
    app.state.sessions.update(session_id, metadata_patch={"composer_queue_paused": True})
    return {
        "suspended": True,
        "pending_steers_suspended": False,
        "queued_messages_paused": True,
    }


def resume_session_composer_autostart(app: Any, session_id: str) -> None:
    """An explicit human send/resume releases only the future composer pause."""
    app.state.sessions.update(session_id, metadata_patch={"composer_queue_paused": False})
    app.state.turn_runner.signal(session_id)
