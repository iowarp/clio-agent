"""Shared session/catalog resolution and batch application for producer tools (S4).

Both the HTTP producer door (``routes/a2ui.py``) and every producer tool here
cross the SAME atomic validate-then-append service
(``app.state.a2ui_store.apply_batch_outcome``, moved verbatim in spirit from
``a2ui_tools.py``) — this module is the ONE place a tool translates every
typed A2UI exception into the typed refusal shape (S4 item 3/4): a producer
mistake is a tool RESULT, never an exception.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.gact import context as _ctx
from clio_agent.gact.a2ui import (
    A2UICatalogNotProducibleError,
    A2UICatalogUnknownError,
    A2UIComponentLimitExceededError,
    A2UIFunctionNotInCatalogError,
    A2UITranscriptFrozenError,
    A2UIValidationError,
)
from clio_agent.gact.a2ui_component_fold import upsert_components_by_id
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


def current_surface_components(existing: Any) -> "list[dict[str, Any]]":
    """Replay ``existing``'s own ``updateComponents`` message history into the
    FULL, currently-live component list (an empty list for ``None``/a
    brand-new surface) -- the surface record stores raw ordered messages, not
    a pre-reduced view, so every producer call that needs "what does this
    surface look like right now" folds it the same way, here, once.

    As of G2 (iowarp/gact-tui#513 comment 5937313752), ``a2ui.py`` itself
    already materializes a surface's stored messages down to AT MOST one
    ``updateComponents`` entry on every write (``a2ui_component_fold.
    materialize_update_components``), so this loop folding several such
    entries together only matters for a surface persisted before that
    change; either way the result is the same live component list.
    """

    merged: list[dict[str, Any]] = []
    if existing is None:
        return merged
    for message in getattr(existing, "messages", None) or []:
        update = message.get("updateComponents") if isinstance(message, dict) else None
        components = update.get("components") if isinstance(update, dict) else None
        if isinstance(components, list):
            merged = upsert_components_by_id(merged, components)
    return merged


def merged_surface_components(
    existing: Any, new_components: "list[dict[str, Any]]"
) -> "list[dict[str, Any]]":
    """The surface's FULL component list AFTER upserting ``new_components``.

    Used for the surface-definition artifact (#1533 S4): a producer call may
    only touch a SUBSET of a multi-component surface (``update_a2ui_components``,
    or ``create_a2ui_surface`` revising an existing id), so the stored
    definition must be the whole live surface, never just this call's own
    payload.
    """

    return upsert_components_by_id(current_surface_components(existing), new_components)


def component_tree_error(components: list[dict[str, Any]]) -> str | None:
    """Explain missing or unattached components in a new surface's root tree."""

    by_id = {str(component.get("id")): component for component in components}
    seen: set[str] = set()
    pending = ["root"]
    while pending:
        component_id = pending.pop()
        if component_id in seen:
            continue
        component = by_id.get(component_id)
        if component is None:
            return f'component id="{component_id}" is referenced but not defined'
        seen.add(component_id)
        child = component.get("child")
        if isinstance(child, str):
            pending.append(child)
        if component.get("component") == "Modal":
            for slot in ("trigger", "content"):
                reference = component.get(slot)
                if isinstance(reference, str):
                    pending.append(reference)
        children = component.get("children")
        if isinstance(children, list):
            pending.extend(value for value in children if isinstance(value, str))
        tabs = component.get("tabs")
        if isinstance(tabs, list):
            pending.extend(
                tab["child"]
                for tab in tabs
                if isinstance(tab, dict) and isinstance(tab.get("child"), str)
            )
    unattached = [component_id for component_id in by_id if component_id not in seen]
    if unattached:
        return (
            f'Components {", ".join(unattached)} are not reachable from id="root" '
            "and will not render. Put their ids in a root Row, Column, Grid, "
            "Frame, or Tabs layout."
        )
    return None


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
    except A2UIComponentLimitExceededError as exc:
        app.state.a2ui_catalogs.record_session_reason(
            session_id,
            "a2ui_component_limit_exceeded",
            component_count=exc.component_count,
            limit=exc.limit,
        )
        return refusal("a2ui_component_limit_exceeded", detail=str(exc))
    except A2UIValidationError as exc:
        return refusal(
            "a2ui_validation_failed",
            detail=str(exc),
            hint=component_hint(app, catalog_id, str(exc)),
        )


def resolve_components(
    app: Any,
    session_id: str,
    components: "Optional[list[dict[str, Any]]]",
    components_path: str,
) -> "list[dict[str, Any]] | dict[str, Any]":
    """Resolve the caller's component list from exactly one of two sources.

    ``components`` (inline) or ``components_path`` (a workspace JSON file
    holding the components array) — passing both or neither is a typed
    refusal, never a guess at which one the caller meant. A resolved
    ``components_path`` must name an existing, readable, workspace-contained
    JSON file whose top-level value is an array.
    """

    has_inline = components is not None
    has_path = bool(components_path.strip())
    if has_inline and has_path:
        return refusal(
            "a2ui_components_source_conflict",
            detail="pass exactly one of components or components_path, never both",
        )
    if not has_inline and not has_path:
        return refusal(
            "a2ui_components_source_missing",
            detail="pass one of components (inline) or components_path (a workspace JSON file)",
        )
    if has_inline:
        assert components is not None
        return components

    from clio_agent.gact.artifacts.minting import (  # noqa: PLC0415
        _contained,
        _session_workspace_id,
        _workspace_root,
    )

    workspace_id = _session_workspace_id(app, session_id)
    root = _workspace_root(app, workspace_id)
    if root is None:
        return refusal(
            "a2ui_components_path_unresolved",
            detail="this session's workspace root is unresolvable; cannot read components_path",
        )
    candidate = Path(components_path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    if not _contained(candidate, root):
        return refusal(
            "a2ui_components_path_unresolved",
            detail=f"{components_path!r} is outside this session's workspace ({root})",
        )
    resolved = candidate.resolve(strict=False)
    if not resolved.is_file():
        return refusal(
            "a2ui_components_path_unresolved",
            detail=f"components_path names no file in this session's workspace: {components_path!r}",
        )
    try:
        raw = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return refusal(
            "a2ui_components_path_unresolved",
            detail=f"components_path could not be read: {exc}",
        )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return refusal(
            "a2ui_components_path_invalid",
            detail=f"components_path is not valid JSON: {exc}",
        )
    if not isinstance(parsed, list):
        return refusal(
            "a2ui_components_path_invalid",
            detail="components_path JSON must be an array of component objects",
        )
    non_objects = [index for index, entry in enumerate(parsed) if not isinstance(entry, dict)]
    if non_objects:
        return refusal(
            "a2ui_components_path_invalid",
            detail=(
                "components_path JSON must be an array of component OBJECTS; "
                f"non-object entries at index(es): {non_objects}"
            ),
        )
    return parsed


__all__ = [
    "MAX_REPORTED_SURFACE_IDS",
    "active_app_and_session",
    "apply_messages",
    "current_surface_components",
    "existing_surface",
    "merged_surface_components",
    "resolve_components",
    "surface_registry_fields",
]
