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

import typing
from collections.abc import Mapping
from typing import Any, ClassVar

import dspy
from pydantic import TypeAdapter, ValidationError


class FlowOptionsInvalid(ValueError):
    """Options that do not have the types the flow declares; ``problems`` names each."""

    def __init__(self, flow_id: str, problems: list[str]) -> None:
        super().__init__(f"options for flow {flow_id!r} are invalid: " + "; ".join(problems))
        self.flow_id = flow_id
        self.problems = problems


def _type_name(hint: Any) -> str:
    return hint.__name__ if isinstance(hint, type) else repr(hint).replace("typing.", "")


class Flow(dspy.Module):
    """Base class for flows, and a by-id reference when instantiated directly."""

    id: ClassVar[str] = ""
    description: ClassVar[str] = ""
    #: The flow's steps as its author draws them, a Pele ``railroad-beta`` grammar
    #: (``sequence``, ``choice``, ``oneOrMore``, ``terminal``...). The request to trust
    #: a plugin Clio wrote shows it; it describes the code, it is not checked against it.
    shape: ClassVar[str] = ""
    #: Limits a flow may declare, enforced by ``ctx`` on top of Clio's own bounds (the
    #: subagent depth and concurrency settings, the checkpoint cap, Stop and cancel).
    #: Zero, the default, means "no flow-level limit" for that dimension.
    max_depth: ClassVar[int] = 0
    max_concurrency: ClassVar[int] = 0
    max_model_calls: ClassVar[int] = 0
    budget_tokens: ClassVar[int] = 0
    #: The model this flow's calls run on when a call names none (``account/model``
    #: or a model id); empty leaves it to each child's preference, then the caller's.
    model: ClassVar[str] = ""

    def __init__(self, ref: str | None = None, **options: Any) -> None:
        super().__init__()
        self._ref_id = str(ref or "")
        self._ref_options = dict(options)
        for key, value in options.items():
            setattr(self, key, value)

    @classmethod
    def checked_options(cls, options: Mapping[str, Any] | None) -> dict[str, Any]:
        """``options`` checked against the types this flow declares for them.

        An option the flow annotates (``members: tuple[str, ...]``) is validated and
        converted as Pydantic does (a list becomes a tuple); one of another shape raises
        :class:`FlowOptionsInvalid` naming it and the declared type. Options the flow
        does not annotate pass unchanged; annotations that do not resolve are refused.
        """

        if not options:
            return {}
        try:
            hints = typing.get_type_hints(cls)
        except (NameError, TypeError) as exc:
            # The flow's own annotations do not resolve: options cannot be checked.
            raise FlowOptionsInvalid(cls.id or cls.__name__, [f"annotations: {exc}"]) from exc
        checked: dict[str, Any] = {}
        problems: list[str] = []
        for key, value in (options or {}).items():
            hint = hints.get(key)
            if typing.get_origin(hint) is ClassVar:
                hint = next(iter(typing.get_args(hint)), None)
            if hint is None:
                checked[key] = value
                continue
            try:
                checked[key] = TypeAdapter(hint).validate_python(value)
            except ValidationError:
                problems.append(f"{key} must be {_type_name(hint)}, not {value!r:.80}")
        if problems:
            raise FlowOptionsInvalid(cls.id or cls.__name__, problems)
        return checked

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
