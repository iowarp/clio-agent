"""Session-scoped producibility: which installed catalogs a session may CREATE.

``CatalogRegistry.get``/``.installed()`` answer "does this id resolve" for
EVERY installed catalog (builtin and every installed plugin's). Producibility
is narrower: a session may only mint a ``createSurface`` against a catalog its
OWN agent's plugins declare (the plugins' catalogs are the COMPLETE allowlist;
an agent whose plugins declare nothing produces nothing).
:func:`resolve_session_catalogs` is the ONE resolution every
producibility/disclosure consumer derives from. An installed-but-undeclared
catalog still resolves (so replay of an old surface never breaks) but is not
producible. The same pinned resolution decides the session's MCP servers, so
the two "what can this session use" answers follow one decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from clio_agent.gact.a2ui_catalogs.reasons import (
    record_a2ui_catalog_reason,
    record_a2ui_catalog_reason_once,
)
from clio_agent.gact.protocol.constants import A2UI_V091

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.a2ui_catalogs.declarations import ResolvedCatalogs
    from clio_agent.gact.a2ui_catalogs.registry import CatalogEntry, CatalogResolver


def _record_once(app: "FastAPI", session_id: str, reason: str, **fields: Any) -> None:
    """Record a re-derived resolution reason once per (session, reason, fields)."""

    key = tuple(sorted((name, str(value)) for name, value in fields.items()))
    registry = getattr(getattr(app, "state", None), "a2ui_catalogs", None)
    if registry is not None:
        registry.record_session_reason_once(session_id, reason, key=key, **fields)
    else:
        record_a2ui_catalog_reason_once(
            (session_id, reason, *key), reason, session_id=session_id, **fields
        )


def session_declaration_sources(app: "FastAPI", session_id: str) -> list[Any]:
    """The session agent's ordered catalog declaration sources, one per plugin.

    An agent whose plugins do not resolve contributes one ``unresolved`` source,
    so the typed empty reason is ``a2ui_agent_unresolved``; nothing is
    substituted in its place.
    """

    from clio_agent.gact.a2ui_catalogs.declarations import (  # noqa: PLC0415
        CatalogDeclarationSource,
        plugin_catalog_sources,
    )
    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        SessionAgentUnavailable,
        session_resolution,
    )

    try:
        resolution = session_resolution(app, session_id)
    except SessionAgentUnavailable as exc:
        _record_once(app, session_id, "a2ui_agent_unresolved", agent_ref=exc.ref)
        return [
            CatalogDeclarationSource(
                unit_kind="agent",
                unit_id=f"agent:{exc.ref}",
                declared=False,
                unresolved=True,
            )
        ]
    return plugin_catalog_sources(resolution.catalogs)


def resolve_session_catalogs(app: "FastAPI", session_id: str) -> "ResolvedCatalogs":
    """Return the ONE catalog resolution for ``session_id``'s agent.

    Producibility (:func:`session_producible_catalog_ids`), the capability
    advertisement, catalog selection, the catalog skill index, producer-tool
    attachment and the catalog routes all derive from this -- the ordered,
    deduplicated union of the agent's declaration sources.
    """

    from clio_agent.gact.a2ui_catalogs.declarations import (  # noqa: PLC0415
        resolve_agent_catalogs,
    )

    resolved = resolve_agent_catalogs(session_declaration_sources(app, session_id))
    for issue in resolved.issues:
        _record_once(
            app,
            session_id,
            issue.reason,
            catalog=issue.name,
            unit=issue.unit_id,
            detail=issue.detail,
        )
    return resolved


def session_producible_catalog_ids(app: "FastAPI", session_id: str) -> list[str]:
    """Return the catalog ids ``session_id`` may CREATE a surface against.

    Exactly the catalogs the session's agent declares, in its declared
    (preference) order -- no builtin is implicit. A session whose agent
    declares nothing (or does not resolve) gets none.

    Args:
        app: The FastAPI app carrying ``app.state.a2ui_catalogs``.
        session_id: The session to resolve producibility for.

    Returns:
        Declared-order, deduplicated catalog ids.
    """

    if getattr(app.state, "a2ui_catalogs", None) is None:
        record_a2ui_catalog_reason(
            "a2ui_catalog_unavailable",
            session_id=session_id,
            detail="app.state.a2ui_catalogs is not set; producibility degrades to no catalogs",
        )
        return []
    return list(resolve_session_catalogs(app, session_id).catalog_ids)


def session_a2ui_producers_enabled(app: Any, session_id: str) -> bool:
    """Whether a ROOT agent in ``session_id`` gets the A2UI producer tools.

    True only when the agent resolves at least one catalog. Otherwise the
    typed reason (``a2ui_no_catalogs_declared`` when nothing is declared,
    ``a2ui_no_catalogs_resolved`` when declarations resolve to nothing) is
    recorded on the session ledger and the trace -- never a silent omission.
    Without an app or session there is nothing to resolve against, which is
    traced the same way.
    """

    from clio_agent.runtime import trace  # noqa: PLC0415

    registry = getattr(getattr(app, "state", None), "a2ui_catalogs", None)
    if registry is None or not session_id:
        trace.event("A2UI", "producer tools withheld: no app/session catalog context")
        return False
    reason = resolve_session_catalogs(app, session_id).empty_reason()
    if reason is None:
        return True
    if registry.record_session_reason_once(session_id, reason):
        trace.event("A2UI", "producer tools withheld for %s: %s", session_id, reason)
    return False


@dataclass(frozen=True)
class _SessionCatalogResolver:
    """The app-level registry, layered with the session's own resolved catalogs.

    The app-level ``CatalogRegistry`` caches the installed plugins' catalogs; a
    session pinned to a plugin version that is no longer the installed one
    still validates against the catalogs its own resolution names, because
    ``validate_server_message`` resolves ``catalogId`` through this resolver
    BEFORE the producibility gate runs.
    """

    base: "CatalogResolver"
    app: "FastAPI"
    session_id: str

    def get(self, catalog_id: str, protocol_version: str = A2UI_V091) -> "CatalogEntry | None":
        found = self.base.get(catalog_id, protocol_version)
        if found is not None:
            return found
        return resolve_session_catalogs(self.app, self.session_id).get(catalog_id, protocol_version)


def session_catalog_resolver(app: "FastAPI", session_id: str) -> "CatalogResolver":
    """Return the catalog resolver a production door should validate against.

    The app-level registry (``app.state.a2ui_catalogs``) plus, when the
    catalogId it names is not otherwise installed, the session's own resolved
    catalogs.
    """

    return _SessionCatalogResolver(base=app.state.a2ui_catalogs, app=app, session_id=session_id)


__all__ = [
    "resolve_session_catalogs",
    "session_a2ui_producers_enabled",
    "session_catalog_resolver",
    "session_declaration_sources",
    "session_producible_catalog_ids",
]
