"""How the recorder treats one injection's text: an event note, or state already held.

The recorder (``StepRecorder.injections``) keeps the context stable by recording a
source's text only when it differs from the copy of that source still in context:
working state (the session's facts, the plan, the todos) is offered at every turn start
and need not be written twice. Two kinds of text need more than that rule:

* :func:`fired` -- an **event note** says that something happened now (the stale-plan
  note, the replanning suggestion, the explore note). Its wording can repeat word for
  word, and each firing is news, so each firing carries its own id and is recorded once
  per firing, never dropped as a repeat of an earlier one.
* :func:`held_by` -- **state the agent wrote itself**: the plan as ``plan_update`` left
  it is already in context as that call. The text names the tool, and the recorder skips
  it while a call of that tool is in the working set; once a compaction folds the call
  away (or the working set is rebuilt without it) the text is recorded again.

A leaf module: plain ``str`` subclasses, no GACT import.
"""

from __future__ import annotations

import uuid

__all__ = ["FiredText", "HeldText", "fired", "held_by"]


class FiredText(str):
    """An event note's text with the id of the firing that produced it."""

    firing_id: str


class HeldText(str):
    """State text the agent already holds while a call of ``held_by`` is in context."""

    held_by: str


def fired(text: str) -> FiredText:
    """``text`` as one firing of an event note (a fresh firing id)."""
    note = FiredText(text)
    note.firing_id = uuid.uuid4().hex
    return note


def held_by(text: str, tool: str) -> HeldText:
    """``text`` as state the agent holds while a ``tool`` call is in its context."""
    state = HeldText(text)
    state.held_by = tool
    return state
