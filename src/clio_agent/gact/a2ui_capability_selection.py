"""A2UI 0.9 catalog selection and per-row/blueprint projection (S3).

Split out of ``gact/a2ui_capabilities.py`` (docs/design/a2ui-compat-campaign-
2026-09.md S3, issue #1369 hygiene follow-up) to keep each owner module to
one core concern: that module owns parsing/remembering/door-guarding the
official capability objects; this one owns the SELECTION decision --
:func:`select_catalog` picking a catalog for a session -- and the per-row /
per-blueprint ``a2ui_capabilities`` projection (:func:`with_a2ui_capabilities`,
:func:`blueprint_a2ui_capability_ids`, :func:`catalog_ids_for_resolved_blueprint`)
that ``routes/agents.py`` and ``routes/blueprints.py`` render.

Public names stay importable from ``gact.a2ui_capabilities`` (re-exported
there) so no caller's import path changes; this module only ever reaches
back into ``a2ui_capabilities`` for :func:`~clio_agent.gact.a2ui_capabilities.
client_capabilities` via a call-scoped import inside :func:`select_catalog`,
never a module-level one -- there is no real import cycle, only one clean
edge (``a2ui_capabilities`` -> this module, for the re-export).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from clio_agent.gact.a2ui_catalogs.activation import resolve_session_catalogs

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.a2ui_catalogs.registry import CatalogRegistry


@dataclass(frozen=True)
class CatalogSelection:
    """The result of :func:`select_catalog` -- always returned, never raised.

    A producer tool (S4) turns an unsuccessful selection into a typed tool
    refusal; it is never silently defaulted to a catalog the client did not
    ask for.
    """

    catalog_id: str | None
    reason: str | None  # None on success
    client_supported_catalog_ids: tuple[str, ...] = field(default_factory=tuple)
    producible_catalog_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        """Whether a catalog was actually selected."""

        return self.catalog_id is not None


def select_catalog(
    app: "FastAPI", session_id: str, preferred: str | None = None, *, record: bool = True
) -> CatalogSelection:
    """Select the first catalog the agent declared that the client supports.

    "The agent selects the best match from the client's ``supportedCatalogIds``
    list" (protocol). The best match is decided by the AGENT's declared
    preference order (v15 S8 owner ruling: the agent's ``a2ui_catalogs`` is
    its complete, ordered allowlist) -- the first declared catalog the client
    advertises. ``preferred`` (e.g. an explicit tool argument) wins ONLY when
    it is itself in both sets; it never bypasses either. The choice is not
    persisted here -- "locked per surface" is a surface-record concern the
    producer tool (S4) owns at ``createSurface`` time.

    Every non-selection is a typed, recorded reason, never a silent default:

    * the agent has no producible catalogs -> ``a2ui_no_catalogs_declared``
      (nothing declared) or ``a2ui_no_catalogs_resolved`` (declarations
      resolved to nothing) -- checked first, since no client advertisement
      could change the outcome
    * no client advertisement yet -> ``a2ui_client_capabilities_unknown``
    * ``preferred`` given but not in BOTH the client-supported and the
      producible set -> ``a2ui_preferred_catalog_not_selectable`` (this
      NEVER falls through to the general preference-order pick -- a caller
      that asked for a specific catalog either gets exactly that one or a
      typed refusal, never a silently substituted different one)
    * a real advertisement with zero intersection against the session's
      producible set -> ``a2ui_catalog_no_client_match``

    ``record`` gates whether a non-selection is written to the S2 ledger --
    a pure READ (e.g. ``GET /v1/sessions/{sid}/a2ui/capabilities``, which
    reports "what would selection currently resolve to" for display) passes
    ``record=False`` so merely looking never pollutes the ledger; a real
    selection ATTEMPT (the S4 producer tool) leaves it ``True``.
    """

    from clio_agent.gact.a2ui_capabilities import client_capabilities  # noqa: PLC0415

    registry: "CatalogRegistry | None" = getattr(app.state, "a2ui_catalogs", None)
    resolved = resolve_session_catalogs(app, session_id)
    producible = resolved.catalog_ids
    empty_reason = resolved.empty_reason()
    if empty_reason is not None:
        if record and registry is not None:
            registry.record_session_reason(session_id, empty_reason)
        return CatalogSelection(catalog_id=None, reason=empty_reason)
    caps = client_capabilities(app, session_id)
    if caps is None:
        if record and registry is not None:
            registry.record_session_reason(session_id, "a2ui_client_capabilities_unknown")
        return CatalogSelection(
            catalog_id=None,
            reason="a2ui_client_capabilities_unknown",
            producible_catalog_ids=producible,
        )
    supported = tuple(caps.v0_9.supportedCatalogIds)
    producible_set = set(producible)
    if preferred is not None:
        if preferred in supported and preferred in producible_set:
            return CatalogSelection(
                catalog_id=preferred,
                reason=None,
                client_supported_catalog_ids=supported,
                producible_catalog_ids=producible,
            )
        intersection = [cid for cid in supported if cid in producible_set]
        if record and registry is not None:
            registry.record_session_reason(
                session_id,
                "a2ui_preferred_catalog_not_selectable",
                preferred_catalog_id=preferred,
                intersection=intersection,
            )
        return CatalogSelection(
            catalog_id=None,
            reason="a2ui_preferred_catalog_not_selectable",
            client_supported_catalog_ids=supported,
            producible_catalog_ids=producible,
        )
    supported_set = set(supported)
    for catalog_id in producible:
        if catalog_id in supported_set:
            return CatalogSelection(
                catalog_id=catalog_id,
                reason=None,
                client_supported_catalog_ids=supported,
                producible_catalog_ids=producible,
            )
    if record and registry is not None:
        registry.record_session_reason(
            session_id,
            "a2ui_catalog_no_client_match",
            client_supported_catalog_ids=list(supported),
            producible_catalog_ids=list(producible),
        )
    return CatalogSelection(
        catalog_id=None,
        reason="a2ui_catalog_no_client_match",
        client_supported_catalog_ids=supported,
        producible_catalog_ids=producible,
    )


def blueprint_a2ui_capability_ids(
    app: "FastAPI", agent_blueprint_id: str, *, session_id: str = ""
) -> list[str]:
    """Return one blueprint's resolved catalog ids, in its declared order.

    Resolution mirrors ``a2ui_catalogs.activation._active_blueprint`` exactly
    (path-first, then the installed registry) rather than only consulting
    ``registry.discovered_blueprints()`` -- a blueprint activated by PATH
    (a marketplace pack launched on-disk, not yet copied into the installed
    registry) would otherwise never resolve here. ``session_id``, when given,
    is the row's OWN session scope (the caller of ``routes/agents.py``'s
    listing already has it) -- required to reach the path-activation lookup;
    a session-less caller (or one whose active blueprint doesn't match
    ``agent_blueprint_id``) falls back to the installed-registry-only lookup.
    An id that still does not resolve records the typed
    ``a2ui_blueprint_unresolved`` reason and yields no catalogs. A row with
    no blueprint declares nothing, so it has no catalogs (v15 S8: nothing is
    implicit).
    """

    registry: "CatalogRegistry | None" = getattr(app.state, "a2ui_catalogs", None)
    if registry is None or not agent_blueprint_id:
        return []
    from clio_agent.gact.a2ui_catalogs.activation import _active_blueprint  # noqa: PLC0415

    blueprint = None
    if session_id:
        candidate = _active_blueprint(app, session_id)
        if candidate is not None and candidate.id == agent_blueprint_id:
            blueprint = candidate
    if blueprint is None:
        blueprint = next(
            (row for row in registry.discovered_blueprints() if row.id == agent_blueprint_id),
            None,
        )
    if blueprint is None:
        registry.record_session_reason(
            session_id, "a2ui_blueprint_unresolved", blueprint_id=agent_blueprint_id
        )
        return []
    return catalog_ids_for_resolved_blueprint(app, blueprint)


def catalog_ids_for_resolved_blueprint(app: "FastAPI", blueprint: Any) -> list[str]:
    """Return an ALREADY-RESOLVED blueprint's catalog ids, in its declared order.

    For a caller that has the blueprint object in hand from its OWN
    discovery pass (e.g. ``routes/blueprints.py``'s
    ``GET /v1/agent-blueprints/{id}``, which resolves it via a
    workspace-scoped ``cwd`` the registry's own cache does not share) --
    re-deriving it through :func:`blueprint_a2ui_capability_ids`'s
    id-based lookup would miss a workspace- or session-scoped blueprint the
    registry's global discovery never sees, and silently under-report. The
    same ``declarations.resolve_agent_catalogs`` resolution a session of
    this blueprint gets.
    """

    from clio_agent.gact.a2ui_catalogs.declarations import (  # noqa: PLC0415
        blueprint_catalog_source,
        resolve_agent_catalogs,
    )

    resolved = resolve_agent_catalogs([blueprint_catalog_source(blueprint)], record=True)
    return list(resolved.catalog_ids)


def with_a2ui_capabilities(app: "FastAPI", row: Any, session_id: str = "") -> Any:
    """Return ``row`` (an ``AgentDef``) with ``metadata["a2ui_capabilities"]`` attached.

    This row's OWN declaring blueprint's resolved catalogs (declared order) --
    not the caller session's active blueprint, since a listing enumerates every
    agent, most of which are not the session's current one. ``session_id`` is
    threaded through to :func:`blueprint_a2ui_capability_ids` so a
    PATH-activated blueprint's row resolves correctly (see there).
    """

    agent_blueprint_id = str(row.metadata.get("agent_blueprint_id") or "")
    if not agent_blueprint_id and row.metadata.get("definition_kind") == "builtin_main":
        # The code-shipped builtin main declares its own catalogs (v15 S8).
        from clio_agent.gact.a2ui_catalogs.declarations import (  # noqa: PLC0415
            resolve_agent_catalogs,
        )
        from clio_agent.gact.catalog import builtin_main_catalog_source  # noqa: PLC0415

        ids = list(resolve_agent_catalogs([builtin_main_catalog_source()]).catalog_ids)
    else:
        ids = blueprint_a2ui_capability_ids(app, agent_blueprint_id, session_id=session_id)
    return row.model_copy(update={"metadata": {**row.metadata, "a2ui_capabilities": ids}})


__all__ = [
    "CatalogSelection",
    "blueprint_a2ui_capability_ids",
    "catalog_ids_for_resolved_blueprint",
    "select_catalog",
    "with_a2ui_capabilities",
]
