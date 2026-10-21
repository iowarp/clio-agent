"""The runtime context a flow's ``forward(task, ctx)`` receives.

``ctx.react(task)`` runs the agent's own ClioReAct program on ``task`` inside
the turn's assignment: model calls happen here, and the program's tools are the
agent's resolved tools. The task-layer primitives (``ctx.call``, ``ctx.start``,
``ctx.gather``, ``ctx.as_completed``, ``ctx.ask``, ``ctx.step``,
``ctx.workspace``, ``ctx.depth``) are documented on each method.
"""

from __future__ import annotations

from typing import Any

import dspy


def _as_prediction(value: Any) -> Any:
    """A flow may return a prediction, a mapping with ``answer`` or plain text."""

    if isinstance(value, dspy.Prediction):
        return value
    if isinstance(value, dict):
        return dspy.Prediction(**{"answer": str(value.get("answer", "")), **value})
    return dspy.Prediction(answer="" if value is None else str(value))


class TurnFlowContext:
    """``ctx`` for a flow running as an agent's turn program."""

    def __init__(self, *, module: Any, kwargs: dict[str, Any], flow: Any) -> None:
        self.module = module
        self.kwargs = dict(kwargs)
        self.flow = flow

    async def react(self, task: Any) -> Any:
        """Run the agent's ReAct loop on ``task`` and return its prediction."""

        kwargs = dict(self.kwargs)
        kwargs["question"] = task if isinstance(task, str) else str(task)
        return self.module.program(**kwargs)

    async def run(self, task: Any) -> Any:
        return _as_prediction(await self.flow.forward(task, self))
