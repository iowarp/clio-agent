"""The delegation plugin's spawn tools: children through the one conversation owner.

``spawn_agent_task`` and ``spawn_agents_parallel`` accept work into the task layer
through :meth:`~clio_agent.gact.conversation_assignments.Conversations.spawn`, the
same path flows use. No agent-only wait or cancel catalog lives here.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import uuid4

from clio_agent.gact.fleet.child_model import accepted_model
from clio_agent.gact.task_declarations import _STRING, _runtime, _tool
from clio_agent.tools.task_call_context import TASK_CALL

logger = logging.getLogger(__name__)


async def spawn_child(
    app: Any,
    caller: Any,
    agent: str,
    task: str,
    invocation: str,
    *,
    input_task_ids: list[str] | None = None,
    strategy: dict[str, Any] | None = None,
    model: str = "",
    name: str = "",
    concurrency: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Start ``agent`` on ``task`` for ``caller``: the spawn tools' path, and the person's.

    Only who ``caller`` is differs between the two: the spawn tools resolve the calling
    agent, a person's summons name the conversation they wrote in (``metadata`` then
    says the person started it).
    """
    from clio_agent.gact.assignment_evidence import select_assignment_evidence

    target = str(agent or "").strip() or "self"
    if not isinstance(name, str):
        raise ValueError("name must be a string")
    child_name = name.strip()
    session_metadata: dict[str, Any] = dict(metadata or {})
    if child_name:
        session_metadata["subagent_name"] = child_name
    if target == "self":
        # A copy of the caller runs react, never the caller's selected flow (D14).
        session_metadata["agent_flow"] = {"id": "react", "options": {}}
    pins = await app.state.task_history.io.run(
        select_assignment_evidence, app.state.task_history, caller, input_task_ids
    )
    result = await app.state.conversations.spawn(
        caller,
        task,
        target,
        invocation,
        inference_concurrency=concurrency,
        strategy=strategy,
        launch_context={"input_task_refs": [pin.model_dump(mode="json") for pin in pins]},
        session_metadata=session_metadata or None,
        model=str(model or "").strip(),
    )
    return {
        **result,
        **({"task_id": result["handle"]} if "handle" in result else {}),
        **accepted_model(app, result),
    }


