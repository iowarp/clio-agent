"""Shared session/catalog resolution and batch application for producer tools (S4).

Both the HTTP producer door (``routes/a2ui.py``) and every producer tool here
cross the SAME atomic validate-then-append service
(``app.state.a2ui_store.apply_batch_outcome``, moved verbatim in spirit from
``a2ui_tools.py``) — this module is the ONE place a tool translates every
typed A2UI exception into the typed refusal shape (S4 item 3/4): a producer
mistake is a tool RESULT, never an exception.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from clio_agent.gact import context as _ctx
from clio_agent.gact.a2ui import (
    A2UICatalogNotProducibleError,
    A2UICatalogUnknownError,
    A2UIFunctionNotInCatalogError,
    A2UITranscriptFrozenError,
    A2UIValidationError,
)
from clio_agent.gact.a2ui_producer import _emit
from clio_agent.gact.a2ui_producer._refusal import catalog_hint, component_hint, refusal

if TYPE_CHECKING:
    from clio_agent.gact.a2ui_store import A2UIBatchOutcome

#: The surface registry rides back in the model lane on every production, so
#: it is bounded: a long session's oldest surfaces are the ones least likely
#: to be revised, so the newest ids survive the cut and the drop is stated,
#: never silent (moved from ``a2ui_tools.py`` verbatim, S4).
MAX_REPORTED_SURFACE_IDS = 32


def active_app_and_session() -> "tuple[Any, str] | dict[str, Any]":
    """Return ``(app, session_id)``, or a typed refusal when neither is live."""

    app = _ctx.active_app()
    session_id = _ctx.active_session_id()
    if app is None or not session_id:
        return refusal(
            "a2ui_session_unavailable",
            detail="A2UI production requires an active GACT session.",
        )
    if app.state.sessions.get(session_id) is None:
        return refusal("a2ui_session_not_found", detail=f"Session not found: {session_id}")
    return app, session_id


def existing_surface(app: Any, session_id: str, surface_id: str) -> Any:
    """Return the surface record (live or deleted) for ``surface_id``, or ``None``."""

    return app.state.a2ui_store.get(session_id, surface_id)


def surface_registry_fields(outcome: "A2UIBatchOutcome") -> dict[str, Any]:
    """Return the bounded ``session_surface_ids`` result fields for one outcome."""

    registry = outcome.session_surface_ids
    truncated = len(registry) > MAX_REPORTED_SURFACE_IDS
    fields: dict[str, Any] = {"session_surface_ids": list(registry[-MAX_REPORTED_SURFACE_IDS:])}
    if truncated:
        fields["session_surface_ids_truncated"] = True
    return fields


def apply_messages(
    app: Any,
    session_id: str,
    messages: list[dict[str, Any]],
    *,
    catalog_id: str,
    part_id: str = "",
) -> "A2UIBatchOutcome | dict[str, Any]":
    """Apply one ordered batch, translating every typed A2UI error into a refusal.

    Returns the store's :class:`~clio_agent.gact.a2ui_store.A2UIBatchOutcome`
    on success, or a typed refusal dict (see ``_refusal.refusal``) on any
    catalog/validation/transcript failure — a caller checks
    ``isinstance(result, dict)`` to tell the two apart.
    """

    minted_part_id = part_id or f"live_a2ui_{uuid.uuid4().hex[:12]}"

    def persist_part(candidate: Any) -> bool:
        return _emit.emit_surface_part(app, session_id, candidate)

    try:
        return app.state.a2ui_store.apply_batch_outcome(
            session_id, messages, part_id=minted_part_id, persist_part=persist_part
        )
    except A2UITranscriptFrozenError:
        # The batch was valid but the turn's ledger is already settled, so
        # nothing was persisted or published: report the typed reason rather
        # than a validation message the model cannot act on.
        return refusal(
            "a2ui_transcript_frozen",
            detail="the turn's ledger is already settled; nothing was persisted",
        )
    except A2UICatalogUnknownError as exc:
        # Routes through the SAME per-session recorder the HTTP production
        # door uses (adversarial S2 review) -- a session's catalog-boundary
        # history is retrievable regardless of which door produced it.
        app.state.a2ui_catalogs.record_session_reason(
            session_id, "a2ui_catalog_unknown", catalog_id=exc.catalog_id
        )
        return refusal("a2ui_catalog_unknown", detail=str(exc))
    except A2UICatalogNotProducibleError as exc:
        app.state.a2ui_catalogs.record_session_reason(
            session_id, "a2ui_catalog_not_producible", catalog_id=exc.catalog_id
        )
        return refusal("a2ui_catalog_not_producible", detail=str(exc))
    except A2UIFunctionNotInCatalogError as exc:
        app.state.a2ui_catalogs.record_session_reason(
            session_id,
            "a2ui_function_not_in_catalog",
            function_name=exc.function_name,
            catalog_id=exc.catalog_id,
        )
        return refusal(
            "a2ui_function_not_in_catalog",
            detail=str(exc),
            hint=catalog_hint(app, exc.catalog_id),
        )
    except A2UIValidationError as exc:
        return refusal(
            "a2ui_validation_failed",
            detail=str(exc),
            hint=component_hint(app, catalog_id, str(exc)),
        )


__all__ = [
    "MAX_REPORTED_SURFACE_IDS",
    "active_app_and_session",
    "apply_messages",
    "existing_surface",
    "surface_registry_fields",
]
