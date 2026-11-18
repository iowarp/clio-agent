"""A2UI surfaces an MCP server sends in its results (host semantics HS1).

A plugin server may return A2UI as an embedded resource with MIME type
``application/a2ui+json`` (see ``docs/design/host-semantics-contract.md``). CLIO
validates each payload against the server's plugin catalogs, stores the surface with
the server as its owner, and replaces the payload in the model's text with a one-line
note. Nothing here names a particular plugin or server.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from clio_agent.gact.a2ui import (
    A2UICatalogNotProducibleError,
    A2UICatalogUnknownError,
    A2UISurfaceOwnerError,
    A2UIValidationError,
)
from clio_agent.gact.events import Event
from clio_agent.gact.protocol.constants import A2UI_WIRE_VERSIONS
from clio_agent.tools.mcp_result_text import is_a2ui_block

logger = logging.getLogger(__name__)

REFUSED_EVENT = "a2ui.server_surface.refused"


class ServerSurfaceRefusal(ValueError):
    """A payload CLIO will not apply, with its typed HS1 reason."""

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


@dataclass(frozen=True)
class ServerScope:
    """The plugin server a result came from, as one session mounts it."""

    plugin: str
    server: str
    namespace: str
    host: Mapping[str, Any] = field(default_factory=dict)

    @property
    def owner(self) -> dict[str, str]:
        return {
            "kind": "server",
            "plugin": self.plugin,
            "server": self.server,
            "namespace": self.namespace,
        }

    @property
    def producible(self) -> frozenset[str]:
        return frozenset(str(c) for c in self.host.get("catalogs") or ())


@dataclass
class PayloadOutcome:
    """What one A2UI payload did."""

    uri: str
    surface_ids: list[str] = field(default_factory=list)
    reason: str = ""
    detail: str = ""

    @property
    def note(self) -> str:
        if self.reason:
            return f"[A2UI payload refused: {self.reason}]"
        return f"[A2UI surface {', '.join(self.surface_ids)} shown to the user]"


def _field(item: Any, *names: str) -> Any:
    for name in names:
        value = item.get(name) if isinstance(item, Mapping) else getattr(item, name, None)
        if value is not None:
            return value
    return None


def result_blocks(result: Any) -> list[Any]:
    """The content blocks of a tool result (an SDK model or its JSON form)."""

    content = _field(result, "content")
    return list(content) if isinstance(content, list | tuple) else []


def a2ui_resources(blocks: list[Any]) -> list[Any]:
    """The embedded A2UI resources among ``blocks``, in content order."""

    return [_field(block, "resource") for block in blocks if is_a2ui_block(block)]


def is_a2ui_contents(item: Any) -> bool:
    """Whether one ``resources/read`` contents item is an A2UI payload."""

    mime = str(_field(item, "mime_type", "mimeType") or "")
    return mime.split(";", 1)[0].strip().lower() == "application/a2ui+json"


def payload_messages(resource: Any) -> list[dict[str, Any]]:
    """Parse one A2UI resource's ``text`` into its ordered message batch.

    Raises:
        ServerSurfaceRefusal: ``a2ui_payload_invalid`` or ``a2ui_version_unsupported``.
    """

    if _field(resource, "blob") is not None:
        raise ServerSurfaceRefusal("a2ui_payload_invalid", "A2UI must be text, not a blob")
    text = _field(resource, "text")
    if not isinstance(text, str):
        raise ServerSurfaceRefusal("a2ui_payload_invalid", "A2UI resource has no text")
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise ServerSurfaceRefusal("a2ui_payload_invalid", f"A2UI text is not JSON: {exc}") from exc
    messages = value if isinstance(value, list) else [value]
    if not messages or not all(isinstance(row, dict) for row in messages):
        raise ServerSurfaceRefusal(
            "a2ui_payload_invalid", "A2UI text must be a message object or a non-empty array"
        )
    for row in messages:
        if row.get("version") not in A2UI_WIRE_VERSIONS:
            raise ServerSurfaceRefusal(
                "a2ui_version_unsupported",
                f"A2UI version {row.get('version')!r} is not one of {sorted(A2UI_WIRE_VERSIONS)}",
            )
    return messages


def refusal_of(exc: A2UIValidationError) -> ServerSurfaceRefusal:
    """The typed HS1 refusal for a validation error from the A2UI fold."""

    if isinstance(exc, A2UISurfaceOwnerError):
        return ServerSurfaceRefusal(exc.reason, str(exc))
    if isinstance(exc, A2UICatalogUnknownError):
        return ServerSurfaceRefusal("a2ui_catalog_unknown", str(exc))
    if isinstance(exc, A2UICatalogNotProducibleError):
        return ServerSurfaceRefusal("a2ui_catalog_not_producible", str(exc))
    return ServerSurfaceRefusal("a2ui_validation_failed", str(exc))


def touched_surface_ids(messages: list[Mapping[str, Any]]) -> list[str]:
    """Surface ids a batch names, in first-seen order."""

    ids: list[str] = []
    for message in messages:
        for operation in ("createSurface", "updateComponents", "updateDataModel", "deleteSurface"):
            payload = message.get(operation)
            if isinstance(payload, Mapping):
                surface_id = str(payload.get("surfaceId") or "")
                if surface_id and surface_id not in ids:
                    ids.append(surface_id)
    return ids


def server_scope(app: Any, session_id: str, namespace: str) -> ServerScope | None:
    """The plugin server mounted as ``namespace`` in the session, or ``None``."""

    from clio_agent.gact.agents.prompt_basis import mounted_servers  # noqa: PLC0415
    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        SessionAgentUnavailable,
        session_resolution,
    )

    if not namespace or not session_id:
        return None
    try:
        rows = mounted_servers(app, session_id, session_resolution(app, session_id))
    except SessionAgentUnavailable:
        return None
    row = rows.get(namespace)
    if not isinstance(row, Mapping) or not row.get("plugin"):
        return None
    return ServerScope(
        plugin=str(row["plugin"]),
        server=str(namespace),
        namespace=str(namespace),
        host=dict(row.get("host") or {}),
    )


def publish_refusal(
    app: Any, session_id: str, scope: ServerScope, *, tool: str, outcome: PayloadOutcome
) -> None:
    """Record a refused payload on the session and tell the UI."""

    payload = {
        "plugin": scope.plugin,
        "server": scope.server,
        "tool": tool,
        "uri": outcome.uri,
        "reason": outcome.reason,
        "detail": outcome.detail,
    }
    catalogs = getattr(app.state, "a2ui_catalogs", None)
    if catalogs is not None:
        catalogs.record_session_reason(
            session_id,
            "a2ui_server_surface_refused",
            refusal=outcome.reason,
            plugin=scope.plugin,
            server=scope.server,
            tool=tool,
            uri=outcome.uri,
        )
    bus = getattr(app.state, "bus", None)
    if bus is not None:
        bus.publish(Event(type=REFUSED_EVENT, session_id=session_id, payload=payload))


def apply_to_conversation(
    app: Any,
    session_id: str,
    scope: ServerScope,
    resource: Any,
    *,
    part_id: str = "",
    persist_part: Callable[[Any], bool] | None = None,
) -> PayloadOutcome:
    """Apply one payload to the session's conversation surfaces."""

    from clio_agent.gact.a2ui_producer import _emit  # noqa: PLC0415

    outcome = PayloadOutcome(uri=str(_field(resource, "uri") or ""))
    try:
        messages = payload_messages(resource)
        result = app.state.a2ui_store.apply_batch_outcome(
            session_id,
            messages,
            part_id=part_id,
            persist_part=persist_part
            or (lambda candidate: _emit.emit_surface_part(app, session_id, candidate)),
            owner=scope.owner,
            producible=scope.producible,
        )
    except ServerSurfaceRefusal as exc:
        outcome.reason, outcome.detail = exc.reason, exc.detail
    except A2UIValidationError as exc:
        refusal = refusal_of(exc)
        outcome.reason, outcome.detail = refusal.reason, refusal.detail
    else:
        outcome.surface_ids = list(dict.fromkeys(surface.id for surface in result.surfaces))
    return outcome


