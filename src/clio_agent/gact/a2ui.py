"""Persistent, bounded A2UI 0.9.1 surface state for GACT 0.3."""

from __future__ import annotations

import json
import math
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from clio_agent.gact.events import Event, EventBus
from clio_agent.gact.protocol_v3 import A2UI_V091, CLIO_A2UI_CATALOG_ID, utcnow_iso

MAX_A2UI_MESSAGE_BYTES = 256 * 1024
MAX_A2UI_COMPONENTS = 256
MAX_A2UI_MESSAGES = 512
MAX_A2UI_DEPTH = 20
MAX_A2UI_STRING = 16 * 1024

SERVER_ACTIONS = frozenset(
    {"agent.submit", "approval.respond", "form.submit", "run.retry", "run.cancel"}
)
CLIENT_ACTIONS = frozenset({"artifact.open", "data.select", "workflow.focus"})

_COMPONENT_PROPS: dict[str, frozenset[str]] = {
    "Text": frozenset({"text", "variant", "accessibility", "weight"}),
    "Icon": frozenset({"name", "accessibility", "weight"}),
    "Image": frozenset({"url", "description", "fit", "variant", "accessibility", "weight"}),
    "Row": frozenset({"children", "justify", "align", "accessibility", "weight"}),
    "Column": frozenset({"children", "justify", "align", "accessibility", "weight"}),
    "Grid": frozenset({"children", "columns", "gap", "accessibility", "weight"}),
    "List": frozenset({"children", "direction", "align", "listStyle", "accessibility", "weight"}),
    "Frame": frozenset({"child", "title", "description", "accessibility", "weight"}),
    "Tabs": frozenset({"tabs", "accessibility", "weight"}),
    "Modal": frozenset({"trigger", "content", "accessibility", "weight"}),
    "Divider": frozenset({"axis", "accessibility", "weight"}),
    "Button": frozenset(
        {
            "child",
            "variant",
            "action",
            "checks",
            "isValid",
            "validationErrors",
            "accessibility",
            "weight",
        }
    ),
    "CheckBox": frozenset(
        {"label", "value", "checks", "isValid", "validationErrors", "accessibility", "weight"}
    ),
    "TextField": frozenset(
        {
            "label",
            "value",
            "variant",
            "validationRegexp",
            "checks",
            "isValid",
            "validationErrors",
            "accessibility",
            "weight",
        }
    ),
    "ChoicePicker": frozenset(
        {
            "label",
            "variant",
            "options",
            "value",
            "displayStyle",
            "filterable",
            "checks",
            "isValid",
            "validationErrors",
            "accessibility",
            "weight",
        }
    ),
    "Slider": frozenset(
        {
            "label",
            "min",
            "max",
            "value",
            "checks",
            "isValid",
            "validationErrors",
            "accessibility",
            "weight",
        }
    ),
    "clio.status.v1": frozenset(
        {"label", "state", "detail", "elapsedMs", "accessibility", "weight"}
    ),
    "clio.metric.v1": frozenset(
        {"label", "value", "unit", "trend", "detail", "accessibility", "weight"}
    ),
    "clio.progress.v1": frozenset(
        {"label", "value", "max", "state", "detail", "accessibility", "weight"}
    ),
    "clio.callout.v1": frozenset(
        {"title", "body", "severity", "action", "accessibility", "weight"}
    ),
    "clio.data-table.v1": frozenset(
        {"columns", "rows", "selection", "action", "accessibility", "weight"}
    ),
    "clio.time-series.v1": frozenset(
        {"series", "xKey", "yKeys", "title", "accessibility", "weight"}
    ),
    "clio.mermaid.v1": frozenset({"source", "title", "accessibility", "weight"}),
    "clio.map.v1": frozenset(
        {
            "title",
            "points",
            "selected",
            "action",
            "actionLabel",
            "accessibility",
            "weight",
        }
    ),
    "clio.workflow.v1": frozenset(
        {"nodes", "edges", "selected", "action", "accessibility", "weight"}
    ),
    "clio.artifact.v1": frozenset(
        {"name", "uri", "mediaType", "size", "action", "accessibility", "weight"}
    ),
    "clio.code.v1": frozenset({"code", "language", "title", "accessibility", "weight"}),
    "clio.diff.v1": frozenset({"path", "diff", "status", "action", "accessibility", "weight"}),
    "clio.action-card.v1": frozenset(
        {"title", "body", "severity", "actions", "accessibility", "weight"}
    ),
    "clio.approval.v1": frozenset(
        {"title", "reason", "risk", "actions", "accessibility", "weight"}
    ),
}

