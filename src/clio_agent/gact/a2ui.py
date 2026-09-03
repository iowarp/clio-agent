"""Validated, transcript-projected A2UI surface state for GACT 0.3."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, replace
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


MAX_A2UI_COMPONENTS = 256
MAX_A2UI_DEPTH = 20


def max_a2ui_message_bytes() -> int:
    """Byte ceiling for one encoded A2UI server-to-client message.

    Config: ``a2ui.max_message_bytes`` / ``CLIO_A2UI_MAX_MESSAGE_BYTES``
    (default 262144). Raise it only for a deployment whose trusted producers
    legitimately emit larger single surface updates.
    """

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "a2ui.max_message_bytes",
        env="CLIO_A2UI_MAX_MESSAGE_BYTES",
        default=256 * 1024,
        cast=conf.as_int,
    )


def max_a2ui_string_chars() -> int:
    """Character ceiling for any single string inside an A2UI payload.

    Config: ``a2ui.max_string_chars`` / ``CLIO_A2UI_MAX_STRING_CHARS``
    (default 16384). Resolved once per validated component and threaded through
    the recursive walk, never re-resolved per node.
    """

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "a2ui.max_string_chars",
        env="CLIO_A2UI_MAX_STRING_CHARS",
        default=16 * 1024,
        cast=conf.as_int,
    )


def max_a2ui_messages() -> int:
    """Retention bound for the ordered message log of one A2UI surface.

    Config: ``gact.ledger_retention.a2ui_messages.max`` /
    ``CLIO_LEDGER_A2UI_MESSAGES_MAX`` (default 512). Reaching it evicts the
    oldest non-``createSurface`` message with a typed ``a2ui_message_limit``
    reason, so replay never loses the surface's constructor.
    """

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "gact.ledger_retention.a2ui_messages.max",
        env="CLIO_LEDGER_A2UI_MESSAGES_MAX",
        default=512,
        cast=conf.as_int,
    )


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


class A2UITranscriptFrozenError(A2UIValidationError):
    """Raised when a settled transcript can no longer accept an A2UI part.

    Distinct from a payload rejection: the batch was valid, but the ledger it
    would land in is closed, so the producer is told ``transcript_frozen``
    rather than being handed a validation message it cannot act on.
    """


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


_URL_KEYS = frozenset({"url", "uri", "datauri"})


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


def _validate_value(
    value: Any,
    *,
    key: str = "",
    depth: int = 0,
    max_string: int | None = None,
    free_form: bool = False,
) -> None:
    """Walk one A2UI value, enforcing the catalog's structural + safety rules.

    ``free_form`` marks a subtree that is an action/event ``context`` — arbitrary
    producer/client DATA, not renderable structure. The SAFETY rules (forbidden
    presentation keys, function calls, url literals, string/nesting bounds) still
    apply there; only the STRUCTURAL Action-envelope rules are lifted. Without
    that distinction ``approval.respond`` was unroutable: its own required
    ``context.action`` ("allow") was read as a malformed Action envelope, so every
    approval action 422'd on both the ``/a2ui/actions`` route and the normalized
    interaction responder.
    """

    if max_string is None:
        # Resolved ONCE at the top of the walk and threaded down: the recursion
        # visits every string node, which is no place for a config lookup.
        max_string = max_a2ui_string_chars()
    if depth > MAX_A2UI_DEPTH:
        raise A2UIValidationError("A2UI value exceeds the nesting limit")
    if isinstance(value, str):
        if len(value) > max_string:
            raise A2UIValidationError("A2UI string exceeds the size limit")
        if key.lower() in _URL_KEYS:
            _validate_url(value)
        return
    if isinstance(value, list):
        for item in value:
            _validate_value(
                item, key=key, depth=depth + 1, max_string=max_string, free_form=free_form
            )
        return
    if not isinstance(value, Mapping):
        return
    if set(value) == {"path"}:
        binding_path = value.get("path")
        if not isinstance(binding_path, str) or not binding_path.startswith("/"):
            raise A2UIValidationError("A2UI data bindings must use JSON Pointer paths")
    # Both envelope shapes that carry a free-form payload — the ``event`` inside a
    # component's action and the client action message itself — pair ``name`` with
    # ``context``. Everything under that ``context`` is producer/client data.
    carries_action_context = "name" in value and "context" in value
    for child_key, child_value in value.items():
        if not isinstance(child_key, str):
            raise A2UIValidationError("A2UI object keys must be strings")
        if child_key in _FORBIDDEN_KEYS:
            raise A2UIValidationError(f"A2UI property is prohibited: {child_key}")
        if (
            child_key.lower() in _URL_KEYS
            and child_value is not None
            and not isinstance(child_value, str)
        ):
            # A data binding or function call resolves in the renderer, after
            # this boundary ran, so the scheme allowlist would never see the URL
            # it ends up fetching. Require the literal instead.
            raise A2UIValidationError(
                f"A2UI {child_key} must be a literal string so its scheme can be checked"
            )
        if child_key == "functionCall" or child_key == "call":
            raise A2UIValidationError("A2UI client function calls are not trusted")
        if child_key == "action" and not free_form:
            _validate_action(child_value)
        if child_key == "event" and not free_form and isinstance(child_value, Mapping):
            action = str(child_value.get("name") or "")
            if action not in SERVER_ACTIONS | CLIENT_ACTIONS:
                raise A2UIValidationError(f"A2UI action is not registered: {action}")
        _validate_value(
            child_value,
            key=child_key,
            depth=depth + 1,
            max_string=max_string,
            free_form=free_form or (carries_action_context and child_key == "context"),
        )


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
    if len(encoded) > max_a2ui_message_bytes():
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
            _validate_accessibility(component, component_name)
            _validate_value(component)
            try:
                A2UIComponent.model_validate(component)
            except ValidationError as exc:
                raise A2UIValidationError(_component_validation_error(component_name, exc)) from exc
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


def _component_ids(message: Mapping[str, Any]) -> set[str]:
    """Return the component ids an ``updateComponents`` message declares.

    Args:
        message: One validated server message.

    Returns:
        The declared component ids, or an empty set for any other operation.
    """

    payload = message.get("updateComponents")
    if not isinstance(payload, Mapping):
        return set()
    components = payload.get("components")
    if not isinstance(components, list):
        return set()
    return {
        str(component.get("id") or "") for component in components if isinstance(component, Mapping)
    }


def _copy_record(record: A2UISurfaceRecord) -> A2UISurfaceRecord:
    """Return an independently foldable copy of one surface record.

    The fold only ever rebinds scalars and appends/removes whole message dicts,
    never mutates a stored message in place, so copying the message *list* is
    enough. A deep copy would duplicate every persisted byte of every surface on
    every projection read, which is what made the fold quadratic.

    Args:
        record: The surface record to copy.

    Returns:
        A copy whose message list can be folded without touching ``record``.
    """

    return replace(record, messages=list(record.messages))


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
        # Compaction is only lossless for the components this message redefines:
        # an incremental upsert must not erase sibling definitions the client
        # still needs, or replay would render a surface the live view never had.
        superseded = _component_ids(message)
        surface.messages = [
            existing
            for existing in surface.messages
            if "updateComponents" not in existing or not _component_ids(existing) <= superseded
        ]
    if len(surface.messages) >= max_a2ui_messages():
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


def _batch_surface_keys(session_id: str, messages: list[Mapping[str, Any]]) -> set[tuple[str, str]]:
    """Return the surface keys a batch can touch, before it is validated.

    Args:
        session_id: Session the batch belongs to.
        messages: The raw ordered batch.

    Returns:
        Every ``(session_id, surface_id)`` key the fold could create or mutate.
    """

    keys: set[tuple[str, str]] = set()
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        for operation in ("createSurface", "updateComponents", "updateDataModel", "deleteSurface"):
            payload = message.get(operation)
            if isinstance(payload, Mapping):
                keys.add((session_id, str(payload.get("surfaceId") or "")))
    return keys


def _fold_batch(
    surfaces: dict[tuple[str, str], A2UISurfaceRecord],
    session_id: str,
    messages: list[Mapping[str, Any]],
    *,
    run_id: str,
    message_id: str,
    part_id: str,
    observed_at: str,
    capture: bool,
) -> list[tuple[str, str, A2UISurfaceRecord]]:
    """Fold one ordered batch into ``surfaces`` in place.

    Args:
        surfaces: Working projection, mutated as the batch applies.
        session_id: Session the batch belongs to.
        messages: The ordered batch.
        run_id: Correlated run id recorded on a created surface.
        message_id: Correlated message id recorded on a created surface.
        part_id: Transcript part id recorded on a created surface.
        observed_at: Timestamp stamped on every record this batch touches.
        capture: Snapshot each applied record (publication needs the per-message
            state); readers that discard the result pass ``False``.

    Returns:
        One ``(operation, surface_id, record)`` row per applied message.

    Raises:
        A2UIValidationError: If any message is rejected; ``surfaces`` is then
            partially folded and the caller owns the rollback.
    """

    deleted_in_batch: set[str] = set()
    applied: list[tuple[str, str, A2UISurfaceRecord]] = []
    for message in messages:
        operation, surface_id, record = _apply_staged_message(
            surfaces,
            session_id,
            message,
            deleted_in_batch=deleted_in_batch,
            run_id=run_id,
            message_id=message_id,
            part_id=part_id,
            observed_at=observed_at,
        )
        applied.append((operation, surface_id, _copy_record(record) if capture else record))
    return applied


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
    staged = {key: _copy_record(record) for key, record in surfaces.items()}
    applied = _fold_batch(
        staged,
        session_id,
        messages,
        run_id=run_id,
        message_id=message_id,
        part_id=part_id,
        observed_at=observed_at or utcnow_iso(),
        capture=True,
    )
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
        # Fold in place and keep only the records this batch can touch, so one
        # read stays linear in the transcript instead of copying every surface
        # once per part. A rejected batch is rolled back to that snapshot.
        snapshot = {
            key: (_copy_record(surfaces[key]) if key in surfaces else None)
            for key in _batch_surface_keys(session_id, messages)
        }
        try:
            _fold_batch(
                surfaces,
                session_id,
                messages,
                run_id=str(metadata.get("run_id") or ""),
                message_id=str(metadata.get("message_id") or ""),
                part_id=part_id,
                observed_at=str(metadata.get("recorded_at") or utcnow_iso()),
                capture=False,
            )
        except A2UIValidationError as exc:
            for key, record in snapshot.items():
                if record is None:
                    surfaces.pop(key, None)
                else:
                    surfaces[key] = record
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
    "SERVER_ACTIONS",
    "A2UISurfaceRecord",
    "A2UITranscriptFrozenError",
    "A2UIValidationError",
    "apply_batch",
    "max_a2ui_message_bytes",
    "max_a2ui_messages",
    "max_a2ui_string_chars",
    "project_a2ui_parts",
    "trusted_component_names",
    "validate_client_action",
    "validate_server_message",
]
