"""Session-scoped producibility: which installed catalogs a session may CREATE.

``CatalogRegistry.get``/``.installed()`` answer "does this id resolve" for
EVERY installed catalog (builtin ∪ every discovered pack). Producibility is
narrower: a session may only mint a ``createSurface`` against a catalog its
OWN active blueprint declared (plus the two builtins, always available). An
installed-but-inactive pack's catalog resolves (so replay of an old surface
never breaks) but is not producible in a session that never activated that
pack — mirrors ``blueprint_activation.resolve_active_blueprint_servers`` /
``blueprint_mcp_servers``'s path-first-then-installed resolution exactly, so
the two "what can this session use" answers (MCP servers, A2UI catalogs)
follow one decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from clio_agent.gact.a2ui_catalogs.reasons import record_a2ui_catalog_reason
from clio_agent.gact.protocol.constants import A2UI_V091

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.a2ui_catalogs.registry import CatalogEntry, CatalogResolver


def _active_blueprint(app: "FastAPI", session_id: str) -> Any | None:
    """Return the session's active blueprint definition, or ``None``.

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
        _runtime_active_agent_blueprint_id,
        _runtime_active_agent_blueprint_path,
    )

    blueprint_id = _runtime_active_agent_blueprint_id(app, session_id)
    if not blueprint_id:
        return None
    blueprint_path = _runtime_active_agent_blueprint_path(app, session_id)
    from clio_agent.gact.agent_blueprints import parse_agent_blueprint_root  # noqa: PLC0415

    if blueprint_path is not None:
        try:
            blueprint = parse_agent_blueprint_root(blueprint_path, scope="session")
        except Exception as exc:  # noqa: BLE001 - typed, recorded, never silent
            record_a2ui_catalog_reason(
                "a2ui_blueprint_discovery_failed",
                blueprint_id=blueprint_id,
                session_id=session_id,
                detail=str(exc),
            )
            return None
        if blueprint.id == blueprint_id and blueprint.enabled:
            return blueprint
        record_a2ui_catalog_reason(
            "a2ui_blueprint_unresolved", blueprint_id=blueprint_id, session_id=session_id
        )
        return None
    registry = getattr(app.state, "a2ui_catalogs", None)
    if registry is not None:
        blueprints = registry.discovered_blueprints()
    else:
        from clio_agent.gact.agent_blueprints import discover_agent_blueprints  # noqa: PLC0415

        try:
            blueprints = discover_agent_blueprints()
        except Exception as exc:  # noqa: BLE001 - typed, recorded, never silent
            record_a2ui_catalog_reason(
                "a2ui_blueprint_discovery_failed",
                blueprint_id=blueprint_id,
                session_id=session_id,
                detail=str(exc),
            )
            return None
    match = next((row for row in blueprints if row.id == blueprint_id and row.enabled), None)
    if match is None:
        record_a2ui_catalog_reason(
            "a2ui_blueprint_unresolved", blueprint_id=blueprint_id, session_id=session_id
        )
    return match


def session_producible_catalog_ids(app: "FastAPI", session_id: str) -> list[str]:
    """Return the catalog ids ``session_id`` may CREATE a surface against.

    Always includes both builtin catalogs (Basic, CLIO workspace) plus every
    catalog the session's active blueprint declares. A session with no
    active blueprint gets the builtins only.

    Args:
        app: The FastAPI app carrying ``app.state.a2ui_catalogs``.
        session_id: The session to resolve producibility for.

    Returns:
        Sorted, deduplicated catalog ids.
    """

    from clio_agent.gact.a2ui_catalogs.blueprint import (  # noqa: PLC0415
        blueprint_catalog_map,
        load_blueprint_catalogs,
    )

    registry = getattr(app.state, "a2ui_catalogs", None)
    if registry is None:
        record_a2ui_catalog_reason(
            "a2ui_catalog_unavailable",
            session_id=session_id,
            detail="app.state.a2ui_catalogs is not set; producibility degrades to no catalogs",
        )
        ids: set[str] = set()
    else:
        ids = {entry.catalog_id for entry in registry.builtin()}
    blueprint = _active_blueprint(app, session_id)
    if blueprint is not None and blueprint_catalog_map(blueprint):
        ids.update(entry.catalog_id for entry in load_blueprint_catalogs(blueprint))
    return sorted(ids)


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
        blueprint = _active_blueprint(self.app, self.session_id)
        if blueprint is None:
            return None
        from clio_agent.gact.a2ui_catalogs.blueprint import load_blueprint_catalogs  # noqa: PLC0415

        return next(
            (
                entry
                for entry in load_blueprint_catalogs(blueprint)
                if entry.catalog_id == catalog_id and entry.protocol_version == protocol_version
            ),
            None,
        )


def session_catalog_resolver(app: "FastAPI", session_id: str) -> "CatalogResolver":
    """Return the catalog resolver a production door should validate against.

    The app-level registry (``app.state.a2ui_catalogs``) plus, when the
    catalogId it names is not otherwise installed, the session's own
    PATH-activated blueprint's declared catalogs.
    """

    return _SessionCatalogResolver(base=app.state.a2ui_catalogs, app=app, session_id=session_id)


__all__ = ["session_catalog_resolver", "session_producible_catalog_ids"]
