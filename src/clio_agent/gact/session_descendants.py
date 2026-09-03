"""Session-topology substrate: who descends from whom, and by which record.

The agent-task registry and the session store are TWO substrates over one
topology, and neither alone is it: the registry knows which child an
:class:`~clio_agent.gact.agent_tasks.AgentTask` delegated but cannot see a user
FORK, while the session store's ``parent_session_id`` sees the fork but cannot
say which task owns a delegated child. Every read-side walk -- the interactions
scope, the provenance lineage, the artifact aggregation -- goes through this ONE
module so they cannot disagree about what a session's descendants are.

Owner module (#775 no-accretion): this lived inside ``agent_tasks``, which is the
record + registry + lifecycle owner and had no room for a second concern.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


#: Runaway backstop on spawn depth (NOT a 3-tier rule): ``tier`` is semantic
#: weight, not depth, so deep declared chains are legitimate. A spawn whose
#: computed depth would exceed this is refused (``spawn_depth_exceeded``). Defined
#: here, in the leaf module both the spawn path and every read-side walk import,
#: rather than in ``turn_spawn`` (which imports this module).
MAX_SPAWN_DEPTH = 8

#: Ceiling on descendant-session traversal (:func:`descendant_sessions`,
#: :func:`~clio_agent.gact.provenance.child_projection.child_session_lineage`).
#: Deliberately THE SAME constant as the spawn backstop: a walk that stopped
#: shallower would silently hide legitimate children, and one that went deeper
#: could only find a graph that spawning refuses to create. Two independent
#: numbers is how the aggregation walk and the lineage walk came to disagree
#: about what the tree even is.
_DEFAULT_DESCENDANT_DEPTH = MAX_SPAWN_DEPTH

#: A descendant a durable :class:`AgentTask` delegated.
AGENT_TASK_ATTRIBUTION = "agent_task"
#: A descendant reached only through the session store's ``parent_session_id`` --
#: a user fork, or a child whose task row is gone. Real topology that no task
#: owns: attributing it to a task (or to the root) would be a fabrication.
SESSION_FORK_ATTRIBUTION = "session_fork"


@dataclass(frozen=True)
class SessionDescendant:
    """One descendant session plus HOW it was reached."""

    session_id: str
    parent_session_id: str
    depth: int
    attribution: str
    task_id: str = ""


def child_session_ids(app: "FastAPI", parent_session_id: str) -> list[str]:
    """Return the direct child session ids a parent spawned (via the task registry).

    Each :class:`AgentTask` carries the ``child_session_id`` of the real child
    SESSION it projects (#948 S2 substrate). Empty when the registry is absent or
    the session spawned nothing.
    """
    reg = getattr(app.state, "agent_task_registry", None)
    if reg is None:
        return []
    out: list[str] = []
    for task in reg.for_parent(parent_session_id):
        child = str(getattr(task, "child_session_id", "") or "")
        if child:
            out.append(child)
    return out


def _session_store_children(app: "FastAPI", parent_session_id: str) -> list[str]:
    """Direct children of ``parent_session_id`` per the SESSION STORE's own pointer."""

    sessions = getattr(app.state, "sessions", None)
    if sessions is None:
        return []
    rows = sessions.list(workspace_id=None)
    return [
        str(getattr(row, "id", "") or "")
        for row in rows
        if str(getattr(row, "parent_session_id", "") or "") == parent_session_id
        and str(getattr(row, "id", "") or "")
    ]


def descendant_sessions(
    app: "FastAPI", root_session_id: str, *, max_depth: int = _DEFAULT_DESCENDANT_DEPTH
) -> list[SessionDescendant]:
    """Return every descendant of ``root_session_id`` (BFS, bounded), typed by origin.

    ONE walk over BOTH substrates, because neither alone is the topology: the
    agent-task registry knows which child a task delegated but cannot see a user
    FORK, and the session store's ``parent_session_id`` sees the fork but cannot
    say which task owns a delegated child. Reading only the registry is what let a
    permission raised inside a fork be invisible to every interactions poll;
    reading only the store is what made a delegated child look parentless.

    Delegated children are visited first at each depth, so a session reachable
    both ways is attributed to its task rather than to the bare pointer. The root
    is NOT included; each descendant appears once (a ``seen`` set makes a repeated
    or cyclic graph terminate); order is breadth-first, siblings newest-created
    first (the registry's ``for_parent`` order).
    """

    if max_depth < 1:
        return []
    reg = getattr(app.state, "agent_task_registry", None)
    out: list[SessionDescendant] = []
    seen: set[str] = {root_session_id}
    frontier = [root_session_id]
    depth = 0
    while frontier and depth < max_depth:
        next_frontier: list[str] = []
        for parent in frontier:
            delegated: list[tuple[str, str]] = []
            if reg is not None:
                delegated = [
                    (str(getattr(task, "child_session_id", "") or ""), task.task_id)
                    for task in reg.for_parent(parent)
                ]
            forked = [(child, "") for child in _session_store_children(app, parent)]
            for child, task_id in (*delegated, *forked):
                if not child or child in seen:
                    continue
                seen.add(child)
                out.append(
                    SessionDescendant(
                        session_id=child,
                        parent_session_id=parent,
                        depth=depth + 1,
                        attribution=(
                            AGENT_TASK_ATTRIBUTION if task_id else SESSION_FORK_ATTRIBUTION
                        ),
                        task_id=task_id,
                    )
                )
                next_frontier.append(child)
        frontier = next_frontier
        depth += 1
    return out


def descendant_session_ids(
    app: "FastAPI", root_session_id: str, *, max_depth: int = _DEFAULT_DESCENDANT_DEPTH
) -> list[str]:
    """The id-only view of :func:`descendant_sessions`, in the same order.

    This is the substrate for parent-orchestrator provenance aggregation (GAP B,
    S5 #971): a parent session whose children executed the tools can merge their
    transform/artifact records with per-row session attribution.
    """

    return [
        row.session_id for row in descendant_sessions(app, root_session_id, max_depth=max_depth)
    ]


def purge_session_tasks(app: "FastAPI", session_id: str) -> list[str]:
    """Drop the agent-task rows a deleted session leaves behind.

    The registry is a PROJECTION over the session store, so those rows are stale
    the moment the store loses the session: the lineage kept naming a session
    nothing could read, and ``for_parent`` kept handing out a task whose child is
    gone. Returns the purged task ids.
    """

    registry = getattr(app.state, "agent_task_registry", None)
    if registry is None:
        return []
    forgotten = registry.forget_session(session_id)
    if forgotten:
        logger.info(
            "agent-task rows purged with their session session=%s tasks=%s",
            session_id,
            ",".join(forgotten),
        )
    return forgotten
