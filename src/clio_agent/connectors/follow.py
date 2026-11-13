"""The plugins that follow connectors join the person's additions (CN9, CN10).

An enabled connector reaches an agent through its *carrier*: the installed plugins
whose ``ai.iowarp.clio`` extension says ``follows: <id>`` (each binds its server
name to the connector's connection with ``connections: {<id>: <id>}``), or, when no
installed plugin follows it, a carrier CLIO builds from the entry: a plugin with no
content but that one bound server. :func:`additions` is applied by
``PluginService.resolve`` before the person's own delta, for every agent the
connector's scope names, so enabling a connector adds its carrier and disabling it
removes it; nothing is written to the person's agents.

Carriers carry ``follows``, which the plugin routes return so pickers can hide them:
without its connector a carrier has nothing to talk to.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from clio_agent.connectors import state

#: The marketplace name of the carriers CLIO builds.
CARRIER_MARKETPLACE = "clio-connectors"
CARRIER_PREFIX = "connector-"


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


def applicable(agent_refs: Sequence[str]) -> list[str]:
    """Ids of the enabled connectors whose scope names one of ``agent_refs``, sorted."""

    entries = state.all_entries()
    refs = tuple(ref for ref in agent_refs if ref)
    return sorted(
        connector_id
        for connector_id, choice in state.choices().items()
        if connector_id in entries and choice.applies_to(refs)
    )


def additions(records: Iterable[Any], agent_refs: Sequence[str]) -> tuple[list[str], list[Any]]:
    """The plugin refs to add for ``agent_refs`` and the carriers CLIO built for them.

    ``records`` are the installed plugin records (a following plugin among them is
    preferred over a built carrier).
    """

    connector_ids = applicable(agent_refs)
    if not connector_ids:
        return [], []
    following: dict[str, list[str]] = {}
    for record in records:
        if getattr(record, "follows", ""):
            following.setdefault(record.follows, []).append(record.id)
    entries = state.all_entries()
    refs: list[str] = []
    built: list[Any] = []
    for connector_id in connector_ids:
        plugins = sorted(set(following.get(connector_id, ())))
        if plugins:
            refs.extend(plugins)
        else:
            record = built_carrier(entries[connector_id])
            built.append(record)
            refs.append(record.id)
    return refs, built


def follows_flag(record_or_row: Any) -> str:
    """The connector a plugin record or row follows ("" when none)."""

    if isinstance(record_or_row, dict):
        return str(record_or_row.get("follows") or "")
    return str(getattr(record_or_row, "follows", "") or "")
