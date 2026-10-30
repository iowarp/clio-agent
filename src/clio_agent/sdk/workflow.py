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
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

_STEP_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class WorkflowError(ValueError):
    """A workflow that cannot run: duplicate or malformed ids, unknown dependencies, a cycle."""


@dataclass(frozen=True)
class StatePredicate:
    """One typed gate over the ``workflow_state`` of the steps a step waits on.

    ``field_path`` is a dotted path (``acquisition.status``). ``exists`` requires the
    path to be present or absent. ``equals`` requires it to be present and equal.
    """

    field_path: str
    kind: Literal["exists", "equals"]
    exists: bool | None = None
    equals: Any = None

    def as_declared(self) -> dict[str, Any]:
        inner: dict[str, Any] = (
            {"exists": self.exists} if self.kind == "exists" else {"equals": self.equals}
        )
        return {"when_state": {self.field_path: inner}}


@dataclass(frozen=True)
class GateResult:
    """Whether a step's ``when_state`` holds, and the first unmet predicate."""

    satisfied: bool
    predicate: dict[str, Any] | None = None
    observed: dict[str, Any] | None = None


def _field_lookup(state: Mapping[str, Any], path: str) -> tuple[bool, Any]:
    cur: Any = state
    for part in path.split("."):
        if isinstance(cur, Mapping) and part in cur:
            cur = cur[part]
        else:
            return False, None
    return True, cur


def _predicates(when_state: Any, step_id: str) -> tuple[StatePredicate, ...]:
    """Parse ``when_state``. Empty means the step runs once its dependencies complete."""

    if when_state in (None, "", {}):
        return ()
    if not isinstance(when_state, Mapping):
        raise WorkflowError(
            f"step {step_id!r}: when_state must be a mapping of "
            f"field-path -> {{exists|equals}}, got {type(when_state).__name__}"
        )
    predicates: list[StatePredicate] = []
    for raw_field, raw_pred in when_state.items():
        field_path = str(raw_field).strip()
        if not field_path:
            raise WorkflowError(f"step {step_id!r}: when_state has an empty field path")
        if not isinstance(raw_pred, Mapping):
            raise WorkflowError(
                f"step {step_id!r}: when_state field {field_path!r} must map to "
                f"{{exists|equals}}, got {type(raw_pred).__name__}"
            )
        has_exists = "exists" in raw_pred
        has_equals = "equals" in raw_pred
        if has_exists == has_equals:
            raise WorkflowError(
                f"step {step_id!r}: when_state field {field_path!r} must declare "
                "exactly one of 'exists' or 'equals'"
            )
        if has_exists:
            exists_val = raw_pred["exists"]
            if type(exists_val) is not bool:
                raise WorkflowError(
                    f"step {step_id!r}: when_state field {field_path!r} 'exists' "
                    f"must be a bool, got {exists_val!r}"
                )
            predicates.append(StatePredicate(field_path, "exists", exists=exists_val))
        else:
            predicates.append(StatePredicate(field_path, "equals", equals=raw_pred["equals"]))
    return tuple(predicates)


def _timeout(value: Any, step_id: str) -> float:
    if value in (None, ""):
        return 0.0
    if type(value) is bool or not isinstance(value, (int, float)) or value < 0:
        raise WorkflowError(f"step {step_id!r}: timeout_s must be a number >= 0, got {value!r}")
    return float(value)


def evaluate_when_state(
    predicates: tuple[StatePredicate, ...], state: Mapping[str, Any]
) -> GateResult:
    """The first unmet predicate, read from the dependency fold. An empty gate holds."""

    for pred in predicates:
        found, value = _field_lookup(state, pred.field_path)
        if pred.kind == "exists":
            if found != pred.exists:
                return GateResult(
                    False,
                    pred.as_declared(),
                    {"field": pred.field_path, "exists": found},
                )
        elif not found or value != pred.equals:
            return GateResult(
                False,
                pred.as_declared(),
                {"field": pred.field_path, "found": found, "value": value},
            )
    return GateResult(True)


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
    #: The model the step's child runs on (``account/model`` or a model id); empty
    #: leaves it to the child's preference, then the run's model.
    model: str = ""
    #: Authored gate. The step runs only when every predicate holds on the
    #: ``workflow_state`` of the steps it waits on.
    when_state: dict[str, Any] = field(default_factory=dict, hash=False)
    #: Seconds until the child is cancelled. Zero means no deadline.
    timeout_s: float = 0.0
    predicates: tuple[StatePredicate, ...] = ()

    def __init__(
        self,
        id: str,  # noqa: A002 - the design's public field name
        target: str,
        input: Any = None,  # noqa: A002
        after: list[str] | tuple[str, ...] = (),
        description: str = "",
        options: dict[str, Any] | None = None,
        model: str = "",
        *,
        when_state: Mapping[str, Any] | None = None,
        timeout_s: Any = 0,
    ) -> None:
        if options is not None and not isinstance(options, dict):
            raise WorkflowError(f"step {id!r}: options must be an object, got {options!r}")
        step_id = str(id)
        object.__setattr__(self, "id", step_id)
        object.__setattr__(self, "target", str(target))
        object.__setattr__(self, "input", input)
        object.__setattr__(self, "after", tuple(str(a) for a in after))
        object.__setattr__(self, "description", str(description))
        object.__setattr__(self, "options", dict(options or {}))
        object.__setattr__(self, "model", str(model or "").strip())
        authored = dict(when_state) if when_state else {}
        object.__setattr__(self, "when_state", authored)
        object.__setattr__(self, "predicates", _predicates(authored, step_id))
        object.__setattr__(self, "timeout_s", _timeout(timeout_s, step_id))

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"id": self.id, "target": self.target, "after": list(self.after)}
        if self.input is not None:
            data["input"] = self.input
        if self.description:
            data["description"] = self.description
        if self.options:
            data["options"] = dict(self.options)
        if self.model:
            data["model"] = self.model
        if self.when_state:
            data["when_state"] = dict(self.when_state)
        if self.timeout_s:
            data["timeout_s"] = self.timeout_s
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
            model=data.get("model") or "",
            when_state=data.get("when_state"),
            timeout_s=data.get("timeout_s", 0),
        )


def _concurrency(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise WorkflowError(f"max_concurrency must be an integer >= 0, got {value!r}")
    return value


@dataclass
class Workflow:
    """An ordered set of steps; validated on construction."""

    steps: list[Step] = field(default_factory=list)
    #: How many of this workflow's steps may be in flight. Zero means no step cap.
    max_concurrency: int = 0

    def __post_init__(self) -> None:
        self.max_concurrency = _concurrency(self.max_concurrency)
        self.steps = [n if isinstance(n, Step) else Step.from_dict(n) for n in self.steps]
        validate_steps(self.steps)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"steps": [step.to_dict() for step in self.steps]}
        if self.max_concurrency:
            data["max_concurrency"] = self.max_concurrency
        return data

    @classmethod
    def from_dict(cls, data: Any) -> Workflow:
        if isinstance(data, Workflow):
            return data
        if isinstance(data, list):
            data = {"steps": data}
        if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
            raise WorkflowError("a workflow is {'steps': [...]}")
        return cls(
            steps=[Step.from_dict(row) for row in data["steps"]],
            max_concurrency=data.get("max_concurrency", 0),
        )


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


__all__ = [
    "GateResult",
    "StatePredicate",
    "Step",
    "Workflow",
    "WorkflowError",
    "evaluate_when_state",
    "validate_steps",
]
