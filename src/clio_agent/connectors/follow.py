"""A connector's plugin, added to a conversation by the person (CN9, CN10 *Amended*).

Enabling a connector installs and readies it; it adds nothing to any agent. What brings
its tools to a conversation is its *plugin*: an installed plugin whose ``ai.iowarp.clio``
extension says ``follows: <id>`` (each binds its server name to the connector's
connection with ``connections: {<id>: <id>}``), or, when no installed plugin follows it,
a *carrier* CLIO builds from the entry: a plugin with no content but that one bound
server, id ``connector-<id>``.

* The catalog lists a carrier for every enabled connector (:func:`carriers`), so the
  plugin routes and the resolver both see it, and the conversation's plugin picker
  offers it like any plugin. The person adds it to one conversation (the session delta)
  or to an agent definition (a user agent's ``add``).
* A delta naming the plugin of a connector that is off drops that plugin from the
  resolution with a typed ``removed`` row (:func:`drop_off`): ``connector_off``, or
  ``connector_not_found`` when the connector no longer exists. It never fails the
  resolution, and turning the connector on again brings the plugin back.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from clio_agent.connectors import state

#: The marketplace name of the carriers CLIO builds.
CARRIER_MARKETPLACE = "clio-connectors"
CARRIER_PREFIX = "connector-"
#: Typed ``removed`` reasons of a connector plugin a delta names.
CONNECTOR_OFF = "connector_off"
CONNECTOR_GONE = "connector_not_found"


def carrier_id(connector_id: str) -> str:
    """The id of the carrier CLIO builds for a connector without a following plugin."""
    return f"{CARRIER_PREFIX}{connector_id}"


def built_carrier(entry: Any) -> Any:
    """A content-free plugin that binds ``entry``'s connection under its id."""

    from clio_agent.plugins.records import PluginRecord  # noqa: PLC0415

    return PluginRecord(
        id=carrier_id(entry.id),
        version="1.0.0",
        description=f"{entry.name}: {entry.description}",
        kind="plugin",
        format="agent-plugins",
        root=entry.folder or "",
        marketplace=CARRIER_MARKETPLACE,
        mcp_servers={entry.id: {"type": "clio-connection", "connection": entry.id}},
        follows=entry.id,
    )


def enabled_ids() -> set[str]:
    """The connectors the person has turned on (and that still exist)."""

    entries = state.all_entries()
    return {cid for cid, choice in state.choices().items() if choice.enabled and cid in entries}


def following(records: Iterable[Any]) -> dict[str, list[str]]:
    """Installed plugins by the connector they follow (ids sorted)."""

    out: dict[str, set[str]] = {}
    for record in records:
        if getattr(record, "follows", ""):
            out.setdefault(record.follows, set()).add(record.id)
    return {cid: sorted(ids) for cid, ids in out.items()}


def carriers(records: Iterable[Any]) -> list[Any]:
    """A carrier for every enabled connector, sorted by id.

    Every enabled connector has one in the resolver's catalog, so a delta that named a
    carrier keeps resolving after a following plugin is installed; the plugin listing
    shows it only when no installed plugin follows the connector (:func:`listed_carriers`).
    """

    installed = {record.id for record in records}
    entries = state.all_entries()
    return [
        built_carrier(entries[cid])
        for cid in sorted(enabled_ids())
        if carrier_id(cid) not in installed
    ]


def listed_carriers(records: Iterable[Any]) -> list[Any]:
    """The carriers a plugin picker offers: enabled connectors no installed plugin follows."""

    rows = list(records)
    followed = following(rows)
    return [record for record in carriers(rows) if record.follows not in followed]


def plugin_refs(connector_id: str, records: Iterable[Any]) -> list[str]:
    """``marketplace/id`` of what adds ``connector_id`` to a conversation.

    The installed plugins that follow it, else its carrier.
    """

    rows = [r for r in records if getattr(r, "follows", "") == connector_id]
    if rows:
        return sorted(f"{r.marketplace}/{r.id}" for r in rows)
    return [f"{CARRIER_MARKETPLACE}/{carrier_id(connector_id)}"]


def connector_of(plugin_id: str, records: Iterable[Any]) -> str:
    """The connector ``plugin_id`` follows: an installed plugin's ``follows``, or a
    carrier's id; "" for any other plugin."""

    installed = {record.id: record for record in records}
    record = installed.get(plugin_id)
    if record is not None:
        return str(getattr(record, "follows", "") or "")
    if plugin_id.startswith(CARRIER_PREFIX):
        return plugin_id.removeprefix(CARRIER_PREFIX)
    return ""


def drop_off(delta: Any, records: Iterable[Any]) -> tuple[Any, list[dict[str, str]]]:
    """``delta`` without the plugins of connectors that are off, and why each left.

    Each dropped plugin is a ``removed`` row ``{id, reason, because}`` with ``because``
    naming the connector, so the resolution says why instead of failing.
    """

    from clio_agent.plugins.resolve import Delta  # noqa: PLC0415
    from clio_agent.sdk.versions import PluginRef  # noqa: PLC0415

    if not delta.add:
        return delta, []
    rows = list(records)
    on = enabled_ids()
    entries = state.all_entries()
    kept: list[str] = []
    dropped: list[dict[str, str]] = []
    for ref in delta.add:
        plugin_id = PluginRef.parse(ref).id
        connector_id = connector_of(plugin_id, rows)
        if not connector_id or connector_id in on:
            kept.append(ref)
            continue
        reason = CONNECTOR_OFF if connector_id in entries else CONNECTOR_GONE
        dropped.append({"id": plugin_id, "reason": reason, "because": connector_id})
    if not dropped:
        return delta, []
    return Delta(tuple(kept), delta.remove), dropped


def drifted(resolution: Any) -> bool:
    """Whether a pinned resolution no longer matches which connectors are on.

    A plugin of a connector now off is still in it, or a plugin dropped because its
    connector was off can come back.
    """

    named = {row["follows"] for row in resolution.plugins if row.get("follows")}
    dropped = {
        str(row.get("because") or "")
        for row in resolution.removed
        if row.get("reason") in {CONNECTOR_OFF, CONNECTOR_GONE}
    }
    if not named and not dropped:
        return False
    on = enabled_ids()
    return bool(named - on) or bool(dropped & on)


def follows_flag(record_or_row: Any) -> str:
    """The connector a plugin record or row follows ("" when none)."""

    if isinstance(record_or_row, dict):
        return str(record_or_row.get("follows") or "")
    return str(getattr(record_or_row, "follows", "") or "")