# Keep the server acceptance boundary aligned with the renderer's required
# fields.  Allow-listing property names alone can otherwise persist a surface as
# ``ready`` even though the browser must reject it during schema validation.
_COMPONENT_REQUIRED_PROPS: dict[str, frozenset[str]] = {
    "Text": frozenset({"text"}),
    "Icon": frozenset({"name"}),
    "Image": frozenset({"url"}),
    "Row": frozenset({"children"}),
    "Column": frozenset({"children"}),
    "Grid": frozenset({"children", "columns"}),
    "List": frozenset({"children"}),
    "Frame": frozenset({"child"}),
    "Tabs": frozenset({"tabs"}),
    "Modal": frozenset({"trigger", "content"}),
    "Divider": frozenset(),
    "Button": frozenset({"child"}),
    "CheckBox": frozenset({"label", "value"}),
    "TextField": frozenset({"label", "value"}),
    "ChoicePicker": frozenset({"label", "options", "value"}),
    "Slider": frozenset({"label", "min", "max", "value"}),
    "clio.status.v1": frozenset({"label", "state"}),
    "clio.metric.v1": frozenset({"label", "value"}),
    "clio.progress.v1": frozenset({"label"}),
    "clio.callout.v1": frozenset({"title", "body", "severity"}),
    "clio.data-table.v1": frozenset({"columns", "rows"}),
    "clio.time-series.v1": frozenset({"series", "xKey", "yKeys"}),
    "clio.mermaid.v1": frozenset({"source"}),
    "clio.map.v1": frozenset({"points"}),
    "clio.workflow.v1": frozenset({"nodes", "edges"}),
    "clio.artifact.v1": frozenset({"name", "uri", "mediaType"}),
    "clio.code.v1": frozenset({"code", "language"}),
    "clio.diff.v1": frozenset({"path", "diff"}),
    "clio.action-card.v1": frozenset({"title", "body", "severity", "actions"}),
    "clio.approval.v1": frozenset({"title", "reason", "risk", "actions"}),
}

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
    created_at: str = field(default_factory=utcnow_iso)
    updated_at: str = field(default_factory=utcnow_iso)

    def to_wire(self) -> dict[str, Any]:
        """Return the normalized frontend surface representation."""

        row = asdict(self)
        return {key: value for key, value in row.items() if value != "" and value is not None}


def _message_operation(message: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
    if message.get("version") != "v0.9.1":
        raise A2UIValidationError("A2UI message version must be v0.9.1")
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
        if key.lower() in {"url", "uri"}:
            _validate_url(value)
        return
    if isinstance(value, list):
        for item in value:
            _validate_value(item, depth=depth + 1)
        return
    if not isinstance(value, Mapping):
        return
    for child_key, child_value in value.items():
        if not isinstance(child_key, str):
            raise A2UIValidationError("A2UI object keys must be strings")
        if child_key in _FORBIDDEN_KEYS:
            raise A2UIValidationError(f"A2UI property is prohibited: {child_key}")
        if child_key == "functionCall" or child_key == "call":
            raise A2UIValidationError("A2UI client function calls are not trusted")
        if child_key == "action":
            _validate_action(child_value)
        if child_key == "path" and isinstance(child_value, str) and not child_value.startswith("/"):
            raise A2UIValidationError("A2UI data bindings must use JSON Pointer paths")
        if child_key == "event" and isinstance(child_value, Mapping):
            action = str(child_value.get("name") or "")
            if action not in SERVER_ACTIONS | CLIENT_ACTIONS:
                raise A2UIValidationError(f"A2UI action is not registered: {action}")
        _validate_value(child_value, key=child_key, depth=depth + 1)


def _validate_map_component(component: Mapping[str, Any]) -> None:
    """Apply the same bounded geospatial contract enforced by the renderer."""

    points = component.get("points")
    if not isinstance(points, list) or not 1 <= len(points) <= 500:
        raise A2UIValidationError("A2UI map points must be a non-empty list bounded to 500")
    allowed = {"id", "label", "latitude", "longitude", "detail", "category"}
    for point in points:
        if not isinstance(point, Mapping) or set(point) - allowed:
            raise A2UIValidationError("A2UI map point contains unknown properties")
        point_id = point.get("id")
        label = point.get("label")
        if not isinstance(point_id, str) or not 1 <= len(point_id) <= 128:
            raise A2UIValidationError("A2UI map point id is required and bounded")
        if not isinstance(label, str) or not 1 <= len(label) <= 240:
            raise A2UIValidationError("A2UI map point label is required and bounded")
        for key, lower, upper in (
            ("latitude", -90.0, 90.0),
            ("longitude", -180.0, 180.0),
        ):
            value = point.get(key)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not lower <= float(value) <= upper
            ):
                raise A2UIValidationError(f"A2UI map point {key} is outside its valid range")
        detail = point.get("detail")
        category = point.get("category")
        if detail is not None and (not isinstance(detail, str) or len(detail) > 2_000):
            raise A2UIValidationError("A2UI map point detail exceeds the size limit")
        if category is not None and (not isinstance(category, str) or len(category) > 120):
            raise A2UIValidationError("A2UI map point category exceeds the size limit")
    for key, limit in (("title", 240), ("selected", 128), ("actionLabel", 240)):
        value = component.get(key)
        if value is not None and (not isinstance(value, (str, Mapping)) or len(value) > limit):
            raise A2UIValidationError(f"A2UI map {key} is invalid or exceeds the size limit")


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
            allowed_props = _COMPONENT_PROPS.get(component_name)
            if allowed_props is None:
                raise A2UIValidationError(f"A2UI component is not trusted: {component_name}")
            unknown_props = set(component) - {"id", "component"} - allowed_props
            if unknown_props:
                raise A2UIValidationError(
                    f"A2UI {component_name} contains unknown properties: {sorted(unknown_props)}"
                )
            missing_props = _COMPONENT_REQUIRED_PROPS[component_name] - set(component)
            if missing_props:
                raise A2UIValidationError(
                    f"A2UI {component_name} is missing required properties: {sorted(missing_props)}"
                )
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
            if component_name == "clio.map.v1":
                _validate_map_component(component)
    _validate_value(payload)
    return operation, surface_id


