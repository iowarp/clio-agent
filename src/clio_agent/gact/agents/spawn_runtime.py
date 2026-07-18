"""Spawn-runtime tools for react mains (#948 S4).

The routing surface that REPLACES the settle/synthesis orchestration + the inline
``delegate_to_<child>`` / ``fanout_to_children`` tools. A tier-1 main is now a react
agent whose answer IS the user deliverable; instead of emitting a ``next_expert``
route consumed by a settle loop, it CALLS these tools:

* ``spawn_agent_task(agent, task)`` — spawn a declared child as a REAL child turn
  (S3 ``spawn_child_turn``, on the dedicated executor) and return its ``task_id``.
* ``wait_agent_tasks(task_ids, timeout_s)`` — block on the children's completion
  Events and return their results (spawn + wait COMPOSE the old synchronous
  delegate; the child runs on the dedicated pool so the waiting parent thread can
  never starve it).
* ``check_agent_tasks()`` — the parent's spawned tasks + their status.
* ``spawn_agents_parallel(spawns)`` — fan out several children at once (replaces
  ``fanout_to_children``).

Each tool re-emits the wire-facing ``blueprint.delegation.*`` / ``blueprint.fanout.*``
events the old tools emitted, so TUI handoff rendering stays lit (wire parity).
The child sessions + AgentTask records are the real substrate underneath — no
inline in-thread child forward, no ``next_expert`` vocabulary.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from clio_agent.gact import context as _ctx
from clio_agent.gact.runtime.globals import (
    _active_semantic_trace_id,
    _active_semantic_turn_id,
    _emit_semantic_event,
)

if TYPE_CHECKING:
    from clio_agent.gact.agents.types import AgentDef

# Bounded wait so a stuck child never wedges the parent's react loop forever; the
# model passes its own timeout and decides how to proceed on a partial result.
_DEFAULT_WAIT_TIMEOUT_S = 300.0


def _blueprint_block(parent: "AgentDef", child_id: str) -> dict[str, str]:
    return {
        "agent_blueprint_id": parent.metadata.get("agent_blueprint_id") or "",
        "parent_expert": parent.id,
        "child_expert": child_id,
    }


def _completion_payload(task: Any) -> dict[str, Any]:
    """The delegate.completed payload shape (wire parity with the old tool)."""

    result = task.result or {}
    return {
        "agent_id": task.agent_ref.get("expert_id", ""),
        "parent_id": task.agent_ref.get("requesting_expert_id", ""),
        "task_id": task.task_id,
        "status": task.status,
        "stage": "delegate.completed" if task.status == "completed" else f"delegate.{task.status}",
        "output": result.get("answer_excerpt", ""),
        "workflow_state": result.get("workflow_state", {}),
        "message_ref": result.get("message_ref", ""),
        "error_reason": task.error_reason,
    }


def build_spawn_runtime_tools(base_agent: Any, agent_def: "AgentDef") -> list[Any]:
    """Build the react-main spawn tools bound to ``agent_def`` as the requesting
    (parent) expert. Resolved lazily against the active app/session at call time."""

    import dspy  # noqa: PLC0415

    from clio_agent.gact.agents.resolution import _runtime_declared_child_ids  # noqa: PLC0415
    from clio_agent.gact.turn_spawn import (  # noqa: PLC0415
        SpawnError,
        TaskSpec,
        spawn_child_turn_threadsafe,
    )

    # Only an agent with DECLARED children gets the routing surface — a leaf expert
    # has nothing to spawn (and spawn would reject an undeclared child anyway).
    _app = _ctx.active_app()
    _sid = _ctx.active_session_id()
    if _app is None or not _runtime_declared_child_ids(_app, agent_def.id, session_id=_sid):
        return []

    def _ctx_app_session() -> tuple[Any, str]:
        app = _ctx.active_app()
        session_id = _ctx.active_session_id()
        if app is None or not session_id:
            raise RuntimeError("spawn-runtime tool requires an active CLIO app/session context")
        return app, session_id

    def spawn_agent_task(agent: str, task: str) -> str:
        """Spawn a declared child expert as a background child turn; returns its
        task_id (use wait_agent_tasks to collect its result)."""

        app, session_id = _ctx_app_session()
        try:
            spawned = spawn_child_turn_threadsafe(
                app,
                TaskSpec(
                    child_expert_id=agent,
                    task_text=task,
                    parent_session_id=session_id,
                    requesting_expert_id=agent_def.id,
                    mode="sync",
                ),
            )
        except SpawnError as exc:
            return json.dumps({"error": exc.reason, "message": str(exc)}, sort_keys=True)
        _emit_semantic_event(
            app,
            session_id,
            "blueprint.delegation.started",
            turn_id=_active_semantic_turn_id(),
            trace_id=_active_semantic_trace_id(),
            status="running",
            summary=f"{agent_def.id} spawned {agent}",
            actor={"agent_id": agent_def.id, "role": "parent_expert"},
            subject={"agent_id": agent, "role": "child_expert"},
            blueprint=_blueprint_block(agent_def, agent),
        )
        return json.dumps({"task_id": spawned.task_id, "status": spawned.status}, sort_keys=True)

    def wait_agent_tasks(task_ids: list[str], timeout_s: float = _DEFAULT_WAIT_TIMEOUT_S) -> str:
        """Block until the given spawned tasks finish (up to timeout_s), then return
        each one's result. The children run on a dedicated pool, so waiting here
        never starves them."""

        app, session_id = _ctx_app_session()
        registry = app.state.agent_task_registry
        import time as _time  # noqa: PLC0415

        deadline = _time.monotonic() + max(0.0, float(timeout_s or 0.0))
        results = []
        for tid in task_ids or []:
            remaining = max(0.0, deadline - _time.monotonic())
            registry.event(tid).wait(timeout=remaining)
            task = registry.get(tid)
            if task is None:
                results.append({"task_id": tid, "error": "unknown_task"})
                continue
            payload = _completion_payload(task)
            results.append(payload)
            if task.is_terminal:
                event_type = (
                    "blueprint.delegation.completed"
                    if task.status == "completed"
                    else "blueprint.delegation.failed"
                )
                _emit_semantic_event(
                    app,
                    session_id,
                    event_type,
                    turn_id=_active_semantic_turn_id(),
                    trace_id=_active_semantic_trace_id(),
                    status=task.status,
                    summary=f"{task.agent_ref.get('expert_id', '')} returned to {agent_def.id}",
                    actor={"agent_id": task.agent_ref.get("expert_id", ""), "role": "child_expert"},
                    subject={"agent_id": agent_def.id, "role": "parent_expert"},
                    blueprint=_blueprint_block(agent_def, task.agent_ref.get("expert_id", "")),
                    payload=dict(payload),
                )
        return json.dumps({"results": results}, sort_keys=True, default=str)

    def check_agent_tasks() -> str:
        """List the tasks this session has spawned and their current status."""

        app, session_id = _ctx_app_session()
        tasks = app.state.agent_task_registry.for_parent(session_id)
        return json.dumps(
            {
                "tasks": [
                    {
                        "task_id": t.task_id,
                        "agent": t.agent_ref.get("expert_id", ""),
                        "status": t.status,
                        "queued_reason": t.queued_reason,
                    }
                    for t in tasks
                ]
            },
            sort_keys=True,
        )

    def spawn_agents_parallel(spawns: list[dict]) -> str:
        """Fan out several declared children at once. ``spawns`` is a list of
        {agent, task}; returns their task_ids (collect with wait_agent_tasks)."""

        app, session_id = _ctx_app_session()
        _emit_semantic_event(
            app,
            session_id,
            "blueprint.fanout.started",
            turn_id=_active_semantic_turn_id(),
            trace_id=_active_semantic_trace_id(),
            status="running",
            summary=f"{agent_def.id} fanned out to {len(spawns or [])} children",
            actor={"agent_id": agent_def.id, "role": "parent_expert"},
            blueprint=_blueprint_block(agent_def, ""),
        )
        out = []
        for entry in spawns or []:
            agent = str((entry or {}).get("agent") or "")
            task = str((entry or {}).get("task") or "")
            out.append(json.loads(spawn_agent_task(agent, task)))
        return json.dumps({"spawned": out}, sort_keys=True)

    return [
        dspy.Tool(
            func=spawn_agent_task,
            name="spawn_agent_task",
            desc=spawn_agent_task.__doc__,
            args={
                "agent": {"type": "string", "description": "Declared child expert id to spawn."},
                "task": {"type": "string", "description": "The specific task for that child."},
            },
        ),
        dspy.Tool(
            func=wait_agent_tasks,
            name="wait_agent_tasks",
            desc=wait_agent_tasks.__doc__,
            args={
                "task_ids": {"type": "array", "description": "Task ids returned by spawn."},
                "timeout_s": {"type": "number", "description": "Max seconds to wait."},
            },
        ),
        dspy.Tool(
            func=check_agent_tasks,
            name="check_agent_tasks",
            desc=check_agent_tasks.__doc__,
            args={},
        ),
        dspy.Tool(
            func=spawn_agents_parallel,
            name="spawn_agents_parallel",
            desc=spawn_agents_parallel.__doc__,
            args={
                "spawns": {"type": "array", "description": "List of {agent, task} to fan out."},
            },
        ),
    ]
