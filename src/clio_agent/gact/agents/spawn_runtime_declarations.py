"""Tool declarations for the spawn-runtime surface (#1333 ratchet payment).

Split out of ``spawn_runtime.py``: the actual tool-list assembly (names,
descriptions, JSON-schema args) for the react-main spawn tools, plus the one
pure helper (``_failed_spawn_handoff_part``) ``build_spawn_runtime_tools`` calls
to report a batch sibling refused before it ever spawned. The closures
themselves (``spawn_agent_task``, ``wait_agent_tasks``, ...) stay in
``spawn_runtime.py`` — this module owns only their WIRE presentation.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from clio_agent.gact.agents.blueprint_commission import (
    SPAWN_AGENT_ARGUMENT,
    SPAWN_BLUEPRINT_ARGUMENT,
)
from clio_agent.gact.agents.spawn_group import failed_spawn_metadata_row
from clio_agent.gact.tool_observer import _handoff_part_metadata
from clio_agent.gact.types import Part

if TYPE_CHECKING:
    from clio_agent.gact.agents.types import AgentDef


def _failed_spawn_handoff_part(
    agent_def: "AgentDef",
    child_id: str,
    task: str,
    spawn_group_id: str,
    group_size: int,
    exc: Exception,
) -> Part:
    """Terminal Part for a batch sibling refused before it ever spawned (finding
    [E]): builds directly on the terminal lane so the group's declared total
    always reconciles even when one sibling never got a child session."""

    reason = getattr(exc, "reason", type(exc).__name__)
    error_message = (
        "This child is not declared by the current agent, so it was not started."
        if reason == "undeclared_child"
        else f"This child was not started because the spawn request failed with {reason}."
    )
    row = failed_spawn_metadata_row(
        child_id,
        agent_def.id,
        reason,
        spawn_group_id,
        group_size,
        task=task,
        error_message=error_message,
    )
    return Part(
        id=f"live_handoff_{uuid.uuid4().hex[:12]}",
        type="expert_handoff",
        agent_id=agent_def.id,
        parent_agent=agent_def.id,
        child_agent=child_id,
        stage="delegate.completed",
        status="failed",
        text=f"{agent_def.id} -> {child_id}",
        metadata={**_handoff_part_metadata(row), "stream_source": "live"},
    )


def assemble_spawn_runtime_tools(
    agent_def: "AgentDef",
    *,
    spawn_agent_task: Callable[..., str],
    wait_agent_tasks: Callable[..., str],
    spawn_agents_parallel: Callable[..., str],
    run_workflow: Callable[..., str],
    has_declared_children: bool,
    can_commission_blueprints: bool,
) -> list[Any]:
    """Build the declared tool list ``build_spawn_runtime_tools`` returns.

    The four callables are the closures built there (bound to the requesting
    ``agent_def`` and the active app/session); this function owns only their
    name/description/JSON-schema-args wire declaration and the children-gated
    / workflow-gated filtering.
    """

    from clio_agent.gact.agent_messaging import build_message_agent_tool  # noqa: PLC0415
    from clio_agent.gact.agents.agent_task_output_digest import (  # noqa: PLC0415
        build_agent_task_output_tool,
    )
    from clio_agent.gact.agents.native_presenters import build_wait_tool  # noqa: PLC0415
    from clio_agent.gact.agents.observe_runtime import build_observe_tool  # noqa: PLC0415
    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415
    from clio_agent.gact.workflows import parse_workflow  # noqa: PLC0415

    # Declared presentation (tool_instrumentation): spawn/fan-out are represented
    # by their individual ``expert_handoff`` parts. A declared workflow is a
    # compound operation spanning several handoffs, so it keeps its own tool row
    # as the operation boundary and the handoffs remain its ordered body. The
    # collectors are plain ``row`` tools as well: a wait or status check is a real
    # call, never invisible mechanism the narration references.
    tools: list[Any] = [
        native_tool(
            spawn_agent_task,
            name="spawn_agent_task",
            presentation="specialized",
            desc=spawn_agent_task.__doc__,
            title="Spawn Agent",
            representation="handoff",
            args={
                "agent": SPAWN_AGENT_ARGUMENT,
                "task": {"type": "string", "description": "The specific task for that child."},
                "placement": {
                    "type": "string",
                    "description": (
                        "Optional execution placement: local or relay:<cluster>. "
                        "Omit to use the session policy, then the local default."
                    ),
                },
                "input_task_ids": {
                    "type": "array",
                    "description": (
                        "Optional ids of YOUR OWN already-finished spawned tasks whose "
                        "full stored output to hand this child as labeled evidence in "
                        "its own briefing (e.g. a critic reviewing researchers' full "
                        "material) -- the parent never sees this text. A foreign, "
                        "unknown, or still-running id refuses the spawn (typed reason; "
                        "no child created)."
                    ),
                },
                "blueprint_id": SPAWN_BLUEPRINT_ARGUMENT,
            },
        ),
        build_wait_tool(wait_agent_tasks),
        build_message_agent_tool(agent_def),
        # OBSERVE posture (#1000): the read-only child-progress surface, built in
        # its owner module (observe_runtime) so this file stays under the size ratchet.
        build_observe_tool(),
        # #1306 recoverability: fetches a digested (oversize) completed task's full
        # stored output on demand; built in its own owner module for the same reason.
        build_agent_task_output_tool(),
        native_tool(
            spawn_agents_parallel,
            name="spawn_agents_parallel",
            presentation="specialized",
            desc=spawn_agents_parallel.__doc__,
            title="Spawn Agents",
            representation="handoff",
            args={
                "spawns": {
                    "type": "array",
                    "description": (
                        "List of {agent, task, input_task_ids?, blueprint_id?} to fan out. "
                        "input_task_ids works exactly like spawn_agent_task's own "
                        "parameter, per entry."
                    ),
                },
                "placement": {
                    "type": "string",
                    "description": ("Optional placement applied to every spawn in this batch."),
                },
            },
        ),
    ]
    if not has_declared_children:
        collection_names = {
            "wait_agent_tasks",
            "message_agent",
            "observe_agent_tasks",
            "get_agent_task_output",
            *(
                {"spawn_agent_task", "spawn_agents_parallel"}
                if can_commission_blueprints
                else set()
            ),
        }
        tools = [tool for tool in tools if getattr(tool, "name", "") in collection_names]

    # run_workflow is gated on a DECLARED workflow (mirroring the children-gated
    # toolset above): a blueprint with no ``workflow:`` block never sees the tool.
    if has_declared_children and parse_workflow(agent_def) is not None:
        tools.append(
            native_tool(
                run_workflow,
                name="run_workflow",
                presentation="specialized",
                desc=run_workflow.__doc__,
                title="Run Workflow",
                args={
                    "request": {
                        "type": "string",
                        "description": "The user's request, grounding each declared step's task.",
                    },
                },
            )
        )
    return tools
