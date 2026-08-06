"""Built-toolset provenance + trace emission for the obs Tools tab.

The obs Tools tab gets a "called | available" toggle (gact-tui): "called" is
the existing rows-of-calls-made view; "available" is the tool surface
AVAILABLE to the agent, rendered from a server-provided inventory VERBATIM
(no client-side composition or inference). This owner module is that
server half -- carved out of ``builders.py`` (no-accretion ground rule,
cleanup program #775) so the two ``instrument_tools()`` assembly-seam call
sites there only need a couple of one-line calls into it.

Two responsibilities, both populated/read at ``builders.py``'s tool
construction sites -- the ONLY place a tool's real origin is known, so this
module never re-derives or guesses it after the fact:

* :func:`register_tool_source` / :func:`declared_tool_source` -- a name ->
  provenance registry (mirrors ``tool_instrumentation._TOOL_PRESENTATIONS``:
  tool names are stable process-wide, so a plain module dict is the
  registry; no new persistent store, RULE 4).
* :func:`emit_agent_toolset_recorded` -- fires ONE ``agent.toolset.recorded``
  semantic-trace event per built react expert, carrying the FINAL
  instrumented toolset (name/title/source/representation per tool). Rides
  the existing session trace surface (``GET /v1/sessions/{sid}/trace``) the
  obs UI already polls for parent + children -- zero new routes, zero new
  stores. A build with no reachable app/session context leaves no event
  (an honest gap the UI shows), but the miss is always logged, never a
  silent pass (no-silent-fallback ground rule).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from clio_agent.gact import context as _ctx
from clio_agent.runtime import trace

if TYPE_CHECKING:
    from clio_agent.gact.types import AgentDef

# name -> a source label that is EITHER the concrete MCP server/namespace
# name or one of the two literal buckets "native" / "spawn-runtime".
_TOOL_SOURCES: dict[str, str] = {}


def register_tool_source(name: object, source: str) -> None:
    """Record one tool's provenance at its construction site (``builders.py``)."""

    clean = str(name or "").strip()
    if clean:
        _TOOL_SOURCES[clean] = str(source or "").strip() or "unknown"


def declared_tool_source(name: str) -> str:
    """The registered provenance for ``name`` (``"unknown"`` when never registered)."""

    return _TOOL_SOURCES.get(str(name or "").strip(), "unknown")


def register_tool_sources(tools: Iterable[Any], source: str) -> None:
    """Register the SAME provenance for every tool in one already-built sub-list
    (e.g. the whole spawn-runtime or native auto-attached set)."""

    for tool in tools:
        register_tool_source(getattr(tool, "name", ""), source)


def emit_agent_toolset_recorded(agent_def: "AgentDef", tools: list[Any]) -> None:
    """Record one built react expert's REAL effective toolset on the trace highway.

    Call exactly once, right after ``instrument_tools()`` hands back the FINAL
    instrumented tool list -- the exact toolset the model can call this build,
    never a recomputed approximation that could drift from it.
    """

    from clio_agent.gact.agents.tool_instrumentation import (  # noqa: PLC0415
        declared_tool_representation,
        declared_tool_title,
    )
    from clio_agent.gact.runtime.globals import (  # noqa: PLC0415
        _active_semantic_trace_id,
        _active_semantic_turn_id,
        _emit_semantic_event,
    )

    agent_id = str(getattr(agent_def, "id", "") or "")
    app = _ctx.active_app()
    sid = _ctx.active_session_id()
    if app is None or not sid:
        trace.event(
            "TOOLSET-INVENTORY",
            "agent.toolset.recorded skipped reason=no_app_or_session agent_id=%s",
            agent_id,
        )
        return
    rows: list[dict[str, Any]] = []
    for tool in tools:
        name = str(getattr(tool, "name", "") or "").strip()
        if not name:
            continue
        rows.append(
            {
                "name": name,
                "title": declared_tool_title(name),
                "source": declared_tool_source(name),
                "representation": declared_tool_representation(name),
            }
        )
    try:
        _emit_semantic_event(
            app,
            sid,
            "agent.toolset.recorded",
            turn_id=_active_semantic_turn_id(),
            trace_id=_active_semantic_trace_id(),
            status="completed",
            summary=f"{agent_id or '?'} built with {len(rows)} tool(s)",
            actor={"agent_id": agent_id, "role": "expert"},
            payload={"agent_id": agent_id, "session_id": sid, "tools": rows},
        )
    except Exception as exc:  # noqa: BLE001 - capture must never break the build
        trace.event(
            "TOOLSET-INVENTORY",
            "agent.toolset.recorded emit failed for %s: %r",
            agent_id,
            exc,
        )
