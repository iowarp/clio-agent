"""Fork families on the task layer (owner module).

A fork is an ordinary task of its original's kind (D1: no new ``TaskKind``) whose
``fork_of`` names the handle it was forked from; it takes its original's
``parent_handle``, so it runs beside the original under the same parent. One rule
covers every place a fork lands: **waiting on a handle waits on it and on every
task whose** ``fork_of`` **chain reaches it**, and the wait's outcomes list the whole
family, each labelled by handle. A dependent of a workflow step waits on the step's
handle, so it waits on the step's forks through the same rule.

The family is read from the tree's passive ``_forks`` index, which the projection
keeps from each confirmed ``task.observed`` record: nothing here is stored or
scheduled.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from clio_agent.arc.task_types import TaskSnapshot
    from clio_agent.gact.task_history import TaskHistory


def fork_family(history: TaskHistory, handle: str) -> tuple[str, ...]:
    """``handle`` followed by every task whose ``fork_of`` chain reaches it, in the
    order the forks were created (the order ``<step>~1``, ``<step>~2`` label them)."""

    with history.serialized():
        state = history._handle_state(handle)
        if state is None:
            return (handle,)
        found: list[str] = []
        frontier = [handle]
        while frontier:
            current = frontier.pop()
            for fork in state._forks.get(current, ()):
                if fork not in found and fork != handle:
                    found.append(fork)
                    frontier.append(fork)

        def created(fork: str) -> tuple[str, str]:
            task = state._tasks.get(fork)
            return (task.created_at if task is not None else "", fork)

        return (handle, *sorted(found, key=created))


def fork_ancestors(history: TaskHistory, snapshot: TaskSnapshot) -> tuple[str, ...]:
    """The handles ``snapshot``'s ``fork_of`` chain passes through, nearest first."""

    chain: list[str] = []
    current = snapshot.fork_of
    while current and current not in chain:
        chain.append(current)
        task = history.task_snapshot(current)
        current = task.fork_of if task is not None else None
    return tuple(chain)


def family_members(history: TaskHistory, handles: tuple[str, ...]) -> tuple[str, ...]:
    """Each waited handle followed by its forks, without repeats."""

    members: list[str] = []
    for handle in handles:
        for member in fork_family(history, handle):
            if member not in members:
                members.append(member)
    return tuple(members)


__all__ = ["family_members", "fork_ancestors", "fork_family"]
