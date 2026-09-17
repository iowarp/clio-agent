"""Validated, transcript-projected A2UI surface state for GACT 0.3.

Catalog resolution and per-component/safety validation live in
``gact/a2ui_catalogs/`` (docs/design/a2ui-compat-campaign-2026-09.md S2);
this module owns the message envelope, the surface fold, and the transcript
replay projection, all now consuming a ``CatalogResolver`` instead of
trusting one hard-coded catalog id.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Mapping

from clio_schemas import A2UIClientMessage
from pydantic import ValidationError as _PydanticValidationError

from clio_agent.gact.a2ui_catalogs.registry import CatalogEntry, CatalogResolver
from clio_agent.gact.a2ui_catalogs.validation import (
    A2UIValidationError,
    validate_components,
    validate_value,
)
from clio_agent.gact.protocol.constants import A2UI_V091, A2UI_V091_WIRE


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
    (default 16384). Resolved once per validated message and threaded through
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


class A2UITranscriptFrozenError(A2UIValidationError):
    """Raised when a settled transcript can no longer accept an A2UI part.

    Distinct from a payload rejection: the batch was valid, but the ledger it
    would land in is closed, so the producer is told ``transcript_frozen``
    rather than being handed a validation message it cannot act on.
    """


class A2UICatalogUnknownError(A2UIValidationError):
    """Raised when a ``catalogId`` does not resolve in the installed registry.

    Carries ``catalog_id`` so a caller can attach the typed
    ``a2ui_catalog_unknown`` reason (production) — or, on transcript replay
    (``project_a2ui_parts``), fold the surface to ``state="unknown"`` with
    ``a2ui_catalog_unavailable`` instead of quarantining it.
    """

    def __init__(self, catalog_id: str) -> None:
        self.catalog_id = catalog_id
        super().__init__(f"A2UI catalog is not known: {catalog_id or '<empty>'}")


class A2UICatalogNotProducibleError(A2UIValidationError):
    """Raised when a catalog is installed but not producible in this session."""

    def __init__(self, catalog_id: str) -> None:
        self.catalog_id = catalog_id
        super().__init__(f"A2UI catalog is not producible in this session: {catalog_id}")


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


def validate_server_message(
    message: Mapping[str, Any],
    *,
    catalogs: CatalogResolver,
    catalog_entry: CatalogEntry | None = None,
    producible: "frozenset[str] | None" = None,
) -> tuple[str, str]:
    """Validate an official server-to-client message and return operation/id.

    Args:
        message: One raw server-to-client envelope.
        catalogs: Resolver over every installed catalog (builtin ∪ packs).
        catalog_entry: The surface's already-resolved catalog, for
            ``updateComponents``/``updateDataModel``/``deleteSurface`` on an
            EXISTING surface (a surface's catalog is fixed for its
            lifetime — the caller looks it up from the surface record).
            Ignored for ``createSurface``, which resolves its own catalog
            from the message's ``catalogId``.
        producible: When given, gates ``createSurface.catalogId`` to this
            set (production doors only — replay passes ``None``).

    Raises:
        A2UICatalogUnknownError: If a referenced catalogId is not installed.
        A2UICatalogNotProducibleError: If ``producible`` is given and the
            catalog is installed but not in it.
        A2UIValidationError: On any other structural or safety violation.
    """

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
        catalog_id = str(payload.get("catalogId") or "")
        resolved = catalogs.get(catalog_id, A2UI_V091)
        if resolved is None:
            raise A2UICatalogUnknownError(catalog_id)
        if producible is not None and catalog_id not in producible:
            raise A2UICatalogNotProducibleError(catalog_id)
        catalog_entry = resolved
    if operation == "updateComponents":
        if catalog_entry is None:
            raise A2UIValidationError("A2UI updateComponents requires a resolved surface catalog")
        validate_components(
            catalog_entry, payload.get("components"), max_components=MAX_A2UI_COMPONENTS
        )
    if operation == "updateDataModel":
        path = payload.get("path")
        if not isinstance(path, str) or not path.startswith("/"):
            raise A2UIValidationError("A2UI updateDataModel path must be a JSON Pointer")
    validate_value(
        payload,
        entry=catalog_entry,
        max_depth=MAX_A2UI_DEPTH,
        max_string=max_a2ui_string_chars(),
    )
    return operation, surface_id


def validate_client_action(
    message: Mapping[str, Any],
    *,
    surface_id: str,
    catalog_entry: CatalogEntry | None = None,
) -> dict[str, Any]:
    """Validate the official 0.9.1 client action envelope.

    Args:
        message: The raw client-to-server envelope.
        surface_id: The route's surface id; the action must target it.
        catalog_entry: The surface's resolved catalog, used to look up the
            action's sidecar-declared destination (defaults to ``"agent"``
            when the catalog carries no explicit route for this name, or when
            no entry is supplied). Stored on the returned action as
            ``destination`` for the dispatcher to consume (S5).
    """

    try:
        parsed = A2UIClientMessage.model_validate(message)
    except _PydanticValidationError as exc:
        raise A2UIValidationError(
            f"A2UI client message must be a {A2UI_V091_WIRE} action: {exc.errors()}"
        ) from exc
    if parsed.action is None:
        raise A2UIValidationError("A2UI client message must carry an action, not an error report")
    action = parsed.action.model_dump(mode="json")
    if action.get("surfaceId") != surface_id:
        raise A2UIValidationError("A2UI action surface does not match the route")
    validate_value(
        action,
        entry=catalog_entry,
        max_depth=MAX_A2UI_DEPTH,
        max_string=max_a2ui_string_chars(),
    )
    destination = "agent"
    if catalog_entry is not None:
        route = catalog_entry.sidecar.events.get(str(action.get("name") or ""))
        if route is not None:
            destination = route.destination
    action["destination"] = destination
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
    catalogs: CatalogResolver,
    producible: "frozenset[str] | None",
    deleted_in_batch: set[str],
    run_id: str,
    message_id: str,
    part_id: str,
    observed_at: str,
) -> tuple[str, str, A2UISurfaceRecord]:
    """Apply one validated message to an uncommitted projection."""

    _, peek_payload = _message_operation(message)
    peek_surface_id = str(peek_payload.get("surfaceId") or "")
    key = (session_id, peek_surface_id)
    surface = surfaces.get(key)
    catalog_entry: CatalogEntry | None = None
    if surface is not None and surface.state != "deleted":
        catalog_entry = catalogs.get(surface.catalog_id, A2UI_V091)
        if catalog_entry is None:
            raise A2UICatalogUnknownError(surface.catalog_id)
    operation, surface_id = validate_server_message(
        message, catalogs=catalogs, catalog_entry=catalog_entry, producible=producible
    )
    if surface_id in deleted_in_batch:
        raise A2UIValidationError("A2UI deleteSurface is terminal within a message batch")
    if operation == "createSurface":
        if surface is not None and surface.state != "deleted":
            raise A2UIValidationError("A2UI surface already exists")
        surface = A2UISurfaceRecord(
            id=surface_id,
            session_id=session_id,
            catalog_id=str(peek_payload.get("catalogId") or ""),
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
    catalogs: CatalogResolver,
    producible: "frozenset[str] | None" = None,
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
        catalogs: Resolver over every installed catalog.
        producible: Session-producible catalog ids, or ``None`` to skip the
            producibility gate (replay).
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
            catalogs=catalogs,
            producible=producible,
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
    catalogs: CatalogResolver,
    producible: "frozenset[str] | None" = None,
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
        catalogs=catalogs,
        producible=producible,
        run_id=run_id,
        message_id=message_id,
        part_id=part_id,
        observed_at=observed_at or utcnow_iso(),
        capture=True,
    )
    return staged, applied


def _unknown_catalog_stub(
    key: tuple[str, str],
    existing: A2UISurfaceRecord | None,
    *,
    catalog_id: str,
    messages: list[Mapping[str, Any]],
    part_id: str,
    observed_at: str,
) -> A2UISurfaceRecord:
    """Build the ``state="unknown"`` stand-in for a surface whose catalog vanished.

    Never quarantined, never dropped (docs/design/a2ui-compat-campaign-2026-09.md
    S2): the raw messages this part carried are appended so a future reinstall
    of the catalog could, in principle, recover the surface.
    """

    _, surface_id = key
    if existing is None:
        return A2UISurfaceRecord(
            id=surface_id,
            session_id=key[0],
            catalog_id=catalog_id,
            state="unknown",
            messages=[dict(m) for m in messages],
            part_id=part_id,
            created_at=observed_at,
            updated_at=observed_at,
        )
    return replace(
        existing,
        state="unknown",
        messages=[*existing.messages, *(dict(m) for m in messages)],
        updated_at=observed_at,
    )


def project_a2ui_parts(
    parts: list[Any],
    session_id: str,
    *,
    catalogs: CatalogResolver,
) -> tuple[dict[tuple[str, str], A2UISurfaceRecord], list[dict[str, str]]]:
    """Fold persisted A2UI parts and quarantine unknown or invalid records.

    A part whose surface's catalog is no longer installed is a special case:
    it folds to ``state="unknown"`` with a typed ``a2ui_catalog_unavailable``
    degradation instead of being quarantined — the surface (and its raw
    messages) is never dropped, only marked unrenderable until the catalog
    reappears.
    """

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
        recorded_at = str(metadata.get("recorded_at") or utcnow_iso())
        # Fold in place and keep only the records this batch can touch, so one
        # read stays linear in the transcript instead of copying every surface
        # once per part. A rejected batch is rolled back to that snapshot.
        touched = _batch_surface_keys(session_id, messages)
        snapshot = {
            key: (_copy_record(surfaces[key]) if key in surfaces else None) for key in touched
        }
        try:
            _fold_batch(
                surfaces,
                session_id,
                messages,
                catalogs=catalogs,
                producible=None,
                run_id=str(metadata.get("run_id") or ""),
                message_id=str(metadata.get("message_id") or ""),
                part_id=part_id,
                observed_at=recorded_at,
                capture=False,
            )
        except A2UICatalogUnknownError as exc:
            for key, record in snapshot.items():
                if record is None:
                    surfaces.pop(key, None)
                else:
                    surfaces[key] = record
            for key in touched:
                surfaces[key] = _unknown_catalog_stub(
                    key,
                    surfaces.get(key),
                    catalog_id=exc.catalog_id,
                    messages=messages,
                    part_id=part_id,
                    observed_at=recorded_at,
                )
            degradations.append(
                {
                    "code": "a2ui_catalog_unavailable",
                    "reason": str(exc),
                    "part_id": part_id,
                    "protocol_version": protocol_version,
                }
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
    "A2UICatalogNotProducibleError",
    "A2UICatalogUnknownError",
    "A2UISurfaceRecord",
    "A2UITranscriptFrozenError",
    "A2UIValidationError",
    "apply_batch",
    "max_a2ui_message_bytes",
    "max_a2ui_messages",
    "max_a2ui_string_chars",
    "project_a2ui_parts",
    "validate_client_action",
    "validate_server_message",
]
