"""``clio doctor`` row for installed agents that declare no A2UI catalogs (v15 S8).

Under the per-agent rule an agent with no ``a2ui_catalogs`` cannot create
interactive views. An installed pack can end up that way after an upgrade when
the version-change re-sync skipped it (locally edited, foreign source, pinned,
or the re-sync failed), so the doctor names each such agent with the typed
``a2ui_declaration_missing_after_upgrade`` notice and the fix. The rule itself
stays strict: nothing here adds catalogs.
"""

from __future__ import annotations

from typing import Any

from clio_agent.gact.a2ui_catalogs.declarations import (
    MISSING_DECLARATION_GUIDANCE,
    a2ui_declaration_notice,
)


def probe_a2ui_declarations(blueprints: list[Any] | None = None) -> Any:
    """Return an ``IntegrationStatus`` for installed blueprints' A2UI declarations.

    Args:
        blueprints: Pre-discovered blueprints; ``None`` runs discovery.

    Returns:
        ``ready`` when every installed blueprint declares ``a2ui_catalogs``,
        ``degraded`` (listing the ids) when some do not, ``unavailable`` when
        discovery itself fails.
    """

    from clio_agent.runtime.status import IntegrationState, IntegrationStatus  # noqa: PLC0415

    name = "a2ui_catalog_declarations"
    source = "installed Agent Blueprints (AGENT.md a2ui_catalogs)"
    if blueprints is None:
        from clio_agent.gact.agent_blueprints import discover_agent_blueprints  # noqa: PLC0415

        try:
            blueprints = discover_agent_blueprints()
        except Exception as exc:  # noqa: BLE001 - reported as a doctor row, never raised
            return IntegrationStatus(
                name=name,
                state=IntegrationState.UNAVAILABLE,
                summary=f"agent blueprint discovery failed: {exc}",
                config_source=source,
                next_action="check the installed agent blueprints",
                required=False,
            )
    missing = sorted(
        row.id
        for row in blueprints
        if str(getattr(row, "root_expert", "") or "").strip()
        and a2ui_declaration_notice(row) is not None
    )
    if not missing:
        return IntegrationStatus(
            name=name,
            state=IntegrationState.READY,
            summary="every installed agent declares its A2UI catalogs",
            config_source=source,
            next_action="none",
            required=False,
        )
    return IntegrationStatus(
        name=name,
        state=IntegrationState.DEGRADED,
        summary=(
            "a2ui_declaration_missing_after_upgrade: "
            f"{', '.join(missing)} cannot create interactive views"
        ),
        config_source=source,
        next_action=MISSING_DECLARATION_GUIDANCE,
        details={"reason": "a2ui_declaration_missing_after_upgrade", "blueprints": missing},
        required=False,
    )


__all__ = ["probe_a2ui_declarations"]
