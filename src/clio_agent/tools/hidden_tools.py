"""Server tools a connector does not offer (``hide_tools`` in its entry).

A connector entry can leave out tools of the server it runs, by glob pattern
(``sim_*``): the KiCad connector, for example, offers circuit simulation through the
spice connector, not the server's own. The connector's proxy then lists none of them
and refuses each by name, so they reach no agent. Nothing is matched against what a
tool says or does; only its name, against the patterns the entry declares.
"""

from __future__ import annotations

from collections.abc import Sequence
from fnmatch import fnmatchcase
from typing import Any

from fastmcp.server.transforms import Transform

from clio_agent.runtime import trace


class HiddenTools(Transform):
    """Leave out of a server the tools whose names match any pattern."""

    def __init__(self, patterns: Sequence[str], *, connector: str = "") -> None:
        self.patterns = tuple(patterns)
        self.connector = connector

    def __repr__(self) -> str:
        return f"HiddenTools({', '.join(self.patterns)})"

    def hides(self, name: str) -> bool:
        return any(fnmatchcase(name, pattern) for pattern in self.patterns)

    async def list_tools(self, tools: Sequence[Any]) -> Sequence[Any]:
        kept = [tool for tool in tools if not self.hides(tool.name)]
        if len(kept) != len(tools):
            trace.event(
                "TOOLS",
                "connector_tools_hidden connector=%s hidden=%d patterns=%s",
                self.connector,
                len(tools) - len(kept),
                ",".join(self.patterns),
            )
        return kept

    async def get_tool(self, name: str, call_next: Any, *, version: Any = None) -> Any:
        if self.hides(name):
            return None
        return await call_next(name, version=version)


def apply[P](proxy: P, connector: str) -> P:
    """Add the connector's ``hide_tools`` to ``proxy`` and return it (unchanged when none)."""

    if not connector:
        return proxy
    from clio_agent.connectors import state  # noqa: PLC0415

    entry = state.all_entries().get(connector)
    patterns = tuple(getattr(entry, "hide_tools", ()) or ())
    if patterns:
        proxy.add_transform(HiddenTools(patterns, connector=connector))  # type: ignore[attr-defined]
    return proxy
