"""The person's ceilings on how far a running workflow may grow.

A workflow's caller may bound its editors (``max_added`` steps, ``max_rounds`` editing
rounds); unbounded is the default. These settings cap what any caller asks for; 0 leaves
growth to the caller and to Clio's own bounds (subagent depth and concurrency, the run's
checkpoint size, Stop and ``cancel_tasks``).
"""

from __future__ import annotations

from clio_agent import conf


def _ceiling(key: str, value: int) -> int:
    if value < 0:
        raise ValueError(f"{key} must be zero (unlimited) or a positive integer")
    return int(value)


def max_added_steps() -> int:
    """Steps editors may add to one workflow run; 0 means unlimited."""

    value = conf.resolve(
        "workflows.max_added_steps",
        env="CLIO_WORKFLOW_MAX_ADDED_STEPS",
        default=0,
        cast=conf.as_int,
    )
    return _ceiling("workflows.max_added_steps", value)


def max_edit_rounds() -> int:
    """Editing rounds in one workflow run; 0 means unlimited."""

    value = conf.resolve(
        "workflows.max_edit_rounds",
        env="CLIO_WORKFLOW_MAX_EDIT_ROUNDS",
        default=0,
        cast=conf.as_int,
    )
    return _ceiling("workflows.max_edit_rounds", value)


def effective(asked: int | None, ceiling: int) -> int | None:
    """The bound a run uses: the tighter of what its caller asked and the person's
    ceiling, or ``None`` (unbounded) when neither sets one."""

    bounds = [b for b in (asked, ceiling or None) if b is not None]
    return min(bounds) if bounds else None