def build_spawn_runtime_tools(
    base_agent: Any, agent_def: Any, *, resolution: Any = None, **options: Any
) -> list[Any]:
    """Spawn through the same conversation owner; no agent-only wait/cancel catalogs.

    The ``delegation`` plugin claims these tools. ``agent`` names one of the
    agent's defined subagents (listed in the description) or an installed agent
    started as a general subagent. Omitted, the child is a copy of the caller: the
    caller's exact resolution and delta (``self``), running ``react`` (D14).
    """
    fanout = getattr(agent_def, "fanout", {}) or {}
    limits = fanout.get("limits", {})
    concurrency = limits.get("concurrency")
    if concurrency is not None and (
        isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1
    ):
        raise ValueError("Declared fanout concurrency must be a positive integer")

    def local_placement(placement: str | None) -> None:
        if placement == "":
            raise ValueError("placement must be 'local' or null")
        if placement not in {None, "local"}:
            raise ValueError(
                "Special relay placement is unavailable; configure an ordinary MCP server"
            )

    async def prepare_and_spawn(
        agent: str,
        task: str,
        placement: str | None,
        input_task_ids: list[str] | None,
        strategy: dict[str, Any] | None,
        invocation: str,
        model: str = "",
        name: str = "",
    ) -> dict[str, Any]:
        from clio_agent.gact import summon  # noqa: PLC0415

        local_placement(placement)
        app, caller = _runtime()
        # A subagent the person @-named this turn: the person is recorded as its starter.
        claimed = summon.claim(summon.SUBAGENT, str(agent or "").strip())
        try:
            result = await spawn_child(
                app,
                caller,
                agent,
                task,
                invocation,
                input_task_ids=input_task_ids,
                strategy=strategy,
                model=model,
                name=name,
                concurrency=concurrency,
                metadata=summon.child_metadata(claimed) or None,
            )
        except Exception as error:
            summon.started(claimed, {"error": str(error)})
            raise
        summon.started(claimed, result)
        return result

    async def spawn_agent_task(
        task: str,
        agent: str = "",
        placement: str | None = None,
        input_task_ids: list[str] | None = None,
        strategy: dict[str, Any] | None = None,
        model: str = "",
        name: str = "",
    ) -> dict[str, Any]:
        return await prepare_and_spawn(
            agent,
            task,
            placement,
            input_task_ids,
            strategy,
            TASK_CALL.get().invocation_id or uuid4().hex,
            model,
            name,
        )

    async def spawn_agents_parallel(
        spawns: list[dict[str, Any]],
        placement: str | None = None,
        wait: bool = False,
    ) -> dict[str, Any]:
        local_placement(placement)
        maximum = limits.get("max_items", 0)
        if not spawns or maximum and len(spawns) > maximum:
            raise ValueError("Spawn batch is empty or exceeds its declared item budget")
        invocation = TASK_CALL.get().invocation_id or uuid4().hex

        async def one(index: int, assignment: dict[str, Any]) -> dict[str, Any]:
            try:
                return await prepare_and_spawn(
                    assignment.get("agent") or "",
                    assignment["task"],
                    placement,
                    assignment.get("input_task_ids"),
                    assignment.get("strategy"),
                    invocation + ":" + str(index),
                    assignment.get("model") or "",
                    assignment.get("name") or "",
                )
            except Exception as error:
                logger.exception(
                    "Task boundary reason=group_member_control_failed error_type=%s",
                    type(error).__name__,
                )
                return {"index": index, "error": str(error)}

        # The children start together; each keeps its own index, handle and error.
        outcomes = list(
            await asyncio.gather(*(one(i, assignment) for i, assignment in enumerate(spawns)))
        )
        if not wait:
            return {"spawned": outcomes, "outcomes": outcomes}
        from clio_agent.gact.spawn_batch_wait import wait_for_batch  # noqa: PLC0415

        return await wait_for_batch(outcomes)

    async def fork(note: str, title: str | None = None) -> dict[str, Any]:
        from clio_agent.gact.agent_fork import fork_caller  # noqa: PLC0415
        from clio_agent.gact.artifacts.observer_bridge import current_call_id  # noqa: PLC0415

        app, caller = _runtime()
        # The observer's id of this very call: the fork's copy answers it, and a
        # replay of the call addresses the same fork.
        invocation = current_call_id() or TASK_CALL.get().invocation_id or uuid4().hex
        return await fork_caller(app, caller, note, title, invocation)

    # Internal subagents (started by their plugin's server code) are never offered.
    rows = getattr(resolution, "subagents", None) or []
    subagents = [row for row in rows if not row.get("internal")]
    copy = (
        "Omit to delegate to a copy of yourself: your plugins, tools and model, starting "
        "with only the task."
    )
    targets = (
        copy
        + " Or one of your subagents: "
        + "; ".join(f"{row['id']} ({row['description'] or 'no description'})" for row in subagents)
        + ". Or an installed agent such as clio/standard, started with only the task."
        if subagents
        else copy + " Or an installed agent such as clio/standard, started with only the task."
    )
    strategy_schema = {
        "anyOf": [
            {
                "type": "object",
                "properties": {
                    "variant": {"type": "string", "enum": ["best_of_n", "refine"]},
                    "n": {"type": "integer", "minimum": 1},
                    "rubric": {"type": "string"},
                    "threshold": {"type": "number"},
                    "judge": {"type": "string", "enum": ["lm", "user"]},
                },
                "required": ["variant", "n", "rubric"],
                "additionalProperties": False,
            },
            {"type": "null"},
        ],
        "default": None,
    }
    model_schema = {
        "type": "string",
        "default": "",
        "description": (
            "The model the child runs on: account/model or a model id from list_models. "
            "Empty: the child's own preference, else your model. A model that cannot run "
            "the child is refused with the reason. If you use explore or another search "
            "child, prefer a fast, cheap model for performance and speed (list_models "
            "marks them fast)."
        ),
    }
    tools = [
        _tool(
            spawn_agent_task,
            "spawn_agent_task",
            {
                "agent": {"type": "string", "default": "", "description": targets},
                "task": {"type": "string"},
                "name": {
                    "type": "string",
                    "default": "",
                    "description": "Optional short caller-authored name for this task, shown in the task list.",
                },
                "placement": _STRING,
                "input_task_ids": {
                    "anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}],
                    "default": None,
                },
                "strategy": strategy_schema,
                "model": model_schema,
            },
            "Delegate a task, written self-contained, to a child agent and immediately receive its Subagent handle; the child reports back. Delegate work that can run in parallel and reading you need only the conclusion of: what you read yourself stays in your context for the rest of the task, while a child returns only its report. A child also costs time of its own: it starts fresh, works on its own model (named in the result), and ends by writing its report, which you then read and answer from; a search you can finish in a few wide steps, or with one run_code script, is usually faster done yourself. Do a lookup whose file you already know yourself. Optional strategy selects best_of_n/refine with n, rubric, threshold and lm/user judge. Continue work, observe_tasks, or wait_tasks. Child results wake eligible idle parents. Pause holds only the child model; cancel_tasks includes its descendant work.",
        )
    ]
    tools.append(
        _tool(
            spawn_agents_parallel,
            "spawn_agents_parallel",
            {
                "placement": _STRING,
                "wait": {
                    "type": "boolean",
                    "default": False,
                    "description": "true: return once every child has finished, with each "
                    "child's report in this result, whole (saved to a file only if it would not fit your context).",
                },
                "spawns": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "agent": {"type": "string", "description": targets},
                            "task": {"type": "string"},
                            "name": {
                                "type": "string",
                                "description": "Optional short caller-authored name for this task, shown in the task list.",
                            },
                            "input_task_ids": {
                                "anyOf": [
                                    {"type": "array", "items": {"type": "string"}},
                                    {"type": "null"},
                                ],
                                "default": None,
                            },
                            "strategy": strategy_schema,
                            "model": model_schema,
                        },
                        "required": ["task"],
                        "additionalProperties": False,
                    },
                },
            },
            "Start several child assignments at once, such as one explore child per area of a survey, and return their Subagent handles with independent errors; with wait=true, return when all have finished, with each child's report. Native inference budgets apply only to model calls. Without wait, use shared observation or waiting across the returned handles.",
        )
    )
    tools.append(
        _tool(
            fork,
            "fork",
            {
                "note": {
                    "type": "string",
                    "description": "What the fork should do differently from you: the "
                    "direction it pursues (another hypothesis, a reframed question).",
                },
                "title": {**_STRING, "description": "Optional short name for the fork."},
            },
            "Start a copy of yourself that carries this conversation so far and pursues "
            "the note instead, beside you under the same parent; you get its handle and "
            "continue your own work. In the fork's copy this same call answers "
            "this_session: 'the fork' with the note: that copy is the fork, and the note "
            "is its direction. Whoever waits on you also waits on the fork. In a "
            "workflow step the fork is drawn beside your step and counts against the "
            "workflow's growth bounds and approval; in a conversation it opens a new "
            "conversation. A refusal names its reason (fork_limit_reached, "
            "max_added_reached, not_approved).",
        )
    )
    return tools
