"""Connector plugins in the plugin catalog (CN9 *Amended*).

``GET /v1/plugins`` lists, besides the installed plugins, the carrier of every enabled
connector that no installed plugin follows (``connectors.follow.listed_carriers``), so a
conversation's plugin picker offers it like any plugin. Every row that follows a
connector carries ``connector``: the connector's id, name, logo, whether it is on, and
its state in words, so a picker shows the logo, the name and the state.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from clio_agent.runtime import trace


def carrier_records(records: list[Any]) -> list[Any]:
    """The carriers the plugin catalog lists beside ``records``."""

    from clio_agent.connectors.follow import listed_carriers  # noqa: PLC0415

    return listed_carriers(records)


def connector_brief(app: Any, connector_id: str) -> dict[str, Any] | None:
    """``{id, name, logo_url, enabled, status, reason}`` of a connector; None if unknown."""

    from clio_agent.connectors import state  # noqa: PLC0415
    from clio_agent.gact import connectors_service as service  # noqa: PLC0415

    found = state.all_entries().get(connector_id)
    if found is None:
        return None
    try:
        row = service.row(app, found)
    except (state.ConnectorStateError, OSError) as exc:
        # The plugin row still lists; the brief says its state was not read, and why.
        trace.event(
            "PLUGINS",
            "connector_brief_unavailable connector=%s (%s)",
            connector_id,
            type(exc).__name__,
        )
        # An unreadable state file says nothing of the choice either: not enabled.
        unread = isinstance(exc, state.ConnectorStateError)
        row = {
            "enabled": False if unread else state.choice(connector_id).enabled,
            "state": {"status": "unknown", "reason": f"connector_state_unreadable: {exc}"},
        }
    status = row.get("state") or {}
    return {
        "id": found.id,
        "name": found.name,
        "logo_url": f"/v1/connectors/{found.id}/logo" if found.logo else None,
        "enabled": bool(row.get("enabled")),
        "status": str(status.get("status") or "unknown"),
        "needs": list(status.get("needs") or ()),
        "reason": status.get("reason"),
        "tool_count": row.get("tool_count"),
    }


def catalog_rows(
    app: Any,
    records: list[Any],
    row: Callable[..., dict[str, Any]],
    trusted: Callable[[Any], bool],
) -> list[dict[str, Any]]:
    """Installed plugin rows plus listed carriers, each following row with ``connector``."""

    rows = [row(record, trusted=trusted(record)) for record in records]
    rows += [row(record, trusted=True) for record in carrier_records(records)]
    briefs: dict[str, dict[str, Any] | None] = {}
    for item in rows:
        connector_id = str(item.get("follows") or "")
        if not connector_id:
            continue
        if connector_id not in briefs:
            briefs[connector_id] = connector_brief(app, connector_id)
        item["connector"] = briefs[connector_id]
    return rows
