"""``Flow``: an agent's orchestration program.

A flow is a ``dspy.Module`` whose ``forward(task, ctx)`` calls subagents, agents
and tools through the runtime context. Every ``ctx`` call is an ordinary CLIO
task, so a flow inherits handles, Stop, cancellation and restart recovery from
the task layer. A plugin ships flows in ``ai.iowarp.clio/flows/``; an agent
selects one by id, and ``react`` from ``core`` is the default::

    class Council(Flow):
        id = "council"
        members = ("physicist", "numerics-reviewer", "hpc-engineer")

        async def forward(self, question, ctx):
            views = await ctx.gather([ctx.call(m, question) for m in self.members])
            return await ctx.call("synthesizer", {"question": question, "views": views})

An agent can select a flow with options by naming it::

    class Factorio(ScienceAgent):
        flow = Flow("campaign", max_cycles=5)

Flow code must be deterministic given its results: no clock, randomness or I/O
outside ``ctx``. The runtime re-runs ``forward`` from the top after a restart
and every ``ctx`` call re-attaches to the task it already started.
"""

from __future__ import annotations

from typing import Any, ClassVar

import dspy


class Flow(dspy.Module):
    """Base class for flows, and a by-id reference when instantiated directly."""

    id: ClassVar[str] = ""
    description: ClassVar[str] = ""
    #: Limits enforced by ``ctx`` on top of the runtime's inference capacity.
    #: Zero means "no flow-level limit" for that dimension.
    max_depth: ClassVar[int] = 4
    max_concurrency: ClassVar[int] = 4
    max_model_calls: ClassVar[int] = 0
    budget_tokens: ClassVar[int] = 0

    def __init__(self, ref: str | None = None, **options: Any) -> None:
        super().__init__()
        self._ref_id = str(ref or "")
        self._ref_options = dict(options)
        for key, value in options.items():
            setattr(self, key, value)

    @property
    def is_reference(self) -> bool:
        """True for ``Flow("campaign", max_cycles=5)``: a selection, not a program."""

        return type(self) is Flow and bool(self._ref_id)

    def reference(self) -> dict[str, Any]:
        """The ``{"id", "options"}`` selection this object stands for."""

        if self.is_reference:
            return {"id": self._ref_id, "options": dict(self._ref_options)}
        return {"id": type(self).id, "options": dict(self._ref_options)}

    def limits(self) -> dict[str, int]:
        """The flow's declared limits, after any per-selection options."""

        return {
            "max_depth": int(getattr(self, "max_depth", 0) or 0),
            "max_concurrency": int(getattr(self, "max_concurrency", 0) or 0),
            "max_model_calls": int(getattr(self, "max_model_calls", 0) or 0),
            "budget_tokens": int(getattr(self, "budget_tokens", 0) or 0),
        }

    async def forward(self, task: Any, ctx: Any) -> Any:  # noqa: D102 - subclasses implement
        raise NotImplementedError(f"flow {type(self).id or type(self).__name__} has no forward()")
