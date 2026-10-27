"""Session-topology substrate: who descends from whom, and by which record.

Core task snapshots identify delegated children and their assignments. Session
metadata also identifies user forks through ``parent_session_id``. This read-only
walk combines those edges for interactions, provenance and artifact aggregation;
it does not create tasks or decide their lifecycle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


#: Historical import alias; new admission reads agent_tasks.max_depth dynamically.
MAX_SPAWN_DEPTH = 8

#: Existing lineage is never hidden by the configured limit for new work.
_DEFAULT_DESCENDANT_DEPTH = None

#: A descendant delegated by a durable Core child assignment.
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


def _session_store_index(app: "FastAPI") -> dict[str, list[str]]:
    """Build the store's parent -> children index ONCE, for a whole walk.

    Asking the store for one session's children per parent per depth made the walk
    O(depth x frontier x sessions) -- on the interactions poll, which is the very
    cost this walk exists to bound. The store answers from its parent column alone,
    without reading any session row.
    """

    sessions = getattr(app.state, "sessions", None)
    if sessions is None:
        return {}
    return sessions.child_ids_by_parent()


def descendant_walk(
    app: "FastAPI", root_session_id: str, *, max_depth: int | None = _DEFAULT_DESCENDANT_DEPTH
) -> tuple[list[SessionDescendant], bool]:
    """Read delegated Core lineage and independent session-fork topology."""
    history = app.state.task_history
    children = _session_store_index(app)
    delegated: dict[tuple[str, str], str] = {}
    for handle in history.tree_handles(root_session_id):
        task = history.task_snapshot(handle)
        if task is None or task.kind != "Subagent" or task.owner.parent_handle is None:
            continue
        parent = history.task_snapshot(task.owner.parent_handle)
        if parent is not None:
            key = (parent.owner.session_id, task.owner.session_id)
            delegated[key] = task.handle
            children.setdefault(key[0], []).append(key[1])
    rows, pending, visited, truncated = [], [(root_session_id, 0)], {root_session_id}, False
    for parent, depth in pending:
        for child in dict.fromkeys(children.get(parent, ())):
            if child in visited:
                continue
            if max_depth is not None and depth >= max_depth:
                truncated = True
                continue
            visited.add(child)
            handle = delegated.get((parent, child), "")
            rows.append(
                SessionDescendant(
                    child,
                    parent,
                    depth + 1,
                    AGENT_TASK_ATTRIBUTION if handle else SESSION_FORK_ATTRIBUTION,
                    handle,
                )
            )
            pending.append((child, depth + 1))
    return rows, truncated


def descendant_sessions(
    app: "FastAPI", root_session_id: str, *, max_depth: int | None = _DEFAULT_DESCENDANT_DEPTH
) -> list[SessionDescendant]:
    """The rows-only view of :func:`descendant_walk`."""

    return descendant_walk(app, root_session_id, max_depth=max_depth)[0]


def descendant_session_ids(
    app: "FastAPI", root_session_id: str, *, max_depth: int | None = _DEFAULT_DESCENDANT_DEPTH
) -> list[str]:
    """The id-only view of :func:`descendant_sessions`, in the same order.

    This is the substrate for parent-orchestrator provenance aggregation (GAP B,
    S5 #971): a parent session whose children executed the tools can merge their
    transform/artifact records with per-row session attribution.
    """

    return [
        row.session_id for row in descendant_sessions(app, root_session_id, max_depth=max_depth)
    ]
