"""A note, not a decision: after a run of reading calls, say that ``explore`` exists.

Claude Code steers open-ended searching toward its Explore agent with reminders in the
conversation; a model not trained on CLIO's harness has no such habit. After a number of
read-only calls in a turn with no delegation, the loop adds one injection naming the
``explore`` subagent and the model it runs on. Nothing is spawned: the agent decides, and
the note comes at most once per turn.

The note is the reminder ``clio.reminder.explore_nudge``
(``prompt_packs/builtin/reminders/explore_nudge.md``): its switch, its wording and its
threshold (``x_param_after``; 0 never notes) are edited on the Reminders page, and the
Fleet page's switch writes the same reminder (:mod:`clio_agent.gact.reminders`).
"""

from __future__ import annotations

from typing import Any

from clio_agent.gact import reminders

__all__ = [
    "EXPLORE_NUDGE_SOURCE",
    "note_reading_run",
    "nudge_settings",
    "save_nudge_settings",
]

#: The injection's source (the transcript shows it as harness data).
EXPLORE_NUDGE_SOURCE = "explore_nudge"
#: Calls that delegate: a run of reading ends when the agent hands work to a child.
_DELEGATING = frozenset({"spawn_agent_task", "spawn_agents_parallel", "spawn_skill_task", "fork"})
_DEFAULT_AFTER = 6


def _after(**where: Any) -> int:
    return reminders.param(EXPLORE_NUDGE_SOURCE, "after", _DEFAULT_AFTER, int, **where)


def nudge_settings(app: Any = None) -> dict[str, Any]:
    """The note's switch and threshold as the Fleet settings page shows them."""
    found = reminders.reminder(EXPLORE_NUDGE_SOURCE, app=app)
    return {"enabled": bool(found and found.active), "after": _after(app=app)}


def save_nudge_settings(enabled: bool, after: int, app: Any = None) -> None:
    """The Fleet page's switch and threshold, saved into the reminder itself."""
    reminders.update(EXPLORE_NUDGE_SOURCE, active=enabled, params={"after": after}, app=app)


def _read_only(name: str) -> bool:
    from clio_agent.gact.runtime.grant_resolver import is_read_only  # noqa: PLC0415

    return is_read_only("tool", name, None, None)


def _explore_model() -> str:
    """The model ``explore`` runs on: the fleet role's model, else (by default) the
    account's fast model, else ``your model`` when it inherits the caller's."""
    from clio_agent.gact.fleet_settings import fleet_settings, role_default  # noqa: PLC0415

    try:
        choice = (fleet_settings().get("judges") or {}).get("explore")
        default = role_default("explore")
    except ValueError:  # an invalid fleet setting is reported where it is edited
        return "your model"
    if choice:
        return str(choice.get("model_id") or "your model")
    if default == "fast":
        fast = _fast_model_ref()
        if fast:
            return fast.split("/", 1)[-1]
    return "your model"


def _fast_model_ref() -> str:
    """The session's account's fast model (``account/model``), or "" when it has none."""
    from clio_agent.gact import context as _ctx  # noqa: PLC0415
    from clio_agent.gact.fleet.child_model import caller_model, fleet_entries  # noqa: PLC0415
    from clio_agent.gact.fleet.selection import _fast_model  # noqa: PLC0415

    app, session_id = _ctx.active_app(), _ctx.active_session_id() or ""
    if app is None or not session_id:
        return ""
    session = app.state.sessions.get(session_id)
    if session is None:
        return ""
    account = caller_model(app, session).get("provider_id", "")
    return _fast_model(fleet_entries(app, needed=False), account)


def _can_explore(loop: Any) -> str:
    """The spawn tool to name when this agent can start an ``explore`` child, else ``""``."""
    cached = getattr(loop, "_clio_explore_spawn", None)
    if cached is not None:
        return cached
    tools = set(getattr(loop.agent, "tools", {}) or {})
    spawn = next((t for t in ("spawn_agents_parallel", "spawn_agent_task") if t in tools), "")
    if spawn:
        from clio_agent.gact import context as _ctx  # noqa: PLC0415
        from clio_agent.gact.agents.resolution import session_resolution  # noqa: PLC0415
        from clio_agent.plugins.resolve import delegable_subagents  # noqa: PLC0415

        app, session_id = _ctx.active_app(), _ctx.active_session_id() or ""
        resolution = session_resolution(app, session_id) if app is not None and session_id else None
        roles = {row.get("id") for row in delegable_subagents(resolution)} if resolution else set()
        spawn = spawn if "explore" in roles else ""
    loop._clio_explore_spawn = spawn
    return spawn


def note_reading_run(loop: Any, call_names: list[str]) -> None:
    """Count a step's read-only calls; past the threshold, note ``explore`` once per turn."""
    if getattr(loop, "_clio_explore_noted", False):
        return
    if _DELEGATING & set(call_names):
        loop._clio_reading_calls = 0
        return
    reads = sum(1 for name in call_names if _read_only(name))
    count = getattr(loop, "_clio_reading_calls", 0) + reads
    loop._clio_reading_calls = count
    after = getattr(loop, "_clio_explore_after", None)
    if after is None:  # read once per turn: the loop lives for one turn
        after = loop._clio_explore_after = _after()
    if after <= 0 or count < after:
        return
    spawn = _can_explore(loop)
    if not spawn:
        return
    loop._clio_explore_noted = True
    text = reminders.render(
        EXPLORE_NUDGE_SOURCE,
        {"calls": count, "explore_model": _explore_model(), "spawn_tool": f"`{spawn}`"},
    )
    if text:
        loop.recorder.injections([(EXPLORE_NUDGE_SOURCE, text)])
