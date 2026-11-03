"""The events an injection can attach to, and the variables each one supplies.

The code decides only *when* an event fired and what it knows at that moment; what the
agent is told is the declarations' text (:mod:`clio_agent.injections.declarations`).
Two kinds of event:

* **general events**, fired for every agent loop: ``turn_start`` (the turn's first model
  request) and ``tool_result`` (after each tool call, before the result reaches the
  model; a declaration narrows it with ``tool:``);
* **core events**: each core injection names the moment its code fires it (a result
  spilled to a file, a compaction, a finished child, the plan changed...). A plugin
  may attach its own text to any of them, read from the same variables.

Every template may also read the session state (:data:`STATE`), computed only when the
template uses it.
"""

from __future__ import annotations

from dataclasses import dataclass

from clio_agent.injections.declarations import core_injections

__all__ = ["GENERAL", "STATE", "Event", "events"]


@dataclass(frozen=True)
class Event:
    """One event: when it fires and the variables it supplies."""

    name: str
    when: str
    variables: tuple[str, ...] = ()


#: Events the agent loop fires for every session.
GENERAL: dict[str, Event] = {
    "turn_start": Event(
        "turn_start",
        "at the start of each turn, with the turn's other additions (session facts, plan, "
        "todos), before the first model request",
    ),
    "tool_result": Event(
        "tool_result",
        "after each tool call, appended to the result the model reads; ``tool:`` narrows "
        "it to matching tool names",
        ("tool",),
    ),
}

#: Session state every template may read: ``plan`` (``location``, ``status``,
#: ``exists``), ``todos`` (``open``, ``done``, ``total``, ``items``), ``session``
#: (``id``, ``workspace``, ``delegated``) and ``context`` (``tokens``, ``window``,
#: ``percent``).
STATE = frozenset({"plan", "todos", "session", "context"})


def events() -> dict[str, Event]:
    """Every event: the general ones and one per core injection."""
    core = {
        row.event: Event(row.event, row.trigger or row.description, row.variables)
        for row in core_injections().values()
    }
    return {**core, **GENERAL}
