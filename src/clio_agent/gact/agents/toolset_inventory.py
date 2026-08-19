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
  provenance registry. P5 adversarial review [B]: this was formerly a
  MODULE-GLOBAL dict keyed by tool name only. Child turns build concurrently
  on a ThreadPoolExecutor and external-MCP tool names are not namespaced at
  registration, so two concurrent builds with a colliding tool name could
  race and hand one build the OTHER build's source. There is no shared
  registry anymore -- every construction site owns its OWN per-call ``dict``
  (built fresh in ``builders.py`` and threaded through explicitly), so two
  concurrent builds never share mutable state.
* :func:`emit_agent_toolset_recorded` -- fires ONE ``agent.toolset.recorded``
  semantic-trace event per built react expert, carrying the FINAL
  instrumented toolset (name/title/source/representation per tool). Rides
  the existing session trace surface (``GET /v1/sessions/{sid}/trace``) the
  obs UI already polls for parent + children -- zero new routes, zero new
  stores. A build with no reachable app/session context leaves no event
  (an honest gap the UI shows), but the miss is always logged, never a
  silent pass (no-silent-fallback ground rule); when ``app.state`` IS
  reachable the reason also lands in the structured catalog below (finding
  [F]) so it is queryable after the fact, not just a CLIO_DEBUG log line.
  Finding [C]: ``turn_forward`` rebuilds a fresh module every turn (and the
  stream_fallback compat path rebuilds AGAIN in the same turn), so an
  IDENTICAL toolset is recorded at most once per (session, agent) pair --
  an unchanged rebuild emits nothing, holding an N-turn session to O(experts)
  events instead of O(turns x experts).
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from clio_agent.gact import context as _ctx
from clio_agent.runtime import trace

if TYPE_CHECKING:
    from clio_agent.gact.types import AgentDef

# Guards mutation of the two small per-app dicts below (the built toolset
# fingerprint cache + the structured reason catalog). Builds race on a
# ThreadPoolExecutor; each dict is keyed by (session_id, agent_id) / session_id
# so distinct sessions never collide, but concurrent SAME-key writers still
# need the lock for a correct read-then-write.
_STATE_LOCK = threading.Lock()


def register_tool_source(sources: dict[str, str], name: object, source: str) -> None:
    """Record one tool's provenance, into THIS build's per-call ``sources`` dict
    (never a shared/global registry -- see the module docstring, finding [B])."""

    clean = str(name or "").strip()
    if clean:
        sources[clean] = str(source or "").strip() or "unknown"


def declared_tool_source(sources: dict[str, str], name: str) -> str:
    """The provenance registered for ``name`` in THIS build's ``sources`` dict
    (``"unknown"`` when never registered)."""

    return sources.get(str(name or "").strip(), "unknown")


def register_tool_sources(sources: dict[str, str], tools: Iterable[Any], source: str) -> None:
    """Register the SAME provenance for every tool in one already-built sub-list
    (e.g. the whole spawn-runtime or native auto-attached set), into THIS
    build's ``sources`` dict."""

    for tool in tools:
        register_tool_source(sources, getattr(tool, "name", ""), source)


def mounted_namespace_set(tool_executor: Any) -> set[str]:
    """The namespace set the executor's real dispatcher validates a namespaced
    call against (``mcp_executor.AsyncMCPToolExecutor._route``, read-only --
    that module is owned elsewhere and never edited here). A gateway-mounted
    tool's name-prefix provenance is trustworthy ONLY when it names a namespace
    THIS set actually mounts (finding [D]); anything else is a fabricated guess."""

    async_executor = getattr(tool_executor, "_async_executor", tool_executor)
    namespaces = getattr(async_executor, "_namespace_servers", None)
    return {str(k) for k in namespaces} if isinstance(namespaces, Mapping) else set()


def _toolset_fingerprint(rows: list[dict[str, Any]]) -> frozenset[tuple[tuple[str, Any], ...]]:
    """Order-insensitive identity of a built toolset's rows (finding [C]): two
    builds that produced the SAME tools (any order) fingerprint equal, so a
    same-turn rebuild (or an unchanged next-turn rebuild) is detected as a
    duplicate regardless of dict/list construction order."""

    return frozenset(tuple(sorted(row.items())) for row in rows)


def _toolset_cache(app: Any) -> dict[tuple[str, str], Any]:
    """The per-app ``(session_id, agent_id) -> last recorded fingerprint`` cache
    (finding [C]). A cheap in-process cache on ``app.state`` -- no new store
    (RULE 4); lost on restart, which only means the first post-restart build
    re-emits once (harmless -- the honest state of "nothing recorded yet")."""

    cache = getattr(app.state, "toolset_inventory_last", None)
    if not isinstance(cache, dict):
        cache = {}
        app.state.toolset_inventory_last = cache
    return cache


