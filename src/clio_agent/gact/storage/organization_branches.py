"""A note, not a decision: when one turn reaches NDP data from several organizations,
say that each organization can be a branch run by a child.

Live Tahoe runs searched, connected and read six organizations one after another, even
with the spawn tools loaded and the NDP skill describing per-organization branches. The
existing ``explore_nudge`` did not fire: it counts read-only calls the model makes
itself, and these runs made most of theirs inside ``run_code``. This note counts
organizations instead, from the NDP tools themselves (a search filtered by
``organization``, a dataset connected), so calls from a script count too. Nothing is
spawned and nothing is refused: the agent decides, and the note comes once per turn.

The note is the reminder ``clio.reminder.organization_branches``
(``plugins/builtin/core/ai.iowarp.clio/injections/organization_branches.md``): its switch,
wording and threshold (``x_param_after``; 0 never notes) are edited on the Reminders page.
"""

from __future__ import annotations

import threading
from typing import Any

from clio_agent import injections
from clio_agent.runtime import trace

__all__ = [
    "BRANCHES_NO_SPAWN_TOOL",
    "BRANCHES_NO_TURN",
    "ORGANIZATION_BRANCHES_SOURCE",
    "note_organization",
]

#: The injection's name (its file in the core plugin's injections folder).
ORGANIZATION_BRANCHES_SOURCE = "organization_branches"
_DEFAULT_AFTER = 3
#: Typed trace reasons for an organization that was counted but noted nothing.
BRANCHES_NO_TURN = "organization_branches_no_turn"
BRANCHES_NO_SPAWN_TOOL = "organization_branches_no_spawn_tool"
_LOCK = threading.Lock()


def _spawn_tool(loop: Any) -> str:
    """The spawn tool this agent can call, else ``""`` (a child cannot be started)."""
    tools = set(getattr(loop.agent, "tools", {}) or {})
    return next((t for t in ("spawn_agents_parallel", "spawn_agent_task") if t in tools), "")


def note_organization(organization: str) -> None:
    """Count ``organization`` for this turn; at the threshold, note branches once."""
    name = str(organization or "").strip()
    if not name:
        return
    from clio_agent.gact.agents.clio_react import active_loop  # noqa: PLC0415

    # The turn's own marks ride on its loop, read duck-typed as in ``_spawn_tool``.
    loop: Any = active_loop()
    if loop is None:
        trace.event("INJECTION", "%s organization=%s", BRANCHES_NO_TURN, name)
        return
    with _LOCK:
        if getattr(loop, "_clio_branches_noted", False):
            return
        seen: dict[str, str] = getattr(loop, "_clio_organizations", None) or {}
        seen.setdefault(name.casefold(), name)
        loop._clio_organizations = seen
        after = getattr(loop, "_clio_branches_after", None)
        if after is None:  # read once per turn: the loop lives for one turn
            after = loop._clio_branches_after = injections.param(
                ORGANIZATION_BRANCHES_SOURCE, "after", _DEFAULT_AFTER, int
            )
        if after <= 0 or len(seen) < after:
            return
        spawn = _spawn_tool(loop)
        loop._clio_branches_noted = True  # decided once per turn, noted or not
        names = list(seen.values())
    if not spawn:
        trace.event("INJECTION", "%s organizations=%d", BRANCHES_NO_SPAWN_TOOL, len(names))
        return
    text = injections.render(
        ORGANIZATION_BRANCHES_SOURCE,
        {"count": len(names), "organizations": ", ".join(names), "spawn_tool": f"`{spawn}`"},
    )
    if text:  # an event note: recorded even when an earlier turn's note had these words
        loop.recorder.injections([(ORGANIZATION_BRANCHES_SOURCE, injections.fired(text))])
    trace.event(
        "INJECTION",
        "organization_branches_noted organizations=%d spawn=%s rendered=%s",
        len(names),
        spawn,
        bool(text),
    )
