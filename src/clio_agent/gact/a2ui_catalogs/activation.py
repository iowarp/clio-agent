"""Session-scoped producibility: which installed catalogs a session may CREATE.

``CatalogRegistry.get``/``.installed()`` answer "does this id resolve" for
EVERY installed catalog (builtin ∪ every discovered pack). Producibility is
narrower: a session may only mint a ``createSurface`` against a catalog its
OWN agent declared (v15 S8: the agent's ``a2ui_catalogs`` is the COMPLETE
allowlist -- the builtins are producible only when listed, and an agent that
declares nothing produces nothing). :func:`resolve_session_catalogs` is the
ONE resolution every producibility/disclosure consumer derives from. An
installed-but-undeclared catalog still resolves (so replay of an old surface
never breaks) but is not producible -- the active blueprint resolution
mirrors ``blueprint_activation.resolve_active_blueprint_servers`` /
``blueprint_mcp_servers``'s path-first-then-installed resolution exactly, so
the two "what can this session use" answers (MCP servers, A2UI catalogs)
follow one decision.
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


def _active_blueprint(app: "FastAPI", session_id: str) -> Any | None:
    """Return the session's active blueprint definition, or ``None``.

    Resolves the EFFECTIVE blueprint: a live turn's execution overlay (a
    deep-research turn runs the deep-researcher blueprint,
    ``turn_state.py``) wins over the stored ``active_agent_blueprint_id``,
    so that turn's catalogs are the executing agent's own.

    Path-first (an explicitly activated on-disk pack decides outright), then
    the installed registry for the session's bound blueprint id — the same
    two-step resolution ``resolve_active_blueprint_servers`` /
    ``blueprint_mcp_servers`` apply for MCP servers. Discovery goes through
    ``app.state.a2ui_catalogs.discovered_blueprints()`` (cached, no-silent
    typed reason on failure) when a registry is available, never a fresh
    ``discover_agent_blueprints()`` scan per call. Every degradation --
    parse failure, or a bound blueprint id that resolves to nothing -- is a
    typed, recorded reason; none return ``None`` silently.
    """

    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        _runtime_effective_agent_blueprint_id,
        _runtime_effective_agent_blueprint_path,
    )

    blueprint_id = _runtime_effective_agent_blueprint_id(app, session_id)
    if not blueprint_id:
        return None
    blueprint_path = _runtime_effective_agent_blueprint_path(app, session_id)
    from clio_agent.gact.agent_blueprints import parse_agent_blueprint_root  # noqa: PLC0415

    if blueprint_path is not None:
        try:
            blueprint = parse_agent_blueprint_root(blueprint_path, scope="session")
        except Exception as exc:  # noqa: BLE001 - typed, recorded, never silent
            _record_once(
                app,
                session_id,
                "a2ui_blueprint_discovery_failed",
                blueprint_id=blueprint_id,
                detail=str(exc),
            )
            return None
        if blueprint.id == blueprint_id and blueprint.enabled:
            return blueprint
        _record_once(app, session_id, "a2ui_blueprint_unresolved", blueprint_id=blueprint_id)
        return None
    registry = getattr(app.state, "a2ui_catalogs", None)
    if registry is not None:
        blueprints = registry.discovered_blueprints()
    else:
        from clio_agent.gact.agent_blueprints import discover_agent_blueprints  # noqa: PLC0415

        try:
            blueprints = discover_agent_blueprints()
        except Exception as exc:  # noqa: BLE001 - typed, recorded, never silent
            _record_once(
                app,
                session_id,
                "a2ui_blueprint_discovery_failed",
                blueprint_id=blueprint_id,
                detail=str(exc),
            )
            return None
    match = next((row for row in blueprints if row.id == blueprint_id and row.enabled), None)
    if match is None:
        _record_once(app, session_id, "a2ui_blueprint_unresolved", blueprint_id=blueprint_id)
    return match


def session_declaration_sources(app: "FastAPI", session_id: str) -> list[Any]:
    """Return the session agent's ordered catalog declaration sources.

    Today exactly one unit declares: the session's effective Agent Blueprint,
    or -- when none is active -- the code-shipped builtin main the session then
    runs (``catalog.builtin_main_catalog_source``, its own explicit
    declaration). A bound blueprint that does not resolve contributes nothing
    (its typed reason is recorded) rather than falling back to the builtin
    main. Agent-plugins 1.0 appends one source per plugin here; no consumer
    changes (the forward shape in ``declarations.py``).
    """

    from clio_agent.gact.a2ui_catalogs.declarations import (  # noqa: PLC0415
        blueprint_catalog_source,
    )
    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        _runtime_effective_agent_blueprint_id,
    )

    if not _runtime_effective_agent_blueprint_id(app, session_id):
        from clio_agent.gact.catalog import builtin_main_catalog_source  # noqa: PLC0415

        return [builtin_main_catalog_source()]
    blueprint = _active_blueprint(app, session_id)
    return [blueprint_catalog_source(blueprint)] if blueprint is not None else []


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
    declares nothing (or has no active blueprint) gets none.

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
    """The app-level registry, layered with a session's PATH-activated pack.

    A path-activated blueprint (a marketplace pack launched via an on-disk
    path, not yet copied into the installed registry — the same mechanism
    ``resolve_active_blueprint_servers`` uses for MCP servers) declares
    catalogs the app-level ``CatalogRegistry`` cannot see, since it only
    discovers the INSTALLED registry roots. Without this layer, a session
    running such a pack could never produce a surface against its own
    declared catalog: ``validate_server_message`` resolves ``catalogId``
    through this resolver BEFORE the producibility gate even runs.
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
    catalogs (which is how a PATH-activated pack's catalog resolves).
    """

    return _SessionCatalogResolver(base=app.state.a2ui_catalogs, app=app, session_id=session_id)


__all__ = [
    "resolve_session_catalogs",
    "session_a2ui_producers_enabled",
    "session_catalog_resolver",
    "session_declaration_sources",
    "session_producible_catalog_ids",
]
