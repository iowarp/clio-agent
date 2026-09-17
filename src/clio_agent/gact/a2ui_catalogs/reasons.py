"""Typed, non-silent reason catalog for A2UI catalog resolution and validation.

Mirrors ``gact/mcp_app_observer_reasons.py``'s reason-catalog style (itself
modeled on ``tools.execution``'s ``_TOOL_RUNTIME_REASON_DEFINITIONS``): a
closed dict of typed definitions, a ``stream_audit`` JSONL row, and a bounded
in-process ring queryable after the fact
(:func:`recorded_a2ui_catalog_reasons`). Every catalog-boundary degradation —
an unknown catalog id, a pack catalog that is installed but not producible in
this session, a component that fails its catalog's JSON Schema, an
uninstalled catalog encountered on replay — reaches this ledger instead of a
bare exception message, per the campaign's no-silent-fallback ground rule
(``docs/design/system-cleanup-2026-07.md``, ``.claude/CLAUDE.md``).

This module never decides whether a message is valid; callers in
``gact/a2ui.py`` and this package raise :class:`A2UIValidationError` (or a
typed subclass) for control flow and call :func:`record_a2ui_catalog_reason`
purely to make the reason durable and queryable.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any

from clio_agent.runtime.stream_audit import stream_audit

logger = logging.getLogger(__name__)


_A2UI_CATALOG_REASON_DEFINITIONS: dict[str, dict[str, Any]] = {
    "a2ui_catalog_unknown": {
        "severity": "warning",
        "detail": (
            "the message named a catalogId that no installed catalog (builtin or "
            "blueprint-declared pack) registers for this protocol version"
        ),
    },
    "a2ui_catalog_not_producible": {
        "severity": "warning",
        "detail": (
            "the catalog is installed but not in this session's producible set — it "
            "belongs to a pack the session's active blueprint did not declare"
        ),
    },
    "a2ui_catalog_file_invalid": {
        "severity": "warning",
        "detail": "a pack's catalog.json does not validate as the official CatalogFile shape",
    },
    "a2ui_sidecar_invalid": {
        "severity": "warning",
        "detail": "a pack's catalog.clio.json does not validate as CatalogSidecar",
    },
    "a2ui_component_unimplemented": {
        "severity": "warning",
        "detail": (
            "a pack catalog's sidecar implements[].kernel names a component the "
            "renderer's builtin catalogs do not define"
        ),
    },
    "a2ui_function_not_in_catalog": {
        "severity": "warning",
        "detail": "a functionCall named a function the surface's catalog does not declare",
    },
    "a2ui_event_destination_undeclared": {
        "severity": "info",
        "detail": "an action event name has no sidecar route; it defaults to the agent lane",
    },
    "a2ui_catalog_unavailable": {
        "severity": "warning",
        "detail": (
            "a persisted surface's catalog is no longer installed; it replays as "
            "state=unknown instead of being quarantined or dropped"
        ),
    },
    "a2ui_identifier_not_uax31": {
        "severity": "info",
        "detail": (
            "a pack catalog declares a component name outside UAX#31 identifier syntax "
            "(0.9.1 tolerates this; new catalogs should not)"
        ),
    },
    "a2ui_blueprint_discovery_failed": {
        "severity": "warning",
        "detail": (
            "Agent Blueprint discovery raised while resolving A2UI catalogs -- the "
            "session/registry degrades to builtins only, not silently to a stale list"
        ),
    },
    "a2ui_blueprint_unresolved": {
        "severity": "warning",
        "detail": (
            "the session's bound active_agent_blueprint_id did not resolve to any "
            "discovered or path-activated blueprint -- its declared catalogs are dropped"
        ),
    },
    # S3 (docs/design/a2ui-compat-campaign-2026-09.md): capability negotiation
    # and catalog-selection reasons, recorded through the SAME ledger + the
    # registry's per-session ``record_session_reason`` -- no new store.
    "a2ui_client_capabilities_invalid": {
        "severity": "warning",
        "detail": (
            "metadata['a2uiClientCapabilities'] did not validate as the official "
            "A2UI 0.9 client-capabilities object -- refused 422, never coerced"
        ),
    },
    "a2ui_inline_catalogs_unsupported": {
        "severity": "warning",
        "detail": (
            "the client advertised inlineCatalogs, but this agent's own capabilities "
            "declare acceptsInlineCatalogs: false -- refused 422 per the protocol's "
            "own contract (inline catalogs are only sent when the agent accepts them)"
        ),
    },
    "a2ui_client_data_model_invalid": {
        "severity": "warning",
        "detail": (
            "metadata['a2uiClientDataModel'] did not validate as the official "
            "A2UI 0.9 client-data-model object -- refused 422, never coerced"
        ),
    },
    "a2ui_data_model_not_requested": {
        "severity": "warning",
        "detail": (
            "a2uiClientDataModel was sent but no live surface in this session was "
            "created with sendDataModel -- refused 422, the ingest is not silently "
            "accepted for a surface that never asked for it"
        ),
    },
    "a2ui_client_capabilities_unknown": {
        "severity": "info",
        "detail": (
            "catalog selection was attempted before the client ever advertised "
            "a2uiClientCapabilities for this session -- no client preference exists yet"
        ),
    },
    "a2ui_catalog_no_client_match": {
        "severity": "warning",
        "detail": (
            "none of the client's supportedCatalogIds (preference order) intersect "
            "this session's producible catalog set -- no catalog can be selected"
        ),
    },
}

#: Bounded ring of recorded reasons, queryable after the fact (same contract
#: as ``recorded_mcp_app_observer_skips``).
_A2UI_CATALOG_REASONS: "deque[dict[str, Any]]" = deque(maxlen=256)
_A2UI_CATALOG_REASONS_LOCK = threading.Lock()


def record_a2ui_catalog_reason(reason: str, **fields: Any) -> dict[str, Any]:
    """Record one typed A2UI catalog-boundary reason.

    Args:
        reason: A key of :data:`_A2UI_CATALOG_REASON_DEFINITIONS`.
        **fields: Extra structured context (``catalog_id``, ``session_id``,
            ``component``, ``part_id``, ...) merged into the recorded row.

    Returns:
        The recorded row (definition + fields), for a caller that wants to
        embed it directly (e.g. in an HTTP error detail or a degradation
        list entry).

    Raises:
        ValueError: If ``reason`` is not a known definition — an unlisted
            reason must not enter the ledger silently typo'd.
    """

    definition = _A2UI_CATALOG_REASON_DEFINITIONS.get(reason)
    if definition is None:
        raise ValueError(f"Unknown A2UI catalog reason: {reason}")
    payload: dict[str, Any] = {"reason": reason, **definition, **fields}
    with _A2UI_CATALOG_REASONS_LOCK:
        _A2UI_CATALOG_REASONS.append(payload)
    stream_audit("a2ui_catalog_reason", **payload)
    log = logger.warning if definition["severity"] == "warning" else logger.info
    log(
        "A2UI catalog reason=%s detail=%s fields=%s",
        reason,
        definition["detail"],
        dict(fields),
    )
    return payload


def recorded_a2ui_catalog_reasons() -> list[dict[str, Any]]:
    """Return a snapshot of every recorded A2UI catalog reason (queryable audit)."""

    with _A2UI_CATALOG_REASONS_LOCK:
        return list(_A2UI_CATALOG_REASONS)


__all__ = [
    "record_a2ui_catalog_reason",
    "recorded_a2ui_catalog_reasons",
]
