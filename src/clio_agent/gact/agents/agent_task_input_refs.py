"""Task-reference evidence forwarding on spawn (#1306 review round, finding 1).

The digest half (``agent_task_output_digest.py``) keeps the PARENT lean when it
collects an oversize completed child's output. That alone forces the exact flow
#1306 named as motivating (a coordinator sending assembled researcher evidence
through an independent critic) back through the parent: it would have to
``get_agent_task_output`` every researcher and re-inline the sum right back into
its own context -- the SAME bloat, one hop later.

This module is the other half: a typed ``input_task_ids`` reference on
``spawn_agent_task`` / ``spawn_agents_parallel``'s per-spawn entries. The parent
never touches the text -- it passes ids; the runtime resolves each referenced
task's FULL stored output (the identical ``spawn_runtime._resolve_verbatim_output``
path ``wait_agent_tasks`` and ``get_agent_task_output`` already use) and appends it,
clearly labeled, to the CHILD's own task briefing. The material lands in the
child's OWN context, never the parent's -- which is the point: a critic gets full
researcher evidence without the coordinator ever holding it.

Validation is a reality check, not a decision: an id must belong to the SPAWNING
session's own task registry scope (it spawned that task) and have reached a
terminal status, or the spawn is refused typed (no child created) rather than
silently starting a critic with a missing/foreign/incomplete reference. Nothing
here decides what the child does with the evidence -- forwarding it is the whole
mechanism.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def _evidence_block(task: Any, output: str) -> str:
    """One clearly labeled evidence block: which child, which agent, which task."""

    agent_id = task.agent_ref.get("expert_id", "")
    return (
        f"--- Evidence from {agent_id} "
        f"(task {task.task_id}, child session {task.child_session_id}) ---\n"
        f"{output}\n"
        f"--- end evidence ({task.task_id}) ---"
    )


def resolve_input_task_evidence(
    app: Any,
    session_id: str,
    task_text: str,
    input_task_ids: Iterable[str] | None,
) -> str:
    """Append each referenced task's full output to ``task_text`` as evidence.

    Validates every id BEFORE building anything: unknown, not this session's own,
    or not yet terminal each raise a typed :class:`SpawnError` (reasons
    ``task_ref_unknown`` / ``task_ref_not_yours`` / ``task_ref_not_terminal``) so
    the caller (``spawn_runtime._do_spawn``, inside its existing try/except
    SpawnError block) refuses the WHOLE spawn -- no child is ever created on a
    broken reference, and a batch sibling's slot still reconciles the same way
    any other refused spawn does.

    Args:
        app: The active CLIO app (carries ``agent_task_registry``).
        session_id: The SPAWNING session's id -- a referenced task must be one
            IT spawned (``task.parent_session_id == session_id``).
        task_text: The child's task briefing before evidence is appended.
        input_task_ids: Optional task ids (from the spawning session's own
            prior spawns) whose full output to forward.

    Returns:
        ``task_text`` unchanged when ``input_task_ids`` is empty/``None``, else
        ``task_text`` followed by one labeled evidence block per referenced task,
        in the order given.

    Raises:
        SpawnError: on the first invalid reference (see reasons above).
    """

    ids = [str(t) for t in (input_task_ids or []) if str(t)]
    if not ids:
        return task_text

    from clio_agent.gact.agents.invoker import SpawnError  # noqa: PLC0415
    from clio_agent.gact.agents.spawn_runtime import (  # noqa: PLC0415
        _resolve_verbatim_output,
    )

    registry = app.state.agent_task_registry
    blocks: list[str] = []
    for tid in ids:
        task = registry.get(tid)
        if task is None:
            raise SpawnError(f"referenced task {tid!r} is unknown", reason="task_ref_unknown")
        if task.parent_session_id != session_id:
            raise SpawnError(
                f"referenced task {tid!r} was not spawned by this session",
                reason="task_ref_not_yours",
            )
        if not task.is_terminal:
            raise SpawnError(
                f"referenced task {tid!r} has not completed yet (status={task.status!r})",
                reason="task_ref_not_terminal",
            )
        output, _markers = _resolve_verbatim_output(app, task)
        blocks.append(_evidence_block(task, output))
    return task_text + "\n\n" + "\n\n".join(blocks)
