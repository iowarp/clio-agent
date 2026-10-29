"""Runtime configuration for admitting new child-agent work."""

from __future__ import annotations

from clio_agent import conf

DEFAULT_MAX_SPAWN_DEPTH = 8
DEFAULT_MAX_CONCURRENT_AGENT_TASKS = 3


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


def max_inference_concurrency() -> int:
    """Read the child model-call capacity; zero leaves it unlimited."""
    value = conf.resolve(
        "agent_tasks.max_concurrent",
        env="CLIO_MAX_CONCURRENT_AGENT_TASKS",
        default=DEFAULT_MAX_CONCURRENT_AGENT_TASKS,
        cast=conf.as_int,
    )
    if value < 0:
        raise ValueError("Child inference concurrency must be nonnegative")
    return int(value)


def max_forks_per_task() -> int:
    """Forks one task may have outside a workflow (``fork``); zero means unlimited.

    Inside a workflow a fork counts as an added step against the run's growth bounds
    instead (``workflows.max_added_steps``).
    """
    value = conf.resolve(
        "agent_tasks.max_forks_per_task",
        env="CLIO_MAX_FORKS_PER_TASK",
        default=0,
        cast=conf.as_int,
    )
    if value < 0:
        raise ValueError(
            "agent_tasks.max_forks_per_task must be zero (unlimited) or a positive integer"
        )
    return int(value)
