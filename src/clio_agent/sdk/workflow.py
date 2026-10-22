"""Workflows as data, run by the ``workflow`` flow from ``core``.

A workflow is a set of steps, each naming a target (a subagent, an installed agent,
a flow or a tool), its input and the steps it waits on. Every step whose
dependencies are met starts as a task, as soon as its own inputs finish::

    workflow = Workflow(steps=[
        Step("survey", "literature-search", input="$task"),
        Step("design", "setup-tosca", after=["survey"]),
        Step("review", "process-reviewer", after=["design"]),
    ])

Inputs are data: ``"$task"`` is the workflow's task and ``"$<step>"`` a finished
step's result, anywhere inside a string-free mapping or list. A step with no
input receives the task and its dependencies' answers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_STEP_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class WorkflowError(ValueError):
    """A workflow that cannot run: duplicate or malformed ids, unknown dependencies, a cycle."""


@dataclass(frozen=True)
class Step:
    """One step of a workflow."""

    id: str
    target: str
    input: Any = None
    after: tuple[str, ...] = ()
    description: str = ""
    #: Options for a flow target, e.g. an arena's ``contestants`` or a council's ``members``.
    options: dict[str, Any] = field(default_factory=dict, hash=False)

    def __init__(
        self,
        id: str,  # noqa: A002 - the design's public field name
        target: str,
        input: Any = None,  # noqa: A002
        after: list[str] | tuple[str, ...] = (),
        description: str = "",
        options: dict[str, Any] | None = None,
    ) -> None:
        if options is not None and not isinstance(options, dict):
            raise WorkflowError(f"step {id!r}: options must be an object, got {options!r}")
        object.__setattr__(self, "id", str(id))
        object.__setattr__(self, "target", str(target))
        object.__setattr__(self, "input", input)
        object.__setattr__(self, "after", tuple(str(a) for a in after))
        object.__setattr__(self, "description", str(description))
        object.__setattr__(self, "options", dict(options or {}))

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"id": self.id, "target": self.target, "after": list(self.after)}
        if self.input is not None:
            data["input"] = self.input
        if self.description:
            data["description"] = self.description
        if self.options:
            data["options"] = dict(self.options)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Step:
        if not isinstance(data, dict):
            raise WorkflowError(f"a workflow step must be an object, got {data!r}")
        return cls(
            id=data.get("id", ""),
            target=data.get("target", ""),
            input=data.get("input"),
            after=data.get("after") or (),
            description=data.get("description", ""),
            options=data.get("options"),
        )


@dataclass
class Workflow:
    """An ordered set of steps; validated on construction."""

    steps: list[Step] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.steps = [n if isinstance(n, Step) else Step.from_dict(n) for n in self.steps]
        validate_steps(self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {"steps": [step.to_dict() for step in self.steps]}

    @classmethod
    def from_dict(cls, data: Any) -> Workflow:
        if isinstance(data, Workflow):
            return data
        if isinstance(data, list):
            data = {"steps": data}
        if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
            raise WorkflowError("a workflow is {'steps': [...]}")
        return cls(steps=[Step.from_dict(row) for row in data["steps"]])


def validate_steps(steps: list[Step]) -> None:
    """Raise :class:`WorkflowError` naming the first problem."""

    seen: set[str] = set()
    for step in steps:
        if not _STEP_ID.match(step.id):
            raise WorkflowError(f"step id {step.id!r} must be lowercase letters, digits, - or _")
        if step.id in seen or step.id == "task":
            raise WorkflowError(f"step id {step.id!r} is used twice or reserved")
        if not step.target.strip():
            raise WorkflowError(f"step {step.id!r} has no target")
        seen.add(step.id)
    for step in steps:
        for dependency in step.after:
            if dependency not in seen:
                raise WorkflowError(f"step {step.id!r} waits on unknown step {dependency!r}")
    order = {step.id: step.after for step in steps}
    state: dict[str, int] = {}

    def visit(step_id: str, path: tuple[str, ...]) -> None:
        if state.get(step_id) == 2:
            return
        if state.get(step_id) == 1:
            raise WorkflowError("workflow has a dependency cycle: " + " -> ".join((*path, step_id)))
        state[step_id] = 1
        for dependency in order[step_id]:
            visit(dependency, (*path, step_id))
        state[step_id] = 2

    for step in steps:
        visit(step.id, ())


__all__ = ["Step", "Workflow", "WorkflowError", "validate_steps"]
