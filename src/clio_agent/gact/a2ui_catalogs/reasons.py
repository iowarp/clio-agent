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
            "the catalog is installed but not in this session's producible set — the "
            "session's agent does not list it in its a2ui_catalogs"
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
            "Agent Blueprint discovery raised while resolving A2UI catalogs -- that "
            "blueprint's declared catalogs are unavailable (no builtin is added in "
            "their place), not silently replaced by a stale list"
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
            "a surface named in a2uiClientDataModel was not created with "
            "sendDataModel (or has since been deleted) -- on POST /messages "
            "and .../retry (S3) this refuses the WHOLE request 422 when no live "
            "surface in the session requested it at all; on the A2UI action door "
            "(S5) it is a PER-SURFACE drop-and-continue -- that one entry is "
            "removed from the carried data model, never silently accepted, and "
            "the rest of the request still proceeds"
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
    "a2ui_preferred_catalog_not_selectable": {
        "severity": "warning",
        "detail": (
            "the caller-preferred catalog id is not in BOTH the client's "
            "supportedCatalogIds and this session's producible set -- selection "
            "refuses rather than silently substituting a different catalog"
        ),
    },
    # S5 (docs/design/a2ui-compat-campaign-2026-09.md): action-dispatcher
    # lifecycle reasons, recorded through the SAME ledger.
    "a2ui_data_model_foreign_surface": {
        "severity": "warning",
        "detail": (
            "a2uiClientDataModel named a surfaceId this session never created -- "
            "that entry is dropped from the carried data model, the rest of the "
            "request still proceeds"
        ),
    },
    "a2ui_action_duplicate": {
        "severity": "info",
        "detail": (
            "an action envelope resubmitted the same idempotency key -- the "
            "existing record is returned, nothing is re-delivered"
        ),
    },
    "a2ui_waiting_user_uncorrelated": {
        "severity": "warning",
        "detail": (
            "the session is waiting_user but no pending question correlates to "
            "this action's surface or context.question_id -- refused 409, the "
            "record is durably marked failed"
        ),
    },
    "a2ui_permission_out_of_scope": {
        "severity": "warning",
        "detail": (
            "the action named a permission_id outside this session's own scope "
            "(itself plus its spawned descendants) -- refused 404, the record is "
            "durably marked failed"
        ),
    },
    "a2ui_repair_exhausted": {
        "severity": "warning",
        "detail": (
            "a second VALIDATION_FAILED for the same surface revision arrived "
            "after one repair delivery already ran -- the surface is marked "
            "state=failed, no further repair is delivered"
        ),
    },
    "a2ui_client_error_unhandled": {
        "severity": "info",
        "detail": (
            "a client error report used a code other than VALIDATION_FAILED -- "
            "persisted as an error record, never delivered to the agent"
        ),
    },
    "a2ui_error_surface_unknown": {
        "severity": "warning",
        "detail": (
            "a client error report named a surfaceId this session never created -- "
            "persisted as a failed error record (200, the record id), NEVER "
            "delivered: an unknown surface is a dead end, not a repair target "
            "(adversarial review #1372, finding #3 -- an unbounded re-drive risk)"
        ),
    },
    "a2ui_delivery_error": {
        "severity": "warning",
        "detail": (
            "an owner call the agent/permission/run delivery lane made "
            "(answer_user_question, _start_background_user_turn, "
            "enqueue_user_steer, resolve_permission, cancel_session_state, "
            "retry_turn_action) raised unexpectedly -- the record is durably "
            "marked failed/rejected before the same exception is re-raised"
        ),
    },
    "a2ui_event_context_invalid": {
        "severity": "warning",
        "detail": (
            "the action's resolved context failed its sidecar-declared "
            "events[<name>].context_schema (JSON Schema Draft 2020-12, "
            "server-side, no network) -- refused 422, the record is durably "
            "marked failed/rejected, naming the failing JSON Pointer "
            "(adversarial review #1372 S7 finding #12)"
        ),
    },
    # S5b (clio-schemas 0.3.2, clio-agent#1363 live-gate finding): the
    # event's MEANING is the pack author's to declare via
    # events[<name>].narration -- when a route declares none (no route at
    # all, or a route without `narration`), delivery falls back to the
    # name+context form a resuming model can misread as a report rather than
    # a request. Recorded ONCE per (session, event name) -- see
    # ``CatalogRegistry.record_narration_undeclared_once``.
    "a2ui_event_narration_undeclared": {
        "severity": "info",
        "detail": (
            "an action event's sidecar route declares no `narration` template "
            "-- delivery falls back to the legacy name+context text instead of "
            "the pack author's declared meaning"
        ),
    },
    # S8 (issue #1374 live-gate comment): a producer-tool refusal reason
    # (gact/a2ui_producer/_refusal.py) recurring within one turn. Recorded by
    # ``CatalogRegistry.record_producer_refusal_reason`` -- observability
    # only, never a cap: the idle-cell evidence was 14 identical
    # ``a2ui_client_capabilities_unknown`` refusals in one turn, invisible
    # without hand-reading the semantic trace.
    "a2ui_producer_refusal_repeated": {
        "severity": "info",
        "detail": (
            "the same producer-tool refusal reason recurred within one turn "
            "-- surfaced for observability only, this call's own result is "
            "never capped or rerouted because of it"
        ),
    },
    # S8 review round (issue #1374 item B): A2UIStore's per-session
    # projection cache (a2ui_store.py::_project) is self-verifying, not
    # hook-driven -- a NON-extension of the previously-folded part sequence
    # (a part removed/reordered, a late-arriving part whose stamp sorts
    # before the cached high-water mark, or a bumped CatalogRegistry/active-
    # blueprint generation) falls back to a full refold from scratch rather
    # than silently trusting a cache that may no longer reflect reality.
    "a2ui_projection_cache_invalidated": {
        "severity": "info",
        "detail": (
            "the session's cached A2UI projection was not a pure extension of "
            "the new part/generation state, so it was rebuilt from scratch "
            "instead of incrementally folded"
        ),
    },
    # Per-agent catalog allowlist (v15 S8, declarations.py): an agent's
    # ``a2ui_catalogs`` is the COMPLETE list of catalogs it may produce
    # against -- nothing is implicit, builtins included.
    "a2ui_no_catalogs_declared": {
        "severity": "info",
        "detail": (
            "the session's agent declares no a2ui_catalogs -- it has no producible "
            "catalogs, so no A2UI producer tools are attached and no catalog skill "
            "is disclosed"
        ),
    },
    "a2ui_declaration_missing_after_upgrade": {
        "severity": "warning",
        "detail": (
            "an installed Agent Blueprint declares no a2ui_catalogs (typically a "
            "pre-S8 snapshot the upgrade re-sync skipped) -- it has no catalogs until "
            "it is updated or its a2ui_catalogs list is added"
        ),
    },
    "a2ui_no_catalogs_resolved": {
        "severity": "warning",
        "detail": (
            "the session's agent declares a2ui_catalogs, but none of them resolved "
            "to a loadable catalog -- it has no producible catalogs"
        ),
    },
    "a2ui_catalog_builtin_unknown": {
        "severity": "warning",
        "detail": (
            "an a2ui_catalogs entry names a builtin catalog this server does not "
            "ship -- the entry is refused, never guessed at"
        ),
    },
    "a2ui_catalog_declaration_invalid": {
        "severity": "warning",
        "detail": (
            "an a2ui_catalogs entry is malformed (not a builtin name or a single "
            "name: directory mapping) or its catalog directory does not load"
        ),
    },
    "a2ui_catalog_declaration_conflict": {
        "severity": "warning",
        "detail": (
            "two a2ui_catalogs declarations use the same name (or the same catalogId) "
            "with a different origin -- the later one is refused, never a silent "
            "override of the earlier one"
        ),
    },
}

