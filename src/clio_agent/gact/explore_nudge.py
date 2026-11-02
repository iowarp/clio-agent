"""A note, not a decision: after a run of reading calls, say that ``explore`` exists.

Claude Code steers open-ended searching toward its Explore agent with reminders in the
conversation; a model not trained on CLIO's harness has no such habit. After
``delegation.explore_nudge_after`` read-only calls in a turn with no delegation, the loop
adds one injection naming the ``explore`` subagent and the model it runs on. Nothing is
spawned: the agent decides, and the note comes at most once per turn.

Configuration (``conf``): ``delegation.explore_nudge`` turns it on or off;
``delegation.explore_nudge_after`` is the number of read-only calls (0 never notes);
``delegation.explore_nudge_text`` is the note itself, a template whose ``${calls}``,
``${explore_model}`` and ``${spawn_tool}`` are filled in (unknown names stay as written;
nothing in it is run).
"""

from __future__ import annotations

from string import Template
from typing import Any

from clio_agent import conf

__all__ = [
    "EXPLORE_NUDGE_SOURCE",
    "note_reading_run",
    "nudge_settings",
]

#: The injection's source (the transcript shows it as harness data).
EXPLORE_NUDGE_SOURCE = "explore_nudge"
#: Calls that delegate: a run of reading ends when the agent hands work to a child.
_DELEGATING = frozenset({"spawn_agent_task", "spawn_agents_parallel", "spawn_skill_task", "fork"})
_DEFAULT_TEXT = (
    "You have made ${calls} reading calls in this turn without delegating. If the rest of "
    "this search can go to an `explore` child (it runs on ${explore_model} and returns only "
    "its findings, with paths), ${spawn_tool} may be faster and keep your context smaller. "
    "If you are close to the answer, carry on."
)


def _enabled() -> bool:
    return conf.resolve(
        "delegation.explore_nudge", env="CLIO_EXPLORE_NUDGE", default=True, cast=conf.as_bool
    )


def _after() -> int:
    return conf.resolve(
        "delegation.explore_nudge_after",
        env="CLIO_EXPLORE_NUDGE_AFTER",
        default=6,
        cast=conf.as_int,
    )


def _text() -> str:
    return conf.resolve(
        "delegation.explore_nudge_text",
        env="CLIO_EXPLORE_NUDGE_TEXT",
        default=_DEFAULT_TEXT,
        cast=conf.as_str,
    )


def nudge_settings() -> dict[str, Any]:
    """The note's switch and threshold as the Fleet settings page shows them."""
    return {"enabled": _enabled(), "after": _after()}


def _read_only(name: str) -> bool:
    from clio_agent.gact.runtime.grant_resolver import is_read_only  # noqa: PLC0415

    return is_read_only("tool", name, None, None)


def _explore_model() -> str:
    """The model the fleet runs ``explore`` on, or ``your model`` when it inherits."""
    from clio_agent.gact.fleet_settings import fleet_settings  # noqa: PLC0415

    try:
        choice = (fleet_settings().get("judges") or {}).get("explore")
    except ValueError:  # an invalid fleet setting is reported where it is edited
        return "your model"
    return str(choice.get("model_id") or "your model") if choice else "your model"


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
    after = _after()
    if after <= 0 or count < after or not _enabled():
        return
    spawn = _can_explore(loop)
    if not spawn:
        return
    loop._clio_explore_noted = True
    text = Template(_text()).safe_substitute(
        calls=count, explore_model=_explore_model(), spawn_tool=f"`{spawn}`"
    )
    loop.recorder.injections([(EXPLORE_NUDGE_SOURCE, text)])
