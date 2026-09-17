"""The durable A2UI action lifecycle record (S5).

docs/design/a2ui-compat-campaign-2026-09.md S5, owner decision 4: "durable
action record = an ``a2ui_action`` transcript part beside the surface parts.
No new store." This module owns that record's shape, its idempotency key,
its transcript encoding (a :class:`~clio_agent.gact.parts.Part`), and the
FOLD that projects a session's ``a2ui_action`` parts into each
:class:`~clio_agent.gact.a2ui.A2UISurfaceRecord`'s ``actions`` list —
mirroring how ``gact/a2ui.py::project_a2ui_parts`` folds ``a2ui`` parts, kept
separate (no accretion onto that S1/S2 owner module).

A record's lifecycle (``received -> delivered -> consumed`` or
``received -> failed``) is append-only, exactly like a surface's own
revision history: every transition is a NEW ``a2ui_action`` part sharing the
SAME record id, and the fold keeps only the latest snapshot per id (last
write wins, ordered the same causal way ``A2UIStore._parts`` orders ``a2ui``
parts).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal, Mapping
from uuid import uuid4

from fastapi import HTTPException

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.parts import Part

#: Every durable state an ``a2ui_action`` record can be in.
ActionState = Literal["received", "delivered", "consumed", "failed"]

#: Every delivery lane a record can be routed through.
ActionDelivery = Literal[
    "start", "steer", "resolve_question", "permission", "run_cancel", "run_retry", "rejected"
]

#: The transcript part type this module owns (sibling of ``"a2ui"``).
A2UI_ACTION_PART_TYPE = "a2ui_action"


def utcnow_iso() -> str:
    """Return a stable UTC timestamp for lifecycle records."""

    return datetime.now(timezone.utc).isoformat()


def new_record_id() -> str:
    """Mint a fresh action record id (also used as its owning part id)."""

    return f"a2ui_action_{uuid4().hex}"


def compute_idempotency_key(
    surface_id: str, source_component_id: str, timestamp: str, context: Mapping[str, Any]
) -> str:
    """Return ``sha256(surfaceId, sourceComponentId, timestamp, canonical(context))``.

    Two submissions of the SAME client action (a double-click, a retried
    POST) hash identically and are folded into one durable record; two
    genuinely distinct clicks (a fresh ``timestamp``) mint distinct records.
    """

    canonical_context = json.dumps(
        dict(context), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    material = "\x1f".join((surface_id, source_component_id, timestamp, canonical_context))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ActionRecord:
    """One durable, idempotent, correlated A2UI action (or error) record.

    Attributes:
        id: The record's own identity (also the owning transcript part id).
        session_id: The owner session this record belongs to.
        surface_id: The surface the action/error targets.
        catalog_id: The surface's resolved catalog id at record time.
        kind: ``"action"`` for a client action envelope, ``"error"`` for a
            client error report (``VALIDATION_FAILED`` or generic).
        envelope: The client's message VERBATIM (``{version, action}`` or
            ``{version, error}``), never mutated.
        action_name: The action/event name (empty for a generic error whose
            envelope carries no action).
        source_component_id: The originating component id, when known.
        state: The record's current lifecycle state.
        delivery: How the record was (or was refused to be) delivered.
        idempotency_key: Empty for an error record (errors have no natural
            per-click identity to dedupe on).
        correlation: Caller-supplied + surface-derived correlation fields
            (``interaction_id``, ``question_id``, ``message_id``, ``part_id``,
            ``task_id``, ``run_id``, and — error records only — ``revision``,
            the surface revision this report targets).
        client_data_model: The validated, session-owned-filtered
            ``a2uiClientDataModel`` carried with this record, if any.
        reason: A typed reason code when ``state`` is ``failed`` or the
            record was a ``duplicate``/``rejected`` delivery.
        narration: The deterministic, bounded text handed to the agent.
        http_status: The HTTP status code a ``failed`` record's ORIGINAL
            refusal raised, so a later duplicate submission can re-raise the
            SAME typed refusal instead of returning a 200 (adversarial
            review finding #5). ``0`` for every non-failed record.
        created_at: When this record's identity was first minted.
        updated_at: When this SNAPSHOT (this particular transcript part) was
            written.
    """

    id: str
    session_id: str
    surface_id: str
    catalog_id: str = ""
    kind: Literal["action", "error"] = "action"
    envelope: dict[str, Any] = field(default_factory=dict)
    action_name: str = ""
    source_component_id: str = ""
    state: ActionState = "received"
    delivery: "ActionDelivery | Literal['']" = ""
    idempotency_key: str = ""
    correlation: dict[str, Any] = field(default_factory=dict)
    client_data_model: dict[str, Any] | None = None
    reason: str = ""
    narration: str = ""
    http_status: int = 0
    created_at: str = field(default_factory=utcnow_iso)
    updated_at: str = field(default_factory=utcnow_iso)

    def transition(
        self,
        *,
        state: ActionState | None = None,
        delivery: "ActionDelivery | Literal[''] | None" = None,
        reason: str | None = None,
        narration: str | None = None,
        correlation: Mapping[str, Any] | None = None,
        http_status: int | None = None,
    ) -> "ActionRecord":
        """Return a NEW snapshot of this record with the given fields updated.

        The identity (``id``/``session_id``/``surface_id``/``envelope``/
        ``idempotency_key``/``created_at``) never changes across transitions;
        only the lifecycle fields do, stamped with a fresh ``updated_at``.
        ``correlation`` is a shallow MERGE onto the existing mapping (a
        transition only ever adds fields learned during delivery, e.g. the
        minted ``message_id`` or resumed ``question_id``).
        """

        updates: dict[str, Any] = {"updated_at": utcnow_iso()}
        if state is not None:
            updates["state"] = state
        if delivery is not None:
            updates["delivery"] = delivery
        if reason is not None:
            updates["reason"] = reason
        if narration is not None:
            updates["narration"] = narration
        if correlation:
            updates["correlation"] = {**self.correlation, **correlation}
        if http_status is not None:
            updates["http_status"] = http_status
        return replace(self, **updates)

    def to_wire(self) -> dict[str, Any]:
        """Return the plain-dict projection this module persists and folds."""

        return asdict(self)

    @classmethod
    def from_wire(cls, data: Mapping[str, Any]) -> "ActionRecord | None":
        """Reconstruct a record from its persisted wire dict, or ``None`` if malformed."""

        try:
            return cls(
                id=str(data["id"]),
                session_id=str(data["session_id"]),
                surface_id=str(data["surface_id"]),
                catalog_id=str(data.get("catalog_id") or ""),
                kind=data.get("kind") or "action",
                envelope=dict(data.get("envelope") or {}),
                action_name=str(data.get("action_name") or ""),
                source_component_id=str(data.get("source_component_id") or ""),
                state=data.get("state") or "received",
                delivery=data.get("delivery") or "",
                idempotency_key=str(data.get("idempotency_key") or ""),
                correlation=dict(data.get("correlation") or {}),
                client_data_model=(
                    dict(data["client_data_model"])
                    if isinstance(data.get("client_data_model"), Mapping)
                    else None
                ),
                reason=str(data.get("reason") or ""),
                narration=str(data.get("narration") or ""),
                http_status=int(data.get("http_status") or 0),
                created_at=str(data.get("created_at") or utcnow_iso()),
                updated_at=str(data.get("updated_at") or utcnow_iso()),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def to_part(self, *, recorded_at: str = "") -> "Part":
        """Build the transcript part this snapshot persists as."""

        from clio_agent.gact.parts import Part  # noqa: PLC0415
        from clio_agent.gact.protocol.constants import A2UI_V091  # noqa: PLC0415

        stamp = recorded_at or self.updated_at
        return Part(
            id=f"{self.id}_{uuid4().hex[:12]}",
            type=A2UI_ACTION_PART_TYPE,
            surface_id=self.surface_id,
            a2ui_protocol_version=A2UI_V091,
            a2ui_action_record=self.to_wire(),
            metadata={"recorded_at": stamp},
        )


def find_by_idempotency_key(
    actions: "Sequence[Mapping[str, Any]]", idempotency_key: str
) -> dict[str, Any] | None:
    """Return the latest recorded action whose idempotency key matches, if any."""

    if not idempotency_key:
        return None
    for row in actions:
        if row.get("kind") == "action" and row.get("idempotency_key") == idempotency_key:
            return dict(row)
    return None


def prior_error_count_for_revision(actions: "Sequence[Mapping[str, Any]]", revision: int) -> int:
    """Count prior VALIDATION_FAILED repairs that actually DELIVERED at this revision.

    Used to decide "first VALIDATION_FAILED -> one repair delivery" vs.
    "second -> the surface is repair-exhausted" (S5 deliverable 2b).
    Adversarial review finding #4: only a record that reached
    ``delivered``/``consumed`` burns the repair budget -- a repair REFUSED
    (e.g. ``a2ui_waiting_user_uncorrelated``, still ``state="failed"``) never
    attempted delivery and must not count, or a transient refusal would
    permanently exhaust a surface's repair budget. A generic
    (non-VALIDATION_FAILED) error never counts either -- it is never
    delivered/repaired in the first place.
    """

    return sum(
        1
        for row in actions
        if row.get("kind") == "error"
        and row.get("reason") != "a2ui_client_error_unhandled"
        and row.get("state") in ("delivered", "consumed")
        and row.get("correlation", {}).get("revision") == revision
    )


def lifecycle_event_payload(record: ActionRecord) -> dict[str, Any]:
    """Build the ``a2ui.action.*`` event payload contract (issue #1372 comment).

    Published on every lifecycle transition, scoped like the pre-S5
    ``a2ui.action.received`` event. ``action`` duplicates ``action_name`` for
    the pre-S5 consumer; unknown extra keys are ignored client-side and
    missing optional keys never produce a stream gap.
    """

    payload: dict[str, Any] = {
        "surface_id": record.surface_id,
        "action_name": record.action_name,
        "action": record.action_name,
        "action_id": record.id,
        "state": record.state,
        "kind": record.kind,
    }
    if record.source_component_id:
        payload["source_component_id"] = record.source_component_id
    if record.delivery:
        payload["delivery"] = record.delivery
    if record.reason:
        payload["reason"] = record.reason
    return payload


def last_action_wire(record: Mapping[str, Any]) -> dict[str, Any]:
    """Project a persisted action record into the legacy ``/lastAction`` wire shape.

    ``routes/interactions.py``'s ``payload.last_action`` is a WIRE CONTRACT
    the client reads: pre-S5, it was the ``/lastAction`` data-model value
    ``dispatch_action`` wrote -- ``{name, status, receivedAt, context}``.
    That write is deleted (S5), but the SHAPE is not -- every field the old
    value exposed is kept byte-compatible here; ``action_id``/``state``/
    ``delivery`` are S5 additions, never a replacement for the originals.
    ``status`` is the literal ``"accepted"`` for every record that reached
    persistence, matching the old ack (stamped once the envelope validated,
    independent of delivery outcome -- the old code never wrote ``/lastAction``
    for a rejected action at all).
    """

    envelope = record.get("envelope")
    action = envelope.get("action") if isinstance(envelope, Mapping) else None
    context = action.get("context") if isinstance(action, Mapping) else None
    return {
        "name": str(record.get("action_name") or ""),
        "status": "accepted",
        "receivedAt": str(record.get("created_at") or ""),
        "context": dict(context) if isinstance(context, Mapping) else {},
        "action_id": str(record.get("id") or ""),
        "state": str(record.get("state") or ""),
        "delivery": str(record.get("delivery") or ""),
    }


def persist_new_record(app: "FastAPI", record: ActionRecord) -> dict[str, Any] | None:
    """Persist a NEW record's ``received`` snapshot, atomically idempotency-checked.

    Adversarial review finding #1 (BLOCKING): the idempotency lookup and the
    persist happen INSIDE ``A2UIStore.persist_action_part``, under the SAME
    per-session lock ``apply_batch_outcome`` already uses -- a genuinely
    concurrent double-submission can no longer race a stale read against the
    write. ``record.idempotency_key`` is empty for an error record, which
    always persists unconditionally (errors have no per-click identity).

    Returns:
        The EXISTING record's wire dict when ``idempotency_key`` already
        matches a persisted record (nothing new was written -- the caller
        must not deliver again), or ``None`` once this record's ``received``
        snapshot has been freshly persisted.
    """

    store = app.state.a2ui_store
    return store.persist_action_part(
        record.session_id, record.to_part(), idempotency_key=record.idempotency_key
    )


def persist_transition(app: "FastAPI", record: ActionRecord) -> bool:
    """Persist a follow-up lifecycle snapshot (same id, new state/delivery).

    Never passes ``idempotency_key`` (a transition is not a NEW record), so
    ``A2UIStore.persist_action_part`` always takes its unconditional-persist
    branch and returns ``None`` on success -- translated to ``True`` here so
    this function's own ``bool`` contract (used by callers as a plain
    success/failure check) stays accurate regardless of the store's own
    duplicate-vs-fresh return shape.
    """

    store = app.state.a2ui_store
    return store.persist_action_part(record.session_id, record.to_part()) is None


def reason_for_exception(exc: BaseException) -> str:
    """Return the typed reason a delivery-failure record should carry.

    An ``HTTPException`` raised by an existing owner (``retry_turn_action``,
    ``answer_user_question``, ...) already carries its own typed
    ``error.error`` code in its ``detail`` -- reuse it verbatim so the
    record's ``reason`` matches the response the caller actually saw. Any
    other exception (a genuinely unexpected failure) gets the generic
    ``a2ui_delivery_error`` reason.
    """

    if isinstance(exc, HTTPException) and isinstance(exc.detail, Mapping):
        error = exc.detail.get("error")
        if isinstance(error, Mapping) and error.get("error"):
            return str(error["error"])
    return "a2ui_delivery_error"


def status_for_exception(exc: BaseException) -> int:
    """Return the HTTP status a delivery-failure record's re-raise should carry."""

    if isinstance(exc, HTTPException):
        return exc.status_code
    return 500


def fail_and_publish(
    app: "FastAPI", session_id: str, record: ActionRecord, exc: BaseException
) -> ActionRecord:
    """Transition ``record`` to ``failed``/``rejected`` for ``exc``, persist, publish.

    Adversarial review finding #2 (BLOCKING): every owner call a delivery
    lane makes (``answer_user_question``, ``_start_background_user_turn``,
    ``enqueue_user_steer``, ``resolve_permission``, ``cancel_session_state``,
    ``retry_turn_action``) is wrapped so a raise lands here BEFORE the
    caller re-raises the SAME exception -- a record can no longer strand at
    ``received`` while its HTTP response reports a failure.
    """

    from clio_agent.gact.events import Event  # noqa: PLC0415

    failed = record.transition(
        state="failed",
        delivery="rejected",
        reason=reason_for_exception(exc),
        http_status=status_for_exception(exc),
    )
    persist_transition(app, failed)
    app.state.bus.publish(
        Event(
            type="a2ui.action.failed",
            session_id=session_id,
            payload=lifecycle_event_payload(failed),
        )
    )
    return failed


def fold_action_records(
    parts: "list[Any]", session_id: str, surfaces: "dict[tuple[str, str], Any]"
) -> list[dict[str, str]]:
    """Fold a session's ``a2ui_action`` parts onto their owning surfaces.

    ``parts`` is assumed already in causal order (the same ``recorded_at``
    ordering :meth:`A2UIStore._parts` applies to ``a2ui`` parts). Mutates
    ``surfaces[(session_id, surface_id)].actions`` in place for every key
    already present in the projection; an action whose surface key is
    unknown here is skipped (the surface was never produced through this
    projection, so there is nowhere to attach its action history).

    Also applies the ONE cross-cutting projection rule S5 needs: a surface
    whose latest error record is a repair-exhausted ``VALIDATION_FAILED`` at
    the surface's CURRENT revision folds to ``state="failed"`` (a genuine new
    revision resets this, since the exhausted record's ``correlation
    ["revision"]`` then no longer matches).

    Returns:
        Typed degradations for any malformed ``a2ui_action`` part encountered
        (mirrors ``project_a2ui_parts``'s ``a2ui_persisted_payload_invalid``).
    """

    latest: dict[str, dict[str, Any]] = {}
    first_seen_order: list[str] = []
    degradations: list[dict[str, str]] = []
    for raw_part in parts:
        part = raw_part.to_wire() if hasattr(raw_part, "to_wire") else raw_part
        if not isinstance(part, Mapping) or part.get("type") != A2UI_ACTION_PART_TYPE:
            continue
        raw_record = part.get("a2ui_action_record")
        record_id = str(raw_record.get("id") or "") if isinstance(raw_record, Mapping) else ""
        if not isinstance(raw_record, Mapping) or not record_id:
            degradations.append(
                {
                    "code": "a2ui_action_persisted_payload_invalid",
                    "reason": (
                        f"A2UI action part {part.get('id', '<unknown>')} has no valid record."
                    ),
                    "part_id": str(part.get("id") or ""),
                }
            )
            continue
        if record_id not in latest:
            first_seen_order.append(record_id)
        latest[record_id] = dict(raw_record)

    by_surface: dict[str, list[dict[str, Any]]] = {}
    for record_id in first_seen_order:
        record = latest[record_id]
        by_surface.setdefault(str(record.get("surface_id") or ""), []).append(record)

    for surface_id, records in by_surface.items():
        surface = surfaces.get((session_id, surface_id))
        if surface is None:
            continue
        surface.actions = records
        latest_error = next((row for row in reversed(records) if row.get("kind") == "error"), None)
        if (
            latest_error is not None
            and latest_error.get("state") == "failed"
            and latest_error.get("reason") == "a2ui_repair_exhausted"
            and latest_error.get("correlation", {}).get("revision") == surface.revision
        ):
            surface.state = "failed"
            surface.error = "a2ui_repair_exhausted"

    return degradations


def mark_a2ui_action_consumed(
    app: "FastAPI", session_id: str, metadata: Mapping[str, Any] | None
) -> bool:
    """Transition a ``delivered`` record to ``consumed`` when a turn reads it.

    The hook every turn-start/steer-drain path calls with the STAGED user
    message's (or drained steer's) metadata (S5 deliverable 3, "consumed is
    emitted when the turn/steer that carried the record starts executing").
    A no-op — never an error — when the metadata carries no
    ``metadata["a2ui_action"]`` record id, the record cannot be found, or it
    is not currently ``delivered`` (already consumed, or this metadata
    belongs to an unrelated turn): callers run this unconditionally on every
    turn start, so silence is the common case, not a defect.
    """

    if not isinstance(metadata, Mapping):
        return False
    record_id = str(metadata.get("a2ui_action") or "")
    surface_id = str(metadata.get("surface_id") or "")
    if not record_id or not surface_id:
        return False
    store = getattr(app.state, "a2ui_store", None)
    if store is None:
        return False
    surface = store.get(session_id, surface_id)
    if surface is None:
        return False
    current = next((row for row in surface.actions if row.get("id") == record_id), None)
    if current is None or current.get("state") != "delivered":
        return False
    record = ActionRecord.from_wire(current)
    if record is None:
        return False
    updated = record.transition(state="consumed")
    if not persist_transition(app, updated):
        return False
    from clio_agent.gact.events import Event  # noqa: PLC0415

    app.state.bus.publish(
        Event(
            type="a2ui.action.consumed",
            session_id=session_id,
            payload=lifecycle_event_payload(updated),
        )
    )
    return True


__all__ = [
    "A2UI_ACTION_PART_TYPE",
    "ActionDelivery",
    "ActionRecord",
    "ActionState",
    "compute_idempotency_key",
    "fail_and_publish",
    "find_by_idempotency_key",
    "fold_action_records",
    "last_action_wire",
    "lifecycle_event_payload",
    "mark_a2ui_action_consumed",
    "new_record_id",
    "persist_new_record",
    "persist_transition",
    "prior_error_count_for_revision",
    "reason_for_exception",
    "status_for_exception",
    "utcnow_iso",
]