def _reason_catalog(app: Any) -> dict[str, list[dict[str, Any]]]:
    """The per-app, per-session structured skip/emit-failure reason catalog
    (finding [F]), patterned on ``gact/streaming.py``'s ``_stream_fallback_reasons``:
    a typed reason recorded per session, queryable after the fact -- never only a
    CLIO_DEBUG-gated log line."""

    reasons = getattr(app.state, "toolset_inventory_reasons", None)
    if not isinstance(reasons, dict):
        reasons = {}
        app.state.toolset_inventory_reasons = reasons
    return reasons


def _record_reason(app: Any, sid: str, agent_id: str, reason: str, detail: str = "") -> None:
    """Append one typed reason row to the structured catalog for ``sid`` (finding [F])."""

    row: dict[str, Any] = {"reason": reason, "agent_id": agent_id}
    if detail:
        row["detail"] = detail
    with _STATE_LOCK:
        _reason_catalog(app).setdefault(sid, []).append(row)


def toolset_inventory_reasons(app: Any, sid: str) -> list[dict[str, Any]]:
    """Public read accessor for ``sid``'s recorded skip/emit-failure reasons
    (finding [F]) -- e.g. for a diagnostics route or a test assertion."""

    return list(_reason_catalog(app).get(sid, []))


def record_tool_unavailable(app: Any, sid: str, agent_id: str, tool_name: str) -> None:
    """Record ONE ACL-requested tool this build could not project (#1228 D3).

    Called only when the agent still resolves >=1 OTHER requested tool, so
    the build is not bricked over a single catalog-side gap. Loud, not
    silent: reaches this module's reason catalog (patterned on
    ``gact/streaming.py``'s ``_stream_fallback_reasons``) and the trace.
    """

    _record_reason(app, sid, agent_id, "custom_agent_tool_unavailable", tool_name)
    trace.event(
        "TOOLSET-INVENTORY",
        "custom_agent_tool_unavailable agent_id=%s tool=%s -- degraded, not bricked",
        agent_id,
        tool_name,
    )


def record_tools_unavailable_degraded(app: Any | None, agent_id: str, missing: list[str]) -> None:
    """Record EACH of ``missing`` as a typed, loud per-tool absence (#1228 D3).

    The owner-module half of ``builders._dynamic_agent_tools``'s degrade
    path: called once at least one OTHER requested tool resolved. An
    app-less build (out-of-band caller, no session) has no reason catalog to
    write into, so it logs to the trace directly instead of silently
    dropping the omission.
    """

    if app is not None:
        sid = _ctx.active_session_id() or ""
        for name in missing:
            record_tool_unavailable(app, sid, agent_id, name)
        return
    trace.event(
        "TOOLSET-INVENTORY",
        "custom_agent_tool_unavailable agent_id=%s tools=%s reason=no_app",
        agent_id,
        ",".join(missing),
    )


def emit_agent_toolset_recorded(
    agent_def: "AgentDef", tools: list[Any], sources: dict[str, str]
) -> None:
    """Record one built react expert's REAL effective toolset on the trace highway.

    Call exactly once, right after ``instrument_tools()`` hands back the FINAL
    instrumented tool list -- the exact toolset the model can call this build,
    never a recomputed approximation that could drift from it. ``sources`` is
    THIS build's per-call provenance dict (finding [B]), populated at the
    construction sites in ``builders.py``.
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
        if app is not None:
            # app.state IS reachable even with no bound session id -- record the
            # structured reason (finding [F]). An app-less build (below) has no
            # app.state to record into: THAT is the one branch the catalog
            # genuinely cannot reach, so the CLIO_DEBUG log stays its only trace.
            _record_reason(app, "", agent_id, "no_session")
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
                "source": declared_tool_source(sources, name),
                "representation": declared_tool_representation(name),
            }
        )
    fingerprint = _toolset_fingerprint(rows)
    key = (sid, agent_id)
    with _STATE_LOCK:
        if _toolset_cache(app).get(key) == fingerprint:
            # Identical rebuild (finding [C]): turn_forward rebuilds fresh every
            # turn, and the stream_fallback compat path rebuilds again in the
            # SAME turn -- an unchanged toolset must not re-emit.
            return
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
        _record_reason(app, sid, agent_id, "emit_failed", repr(exc))
        trace.event(
            "TOOLSET-INVENTORY",
            "agent.toolset.recorded emit failed for %s: %r",
            agent_id,
            exc,
        )
        return
    with _STATE_LOCK:
        _toolset_cache(app)[key] = fingerprint


__all__ = [
    "declared_tool_source",
    "emit_agent_toolset_recorded",
    "mounted_namespace_set",
    "record_tool_unavailable",
    "record_tools_unavailable_degraded",
    "register_tool_source",
    "register_tool_sources",
    "toolset_inventory_reasons",
]
