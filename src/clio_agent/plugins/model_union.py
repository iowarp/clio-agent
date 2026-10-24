"""A resolution's model section: the union of requirements and the preference chain."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from clio_agent.sdk.model import Prefers, Requires

if TYPE_CHECKING:
    from clio_agent.plugins.resolve import Entry


def model_union(
    agent_requires: Mapping[str, Any],
    agent_prefers: Mapping[str, Any],
    entries: list["Entry"],
    agent_id: str,
) -> dict[str, Any]:
    """The session's model requirement (the union over the agent and its plugins),
    which plugin required what, and the preferences in the order they are tried."""

    union = Requires.from_dict(agent_requires)
    attribution: dict[str, list[str]] = {key: [f"agent:{agent_id}"] for key in union.to_dict()}
    for entry in entries:
        if not entry.record.model:
            continue
        need = Requires.from_dict(entry.record.model)
        union = union.union(need)
        for key in need.to_dict():
            attribution.setdefault(key, []).append(entry.record.id)
    own = Prefers.from_dict(agent_prefers).to_dict()
    # The order a child's model preference is tried in: the agent (or subagent)
    # itself, then its plugins in resolution order (docs/design/sdk/model-fleet.md).
    chain = [{"source": f"agent:{agent_id}", **own}] if own else []
    for entry in entries:
        if preferred := Prefers.from_dict(entry.record.prefers).to_dict():
            chain.append({"source": entry.record.id, **preferred})
    return {
        "requires": union.to_dict(),
        "attribution": attribution,
        "prefers": own,
        "prefers_chain": chain,
    }


__all__ = ["model_union"]
