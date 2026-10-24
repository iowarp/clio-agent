"""A2UI 0.9 catalog selection and per-agent projection (S3).

Split out of ``gact/a2ui_capabilities.py`` (docs/design/a2ui-compat-campaign-
2026-09.md S3, issue #1369 hygiene follow-up) to keep each owner module to
one core concern: that module owns parsing/remembering/door-guarding the
official capability objects; this one owns the SELECTION decision --
:func:`select_catalog` picking a catalog for a session -- and the per-agent
catalog projection (:func:`resolution_catalog_ids`) the agent routes render.

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


def resolution_catalog_ids(resolution: Any) -> list[str]:
    """A resolved agent's catalog ids, in plugin and declaration order.

    The same ``declarations.resolve_agent_catalogs`` resolution a session of
    this agent gets.
    """

    from clio_agent.gact.a2ui_catalogs.declarations import (  # noqa: PLC0415
        plugin_catalog_sources,
        resolve_agent_catalogs,
    )

    resolved = resolve_agent_catalogs(
        plugin_catalog_sources(resolution.catalogs, resolution.plugins), record=True
    )
    return list(resolved.catalog_ids)


__all__ = [
    "CatalogSelection",
    "resolution_catalog_ids",
    "select_catalog",
]