#: Ring size shared by this global ledger AND ``CatalogRegistry``'s per-session
#: reason ring (``registry.py``) -- bounded memory is release-gating; one
#: source of truth for "how many" so the two rings can never silently drift.
A2UI_CATALOG_REASON_RING_MAXLEN = 256

#: Bounded ring of recorded reasons, queryable after the fact (same contract
#: as ``recorded_mcp_app_observer_skips``).
_A2UI_CATALOG_REASONS: "deque[dict[str, Any]]" = deque(maxlen=A2UI_CATALOG_REASON_RING_MAXLEN)
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


#: Keys already recorded by :func:`record_a2ui_catalog_reason_once` (bounded:
#: the oldest keys fall out, so a key may be recorded again after that).
_ONCE_KEYS: "deque[tuple[Any, ...]]" = deque(maxlen=A2UI_CATALOG_REASON_RING_MAXLEN)
_ONCE_KEY_SET: set[tuple[Any, ...]] = set()


def record_a2ui_catalog_reason_once(
    key: tuple[Any, ...], reason: str, **fields: Any
) -> dict[str, Any] | None:
    """Record ``reason`` the first time ``key`` is seen; later calls are no-ops.

    For a condition re-derived on every call (a declaration issue found each
    time a session resolves its catalogs), so the ledger and the log carry it
    once rather than once per request.

    Returns:
        The recorded row, or ``None`` when ``key`` was already recorded.
    """

    with _A2UI_CATALOG_REASONS_LOCK:
        if key in _ONCE_KEY_SET:
            return None
        if len(_ONCE_KEYS) == _ONCE_KEYS.maxlen:
            _ONCE_KEY_SET.discard(_ONCE_KEYS[0])
        _ONCE_KEYS.append(key)
        _ONCE_KEY_SET.add(key)
    return record_a2ui_catalog_reason(reason, **fields)


def recorded_a2ui_catalog_reasons() -> list[dict[str, Any]]:
    """Return a snapshot of every recorded A2UI catalog reason (queryable audit)."""

    with _A2UI_CATALOG_REASONS_LOCK:
        return list(_A2UI_CATALOG_REASONS)


__all__ = [
    "A2UI_CATALOG_REASON_RING_MAXLEN",
    "record_a2ui_catalog_reason",
    "record_a2ui_catalog_reason_once",
    "recorded_a2ui_catalog_reasons",
]