def validate_client_action(message: Mapping[str, Any], *, surface_id: str) -> dict[str, Any]:
    """Validate the official 0.9.1 client action envelope."""

    if message.get("version") != "v0.9.1" or set(message) != {"version", "action"}:
        raise A2UIValidationError("A2UI client message must be a v0.9.1 action")
    action = message.get("action")
    if not isinstance(action, Mapping):
        raise A2UIValidationError("A2UI action payload must be an object")
    expected = {"name", "surfaceId", "sourceComponentId", "timestamp", "context"}
    if set(action) != expected:
        raise A2UIValidationError("A2UI action contains missing or unknown properties")
    name = str(action.get("name") or "")
    if name not in SERVER_ACTIONS:
        raise A2UIValidationError(f"A2UI server action is not registered: {name}")
    if action.get("surfaceId") != surface_id:
        raise A2UIValidationError("A2UI action surface does not match the route")
    if not str(action.get("sourceComponentId") or ""):
        raise A2UIValidationError("A2UI action sourceComponentId is required")
    try:
        datetime.fromisoformat(str(action.get("timestamp") or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise A2UIValidationError("A2UI action timestamp must be ISO 8601") from exc
    if not isinstance(action.get("context"), Mapping):
        raise A2UIValidationError("A2UI action context must be an object")
    _validate_value(action)
    return dict(action)


class A2UIStore:
    """Thread-safe persistent surface ledger with event publication."""

    def __init__(self, *, path: Path, bus: EventBus) -> None:
        self._path = path
        self._bus = bus
        self._lock = threading.Lock()
        self._surfaces: dict[tuple[str, str], A2UISurfaceRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for raw in data.get("surfaces", []):
            try:
                surface = A2UISurfaceRecord(**raw)
            except (TypeError, ValueError):
                continue
            self._surfaces[(surface.session_id, surface.id)] = surface

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"surfaces": [asdict(row) for row in self._surfaces.values()]}, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self._path)

    def get(self, session_id: str, surface_id: str) -> A2UISurfaceRecord | None:
        """Return a session-scoped surface if present."""

        with self._lock:
            return self._surfaces.get((session_id, surface_id))

    def list_wire(self, session_id: str) -> list[dict[str, Any]]:
        """Return surfaces for a session ordered by creation time."""

        with self._lock:
            rows = [row for row in self._surfaces.values() if row.session_id == session_id]
            rows.sort(key=lambda row: row.created_at)
            return [row.to_wire() for row in rows]

    def apply(
        self,
        session_id: str,
        message: Mapping[str, Any],
        *,
        run_id: str = "",
        message_id: str = "",
        part_id: str = "",
    ) -> A2UISurfaceRecord:
        """Validate, persist, and publish one ordered server message."""

        operation, surface_id = validate_server_message(message)
        with self._lock:
            key = (session_id, surface_id)
            surface = self._surfaces.get(key)
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
                )
                self._surfaces[key] = surface
            elif surface is None:
                raise A2UIValidationError("A2UI surface does not exist in this session")
            if operation == "updateComponents":
                # updateComponents carries the complete authoritative component
                # set for a surface revision. Retaining superseded definitions
                # makes one historical renderer-invalid revision poison every
                # later valid update during replay, and needlessly grows the
                # durable snapshot. Keep the lifecycle and data-model messages,
                # but compact component state to the newest complete revision.
                surface.messages = [
                    existing for existing in surface.messages if "updateComponents" not in existing
                ]
            if len(surface.messages) >= MAX_A2UI_MESSAGES:
                surface.messages = surface.messages[-(MAX_A2UI_MESSAGES - 1) :]
            surface.messages.append(dict(message))
            surface.revision += 1
            surface.updated_at = utcnow_iso()
            if operation == "deleteSurface":
                surface.state = "deleted"
            elif operation == "createSurface":
                surface.state = "creating"
            else:
                surface.state = "ready"
            self._flush()
            wire = surface.to_wire()
        event_type = (
            "a2ui.surface.deleted" if operation == "deleteSurface" else "a2ui.surface.upserted"
        )
        payload = {"surface_id": surface_id} if operation == "deleteSurface" else wire
        self._bus.publish(Event(type=event_type, session_id=session_id, payload=payload))
        return surface