def apply_result(
    app: Any,
    session_id: str,
    namespace: str,
    tool: str,
    raw_result: Any,
    *,
    part_prefix: str = "",
) -> list[PayloadOutcome] | None:
    """Apply every A2UI payload in a plugin server's tool result to the conversation.

    Returns one outcome per payload in content order, or ``None`` when the result
    carries no A2UI or did not come from a plugin server the session mounts.
    ``part_prefix`` makes the transcript part ids deterministic, so a retried
    application of the same result is refused instead of applied twice.
    """

    resources = a2ui_resources(result_blocks(raw_result))
    if not resources:
        return None
    scope = server_scope(app, session_id, namespace)
    if scope is None:
        return None
    outcomes: list[PayloadOutcome] = []
    for index, resource in enumerate(resources):
        outcome = apply_to_conversation(
            app,
            session_id,
            scope,
            resource,
            part_id=f"{part_prefix}_{index}" if part_prefix else "",
        )
        if outcome.reason:
            publish_refusal(app, session_id, scope, tool=tool, outcome=outcome)
        outcomes.append(outcome)
    return outcomes


def _make_result_observer(app: Any) -> Callable[[str, Any, str], list[str] | None]:
    def observe(name: str, raw_result: Any, namespace: str) -> list[str] | None:
        from clio_agent.gact.runtime.globals import _resolve_tool_session  # noqa: PLC0415

        if not a2ui_resources(result_blocks(raw_result)):
            return None
        sid, _session = _resolve_tool_session(app)
        if not sid:
            return None
        tool = name[len(namespace) + 1 :] if name.startswith(f"{namespace}_") else name
        outcomes = apply_result(app, sid, namespace, tool, raw_result)
        return [outcome.note for outcome in outcomes] if outcomes else None

    return observe


def apply_task_result(app: Any, session_id: str, namespace: str, tool: str, handle: str, result: Any) -> None:
    """Apply A2UI from a task's terminal result (HS1), once per task."""

    try:
        from clio_agent.gact import plugin_tabs  # noqa: PLC0415

        if plugin_tabs.apply_task_result(app, handle, result):
            return
        apply_result(app, session_id, namespace, tool, result, part_prefix=f"a2ui_{handle}")
    except Exception:  # noqa: BLE001 - recording the task's outcome must not fail on its UI
        logger.exception(
            "A2UI from a task result failed task=%s reason=a2ui_server_surface_failed", handle
        )


def install_server_surfaces(app: Any) -> None:
    """Install the per-app tool-result observer that applies server A2UI."""

    app.state.pending_a2ui_result_observer = _make_result_observer(app)


__all__ = [
    "REFUSED_EVENT",
    "PayloadOutcome",
    "ServerScope",
    "ServerSurfaceRefusal",
    "a2ui_resources",
    "apply_result",
    "apply_task_result",
    "apply_to_conversation",
    "install_server_surfaces",
    "is_a2ui_contents",
    "payload_messages",
    "publish_refusal",
    "refusal_of",
    "result_blocks",
    "server_scope",
    "touched_surface_ids",
]
