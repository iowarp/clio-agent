"""Validated, transcript-projected A2UI surface state for GACT 0.3."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlsplit

from clio_schemas import A2UIClientActionMessage, A2UIComponent, trusted_component_names
from pydantic import ValidationError

from clio_agent.gact.protocol.constants import (
    A2UI_V091,
    A2UI_V091_WIRE,
    CLIO_A2UI_CATALOG_ID,
)


def utcnow_iso() -> str:
    """Return a stable UTC timestamp for projection records."""

    return datetime.now(timezone.utc).isoformat()


MAX_A2UI_MESSAGE_BYTES = 256 * 1024
MAX_A2UI_COMPONENTS = 256
MAX_A2UI_MESSAGES = 512
MAX_A2UI_DEPTH = 20
MAX_A2UI_STRING = 16 * 1024

SERVER_ACTIONS = frozenset(
    {"agent.submit", "approval.respond", "form.submit", "run.retry", "run.cancel"}
)
CLIENT_ACTIONS = frozenset({"artifact.open", "data.select", "workflow.focus"})

_FORBIDDEN_KEYS = frozenset(
    {
        "css",
        "style",
        "styles",
        "html",
        "rawHtml",
        "dangerouslySetInnerHTML",
        "srcdoc",
        "script",
        "imports",
        "command",
        "commands",
        "eventHandlers",
        "onClick",
        "onChange",
    }
)


class A2UIValidationError(ValueError):
    """Raised when an A2UI message crosses the trusted catalog boundary."""


@dataclass
class A2UISurfaceRecord:
    """Durable ordered messages and compact surface metadata."""

    id: str
    session_id: str
    catalog_id: str
    protocol_version: str = A2UI_V091
    revision: int = 0
    state: str = "creating"
    messages: list[dict[str, Any]] = field(default_factory=list)
    run_id: str = ""
    message_id: str = ""
    part_id: str = ""
    error: str = ""
    eviction_reason: str = ""
    evicted_messages: int = 0
    created_at: str = field(default_factory=utcnow_iso)
    updated_at: str = field(default_factory=utcnow_iso)

    def to_wire(self) -> dict[str, Any]:
        """Return the normalized frontend surface representation."""

        row = asdict(self)
        return {key: value for key, value in row.items() if value != "" and value is not None}


def _message_operation(message: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
    if message.get("version") != A2UI_V091_WIRE:
        raise A2UIValidationError(f"A2UI message version must be {A2UI_V091_WIRE}")
    operations = [
        key
        for key in ("createSurface", "updateComponents", "updateDataModel", "deleteSurface")
        if key in message
    ]
    if len(operations) != 1:
        raise A2UIValidationError("A2UI message must contain exactly one operation")
    if set(message) != {"version", operations[0]}:
        raise A2UIValidationError("A2UI message contains unknown top-level properties")
    payload = message[operations[0]]
    if not isinstance(payload, Mapping):
        raise A2UIValidationError("A2UI operation payload must be an object")
    return operations[0], payload


def _validate_url(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"https", "artifact", "resource"}:
        raise A2UIValidationError("A2UI URLs must use an allowed non-executable scheme")


def _validate_action(value: Any) -> None:
    """Require the official event-only Action envelope accepted by the renderer."""

    if not isinstance(value, Mapping) or set(value) != {"event"}:
        raise A2UIValidationError(
            'A2UI action must have the shape {"event": {"name": ..., "context": ...}}'
        )
    event = value.get("event")
    if not isinstance(event, Mapping) or not set(event).issubset({"name", "context"}):
        raise A2UIValidationError("A2UI event action contains unknown properties")
    action = str(event.get("name") or "")
    if action not in SERVER_ACTIONS | CLIENT_ACTIONS:
        raise A2UIValidationError(f"A2UI action is not registered: {action}")
    context = event.get("context")
    if context is not None and not isinstance(context, Mapping):
        raise A2UIValidationError("A2UI event action context must be an object")
    context_keys = set(context or {})
    required_context: dict[str, tuple[frozenset[str], str]] = {
        "agent.submit": (
            frozenset({"text", "prompt"}),
            "A2UI agent.submit action requires context.text or context.prompt",
        ),
        "approval.respond": (
            frozenset({"permission_id", "action"}),
            "A2UI approval.respond action requires context.permission_id and context.action",
        ),
        "run.retry": (
            frozenset({"message_id"}),
            "A2UI run.retry action requires context.message_id",
        ),
    }
    requirement = required_context.get(action)
    if requirement is None:
        return
    keys, message = requirement
    if action == "agent.submit":
        if not context_keys.intersection(keys):
            raise A2UIValidationError(message)
    elif not keys.issubset(context_keys):
        raise A2UIValidationError(message)


def _validate_value(value: Any, *, key: str = "", depth: int = 0) -> None:
    if depth > MAX_A2UI_DEPTH:
        raise A2UIValidationError("A2UI value exceeds the nesting limit")
    if isinstance(value, str):
        if len(value) > MAX_A2UI_STRING:
            raise A2UIValidationError("A2UI string exceeds the size limit")
        if key.lower() in {"url", "uri", "datauri"}:
            _validate_url(value)
        return
    if isinstance(value, list):
        for item in value:
            _validate_value(item, depth=depth + 1)
        return
    if not isinstance(value, Mapping):
        return
    if set(value) == {"path"}:
        binding_path = value.get("path")
        if not isinstance(binding_path, str) or not binding_path.startswith("/"):
            raise A2UIValidationError("A2UI data bindings must use JSON Pointer paths")
    for child_key, child_value in value.items():
        if not isinstance(child_key, str):
            raise A2UIValidationError("A2UI object keys must be strings")
        if child_key in _FORBIDDEN_KEYS:
            raise A2UIValidationError(f"A2UI property is prohibited: {child_key}")
        if child_key == "functionCall" or child_key == "call":
            raise A2UIValidationError("A2UI client function calls are not trusted")
        if child_key == "action":
            _validate_action(child_value)
        if child_key == "event" and isinstance(child_value, Mapping):
            action = str(child_value.get("name") or "")
            if action not in SERVER_ACTIONS | CLIENT_ACTIONS:
                raise A2UIValidationError(f"A2UI action is not registered: {action}")
        _validate_value(child_value, key=child_key, depth=depth + 1)


def _validate_accessibility(component: Mapping[str, Any], component_name: str) -> None:
    """Mirror the official renderer's accessibility object at the server boundary.

    ``@a2ui/web_core`` defines ``accessibility`` as an object containing optional
    dynamic ``label`` and ``description`` strings.  Accept literal strings and
    JSON-Pointer bindings; client function calls remain prohibited by CLIO's
    trusted-catalog policy.  Keeping this check server-side prevents a producer
    from receiving a false success for a surface the renderer must reject.
    """

    if "accessibility" not in component:
        return
    accessibility = component.get("accessibility")
    if not isinstance(accessibility, Mapping):
        raise A2UIValidationError(f"A2UI {component_name} accessibility must be an object")
    unknown = set(accessibility) - {"label", "description"}
    if unknown:
        raise A2UIValidationError(
            f"A2UI {component_name} accessibility contains unknown properties: {sorted(unknown)}"
        )
    for key, value in accessibility.items():
        if isinstance(value, str):
            continue
        if (
            not isinstance(value, Mapping)
            or set(value) != {"path"}
            or not isinstance(value.get("path"), str)
            or not value["path"].startswith("/")
        ):
            raise A2UIValidationError(
                f'A2UI {component_name} accessibility.{key} must be a string or {{"path": "/..."}}'
            )


def _component_validation_error(component_name: str, exc: ValidationError) -> str:
    """Translate schema failures into stable CLIO boundary errors."""

    for error in exc.errors():
        location = tuple(error.get("loc", ()))
        for coordinate in ("latitude", "longitude"):
            if coordinate in location:
                return f"A2UI map point {coordinate} is outside its valid range"
    return f"A2UI {component_name} component shape is invalid: {exc.errors()}"


def validate_server_message(message: Mapping[str, Any]) -> tuple[str, str]:
    """Validate an official server-to-client message and return operation/id."""

    encoded = json.dumps(message, separators=(",", ":")).encode()
    if len(encoded) > MAX_A2UI_MESSAGE_BYTES:
        raise A2UIValidationError("A2UI message exceeds the byte limit")
    operation, payload = _message_operation(message)
    allowed_payload_keys = {
        "createSurface": {"surfaceId", "catalogId", "theme", "sendDataModel"},
        "updateComponents": {"surfaceId", "components"},
        "updateDataModel": {"surfaceId", "path", "value"},
        "deleteSurface": {"surfaceId"},
    }[operation]
    unknown_payload = set(payload) - allowed_payload_keys
    if unknown_payload:
        raise A2UIValidationError(
            f"A2UI {operation} contains unknown properties: {sorted(unknown_payload)}"
        )
    surface_id = str(payload.get("surfaceId") or "")
    if not surface_id or len(surface_id) > 128:
        raise A2UIValidationError("A2UI surfaceId is required and bounded to 128 characters")
    if operation == "createSurface":
        if payload.get("catalogId") != CLIO_A2UI_CATALOG_ID:
            raise A2UIValidationError("A2UI catalog is not trusted")
    if operation == "updateComponents":
        components = payload.get("components")
        if not isinstance(components, list) or not 1 <= len(components) <= MAX_A2UI_COMPONENTS:
            raise A2UIValidationError("A2UI components must be a non-empty bounded list")
        for component in components:
            if not isinstance(component, Mapping):
                raise A2UIValidationError("A2UI component must be an object")
            component_name = str(component.get("component") or "")
            if component_name not in trusted_component_names():
                raise A2UIValidationError(f"A2UI component is not trusted: {component_name}")
            try:
                A2UIComponent.model_validate(component)
            except ValidationError as exc:
                raise A2UIValidationError(_component_validation_error(component_name, exc)) from exc
            _validate_accessibility(component, component_name)
            if component_name == "clio.mermaid.v1":
                source = component.get("source")
                if isinstance(source, str) and re.search(
                    r"<|%%\{|\bclick\b|\bhref\b|javascript:|data:text/html|url\s*\(",
                    source,
                    re.IGNORECASE,
                ):
                    raise A2UIValidationError(
                        "A2UI Mermaid source contains an executable or HTML directive"
                    )
    if operation == "updateDataModel":
        path = payload.get("path")
        if not isinstance(path, str) or not path.startswith("/"):
            raise A2UIValidationError("A2UI updateDataModel path must be a JSON Pointer")
    _validate_value(payload)
    return operation, surface_id


def validate_client_action(message: Mapping[str, Any], *, surface_id: str) -> dict[str, Any]:
    """Validate the official 0.9.1 client action envelope."""

    try:
        parsed = A2UIClientActionMessage.model_validate(message)
    except ValidationError as exc:
        raise A2UIValidationError(
            f"A2UI client message must be a {A2UI_V091_WIRE} action: {exc.errors()}"
        ) from exc
    action = parsed.action.model_dump(mode="json")
    if action.get("surfaceId") != surface_id:
        raise A2UIValidationError("A2UI action surface does not match the route")
    _validate_value(action)
    return action


def _apply_staged_message(
    surfaces: dict[tuple[str, str], A2UISurfaceRecord],
    session_id: str,
    message: Mapping[str, Any],
    *,
    deleted_in_batch: set[str],
    run_id: str,
    message_id: str,
    part_id: str,
    observed_at: str,
) -> tuple[str, str, A2UISurfaceRecord]:
    """Apply one validated message to an uncommitted projection."""

    operation, surface_id = validate_server_message(message)
    if surface_id in deleted_in_batch:
        raise A2UIValidationError("A2UI deleteSurface is terminal within a message batch")
    key = (session_id, surface_id)
    surface = surfaces.get(key)
    if operation == "createSurface":
        if surface is not None and surface.state != "deleted":
            raise A2UIValidationError("A2UI surface already exists")
        surface = A2UISurfaceRecord(
            id=surface_id,
            session_id=session_id,
            catalog_id=CLIO_A2UI_CATALOG_ID,
            run_id=run_id,
            message_id=message_id,
            part_id=part_id,
            created_at=observed_at,
            updated_at=observed_at,
        )
        surfaces[key] = surface
    elif surface is None:
        raise A2UIValidationError("A2UI surface does not exist in this session")
    elif surface.state == "deleted":
        raise A2UIValidationError("A2UI deleteSurface is terminal until a new createSurface")
    if operation == "updateComponents":
        surface.messages = [
            existing for existing in surface.messages if "updateComponents" not in existing
        ]
    if len(surface.messages) >= MAX_A2UI_MESSAGES:
        removable = next(
            (
                index
                for index, existing in enumerate(surface.messages)
                if "createSurface" not in existing
            ),
            None,
        )
        if removable is None:
            raise A2UIValidationError("A2UI message limit cannot preserve createSurface")
        surface.messages.pop(removable)
        surface.eviction_reason = "a2ui_message_limit"
        surface.evicted_messages += 1
    surface.messages.append(dict(message))
    surface.revision += 1
    surface.updated_at = observed_at
    if operation == "deleteSurface":
        surface.state = "deleted"
        deleted_in_batch.add(surface_id)
    elif operation == "createSurface":
        surface.state = "creating"
    else:
        surface.state = "ready"
    return operation, surface_id, surface


def apply_batch(
    surfaces: Mapping[tuple[str, str], A2UISurfaceRecord],
    session_id: str,
    messages: list[Mapping[str, Any]],
    *,
    run_id: str = "",
    message_id: str = "",
    part_id: str = "",
    observed_at: str | None = None,
) -> tuple[
    dict[tuple[str, str], A2UISurfaceRecord],
    list[tuple[str, str, A2UISurfaceRecord]],
]:
    """Validate and atomically fold one ordered batch into surface state."""

    if not messages:
        raise A2UIValidationError("A2UI message batch must not be empty")
    staged = deepcopy(dict(surfaces))
    deleted_in_batch: set[str] = set()
    applied: list[tuple[str, str, A2UISurfaceRecord]] = []
    timestamp = observed_at or utcnow_iso()
    for message in messages:
        result = _apply_staged_message(
            staged,
            session_id,
            message,
            deleted_in_batch=deleted_in_batch,
            run_id=run_id,
            message_id=message_id,
            part_id=part_id,
            observed_at=timestamp,
        )
        applied.append((result[0], result[1], deepcopy(result[2])))
    return staged, applied


def project_a2ui_parts(
    parts: list[Any],
    session_id: str,
) -> tuple[dict[tuple[str, str], A2UISurfaceRecord], list[dict[str, str]]]:
    """Fold persisted A2UI parts and quarantine unknown or invalid records."""

    surfaces: dict[tuple[str, str], A2UISurfaceRecord] = {}
    degradations: list[dict[str, str]] = []
    for raw_part in parts:
        part = raw_part.to_wire() if hasattr(raw_part, "to_wire") else raw_part
        if not isinstance(part, Mapping) or part.get("type") != "a2ui":
            continue
        part_id = str(part.get("id") or "")
        protocol_version = str(part.get("a2ui_protocol_version") or "")
        if protocol_version != A2UI_V091:
            degradations.append(
                {
                    "code": "a2ui_persisted_version_unsupported",
                    "reason": f"A2UI part {part_id or '<unknown>'} uses {protocol_version or '<missing>'}.",
                    "part_id": part_id,
                    "protocol_version": protocol_version,
                }
            )
            continue
        messages = part.get("a2ui_messages")
        if not isinstance(messages, list) or not all(isinstance(row, Mapping) for row in messages):
            degradations.append(
                {
                    "code": "a2ui_persisted_payload_invalid",
                    "reason": f"A2UI part {part_id or '<unknown>'} has no valid message batch.",
                    "part_id": part_id,
                    "protocol_version": protocol_version,
                }
            )
            continue
        raw_metadata = part.get("metadata")
        metadata: Mapping[str, Any] = raw_metadata if isinstance(raw_metadata, Mapping) else {}
        try:
            surfaces, _ = apply_batch(
                surfaces,
                session_id,
                messages,
                run_id=str(metadata.get("run_id") or ""),
                message_id=str(metadata.get("message_id") or ""),
                part_id=part_id,
                observed_at=str(metadata.get("recorded_at") or utcnow_iso()),
            )
        except A2UIValidationError as exc:
            degradations.append(
                {
                    "code": "a2ui_persisted_payload_invalid",
                    "reason": str(exc),
                    "part_id": part_id,
                    "protocol_version": protocol_version,
                }
            )
    return surfaces, degradations


__all__ = [
    "CLIENT_ACTIONS",
    "MAX_A2UI_MESSAGES",
    "SERVER_ACTIONS",
    "A2UISurfaceRecord",
    "A2UIValidationError",
    "apply_batch",
    "project_a2ui_parts",
    "trusted_component_names",
    "validate_client_action",
    "validate_server_message",
]
