"""Runtime configuration for admitting new child-agent work."""

from __future__ import annotations

from clio_agent import conf

DEFAULT_MAX_SPAWN_DEPTH = 8


def max_spawn_depth() -> int:
    """Return the current nesting limit; zero means no configured depth limit."""
    value = conf.resolve(
        "agent_tasks.max_depth",
        env="CLIO_MAX_AGENT_TASK_DEPTH",
        default=DEFAULT_MAX_SPAWN_DEPTH,
        cast=conf.as_int,
    )
    if value < 0:
        raise ValueError("agent_tasks.max_depth must be zero (unlimited) or a positive integer")
    return int(value)
