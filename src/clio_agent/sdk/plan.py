"""Plans: workflows as data, run by the ``plan`` flow from ``core``.

A plan is a set of nodes, each naming a target (a subagent, an installed agent,
a flow or a tool), its input and the nodes it waits on. Every node whose
dependencies are met starts as a task, as soon as its own inputs finish::

    plan = Plan(nodes=[
        Node("survey", "literature-search", input="$task"),
        Node("design", "setup-tosca", after=["survey"]),
        Node("review", "process-reviewer", after=["design"]),
    ])

Inputs are data: ``"$task"`` is the plan's task and ``"$<node>"`` a finished
node's result, anywhere inside a string-free mapping or list. A node with no
input receives the task and its dependencies' answers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_NODE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class PlanError(ValueError):
    """A plan that cannot run: duplicate or malformed ids, unknown dependencies, a cycle."""


@dataclass(frozen=True)
class Node:
    """One step of a plan."""

    id: str
    target: str
    input: Any = None
    after: tuple[str, ...] = ()
    description: str = ""

    def __init__(
        self,
        id: str,  # noqa: A002 - the design's public field name
        target: str,
        input: Any = None,  # noqa: A002
        after: list[str] | tuple[str, ...] = (),
        description: str = "",
    ) -> None:
        object.__setattr__(self, "id", str(id))
        object.__setattr__(self, "target", str(target))
        object.__setattr__(self, "input", input)
        object.__setattr__(self, "after", tuple(str(a) for a in after))
        object.__setattr__(self, "description", str(description))

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"id": self.id, "target": self.target, "after": list(self.after)}
        if self.input is not None:
            data["input"] = self.input
        if self.description:
            data["description"] = self.description
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Node:
        if not isinstance(data, dict):
            raise PlanError(f"a plan node must be an object, got {data!r}")
        return cls(
            id=data.get("id", ""),
            target=data.get("target", ""),
            input=data.get("input"),
            after=data.get("after") or (),
            description=data.get("description", ""),
        )


@dataclass
class Plan:
    """An ordered set of nodes; validated on construction."""

    nodes: list[Node] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.nodes = [n if isinstance(n, Node) else Node.from_dict(n) for n in self.nodes]
        validate_nodes(self.nodes)

    def to_dict(self) -> dict[str, Any]:
        return {"nodes": [node.to_dict() for node in self.nodes]}

    @classmethod
    def from_dict(cls, data: Any) -> Plan:
        if isinstance(data, Plan):
            return data
        if isinstance(data, list):
            data = {"nodes": data}
        if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
            raise PlanError("a plan is {'nodes': [...]}")
        return cls(nodes=[Node.from_dict(row) for row in data["nodes"]])


def validate_nodes(nodes: list[Node]) -> None:
    """Raise :class:`PlanError` naming the first problem."""

    seen: set[str] = set()
    for node in nodes:
        if not _NODE_ID.match(node.id):
            raise PlanError(f"node id {node.id!r} must be lowercase letters, digits, - or _")
        if node.id in seen or node.id == "task":
            raise PlanError(f"node id {node.id!r} is used twice or reserved")
        if not node.target.strip():
            raise PlanError(f"node {node.id!r} has no target")
        seen.add(node.id)
    for node in nodes:
        for dependency in node.after:
            if dependency not in seen:
                raise PlanError(f"node {node.id!r} waits on unknown node {dependency!r}")
    order = {node.id: node.after for node in nodes}
    state: dict[str, int] = {}

    def visit(node_id: str, path: tuple[str, ...]) -> None:
        if state.get(node_id) == 2:
            return
        if state.get(node_id) == 1:
            raise PlanError("plan has a dependency cycle: " + " -> ".join((*path, node_id)))
        state[node_id] = 1
        for dependency in order[node_id]:
            visit(dependency, (*path, node_id))
        state[node_id] = 2

    for node in nodes:
        visit(node.id, ())


__all__ = ["Node", "Plan", "PlanError", "validate_nodes"]
