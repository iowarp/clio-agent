"""Bring a server's ``tools/list_changed`` into the agent's tool list.

An MCP server may add or remove tools while connected (googlecolab/colab-mcp unlocks
the notebook tools once the Colab tab connects). The executor re-lists such a
namespace over its persistent connection
(:meth:`~clio_agent.tools.mcp_namespace_executor.SyncNamespacePreparationMixin.
refresh_changed_namespaces`); this module carries the result to the agent:

* at turn start, :func:`refresh_executor_tools` runs before the builder lists tools, so
  the next turn's agent is built with the current list;
* within a turn, the agent loop calls the :class:`LiveToolRefresher` attached to its
  program before every model request, so a tool unlocked by this step's call is
  offered at the next step.

Only namespaces the agent takes whole (its plugins' MCP servers and, for a root
agent, the always-load services) are refreshed: a namespace it was granted one tool of
never grows into the rest. Every change and failure is a typed trace reason.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass, field
from typing import Any

from clio_agent.runtime import trace

#: The agent's tool list changed after a refresh (names added / removed).
MCP_TOOLS_REACHED_AGENT = "mcp_tools_reached_agent"


@dataclass
class LiveToolScope:
    """What a built agent may refresh: its executor and the namespaces it takes whole."""

    executor: Any = None
    namespaces: set[str] = field(default_factory=set)


def refresh_executor_tools(executor: Any) -> None:
    """Re-list the executor's changed namespaces now (a no-op when none changed).

    A server that stops answering (the typed ``NoProgressTimeout``) or an executor
    closed under the call is recorded and the agent keeps its current tools; per-server
    re-list failures are already recorded inside the executor.
    """

    from clio_agent.tools.mcp_tools_changed import MCP_TOOLS_REFRESH_FAILED  # noqa: PLC0415

    refresh = getattr(executor, "refresh_changed_namespaces", None)
    if not callable(refresh):
        return
    try:
        refresh()
    except (TimeoutError, RuntimeError) as exc:  # a stalled server / a closed executor
        trace.event("TOOLS", "%s scope=executor error=%r", MCP_TOOLS_REFRESH_FAILED, exc)


def _wrap_for_agent(tools: list[Any]) -> list[Any]:
    """Wrap executor tools exactly as the builder does (recording, then instrumentation)."""

    from clio_agent.gact.agents.tool_instrumentation import instrument_tools  # noqa: PLC0415
    from clio_agent.gact.agents.tool_recording import recording_tool  # noqa: PLC0415

    return instrument_tools([recording_tool(tool) for tool in tools])


class LiveToolRefresher:
    """Apply an executor's refreshed namespace tools to one agent's tool dictionary."""

    def __init__(
        self,
        executor: Any,
        namespaces: set[str],
        wrap: Callable[[list[Any]], list[Any]] = _wrap_for_agent,
    ) -> None:
        self._executor = executor
        self._prefixes = tuple(f"{namespace}_" for namespace in sorted(namespaces))
        self._wrap = wrap
        self._revision = int(getattr(executor, "tools_revision", 0))

    def __deepcopy__(self, memo: dict[int, Any]) -> "LiveToolRefresher":
        """A per-rollout copy (DSPy deep-copies modules) shares the executor, never clones it."""

        return copy.copy(self)

    def __call__(self, tools: MutableMapping[str, Any]) -> bool:
        """Update ``tools`` in place; return whether its names changed."""

        refresh_executor_tools(self._executor)
        revision = int(getattr(self._executor, "tools_revision", 0))
        if revision == self._revision:
            return False
        self._revision = revision
        current = [
            tool
            for tool in self._executor.to_dspy_tools()
            if str(getattr(tool, "name", "") or "").startswith(self._prefixes)
        ]
        names = {str(tool.name) for tool in current}
        removed = sorted(n for n in tools if n.startswith(self._prefixes) and n not in names)
        new = [tool for tool in current if tool.name not in tools]
        for name in removed:
            del tools[name]
        for tool in self._wrap(new):
            tools[tool.name] = tool
        if "submit" in tools:
            tools["submit"] = tools.pop("submit")  # the final-output tool stays last
        added = sorted(str(tool.name) for tool in new if tool.name in tools)
        if not added and not removed:
            return False
        trace.event(
            "TOOLS",
            "%s added=%s removed=%s",
            MCP_TOOLS_REACHED_AGENT,
            ",".join(added) or "-",
            ",".join(removed) or "-",
        )
        return True


def live_tool_refresher(scope: LiveToolScope) -> LiveToolRefresher | None:
    """The refresher for a built agent, or ``None`` when it takes no namespace whole."""

    if scope.executor is None or not scope.namespaces:
        return None
    if not hasattr(scope.executor, "refresh_changed_namespaces"):
        return None
    return LiveToolRefresher(scope.executor, scope.namespaces)


__all__ = [
    "MCP_TOOLS_REACHED_AGENT",
    "LiveToolRefresher",
    "LiveToolScope",
    "live_tool_refresher",
    "refresh_executor_tools",
]
