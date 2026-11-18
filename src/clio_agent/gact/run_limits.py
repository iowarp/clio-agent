"""A script's run limits: most model steps and most spend over a session tree.

A session created with ``max_turns`` or ``max_budget_usd`` (``gact/run_request.py``)
stores them as ``run_limits`` in its metadata. Every model step of every session in
its tree (the agent's ReAct steps, and through its children a flow's work) calls
:func:`check_before_step` before it calls the model:

* ``max_turns`` counts model steps across the tree, in this server process;
* ``max_budget_usd`` compares the tree's recorded spend (finished turns,
  :func:`clio_agent.gact.fleet.spend.tree_spend`) plus this turn's calls so far with
  the budget. A step already in flight elsewhere in the tree finishes; the next one
  stops.

Reaching a limit records ``run_limit_reached`` on the root (once) and fails the step
with the typed error ``run_limit_reached`` naming the limit, so the turn ends failed
and every later step in the tree fails the same way.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

from clio_agent.errors import ClioError
from clio_agent.gact.unattended import LIMIT_REACHED_KEY, RUN_LIMITS_KEY, run_root

ERROR_TYPE = "run_limit_reached"
_LOCK = threading.Lock()


class RunLimitReached(ClioError):
    """A run reached one of its limits; ``details.limit`` names it."""

    def __init__(self, limit: str, value: float, observed: float) -> None:
        noun = "model steps" if limit == "max_turns" else "USD spent"
        super().__init__(
            f"the run reached its {limit} limit ({value:g}; {observed:g} {noun})",
            error_type=ERROR_TYPE,
            details={"limit": limit, "value": value, "observed": observed},
        )


def _steps(app: Any) -> dict[str, int]:
    state = app.state
    counts = getattr(state, "run_limit_steps", None)
    if counts is None:
        counts = state.run_limit_steps = {}
    return counts


def _turn_cost() -> float:
    from clio_agent.gact.usage import _usage_from_turn_calls  # noqa: PLC0415

    return float(_usage_from_turn_calls(0).get("cost_usd", 0.0) or 0.0)


def _tree_cost(app: Any, root_id: str) -> float:
    from clio_agent.gact.fleet.spend import tree_spend  # noqa: PLC0415

    return sum(spend.cost_usd for spend in tree_spend(app, root_id).by_model.values())


def check_before_step(app: Any, session_id: str) -> None:
    """Raise :class:`RunLimitReached` when the run of ``session_id`` is at a limit."""

    root = run_root(app, session_id)
    metadata = (root.metadata or {}) if root is not None else {}
    limits = metadata.get(RUN_LIMITS_KEY) or {}
    if not limits:
        return
    reached = metadata.get(LIMIT_REACHED_KEY)
    if isinstance(reached, dict) and reached.get("limit"):
        raise RunLimitReached(
            str(reached["limit"]), float(reached["value"]), float(reached["observed"])
        )
    found: tuple[str, float, float] | None = None
    if "max_turns" in limits:
        with _LOCK:
            counts = _steps(app)
            counts[root.id] = counts.get(root.id, 0) + 1
            step = counts[root.id]
        if step > int(limits["max_turns"]):
            found = ("max_turns", float(limits["max_turns"]), float(step - 1))
    if found is None and "max_budget_usd" in limits:
        spent = _tree_cost(app, root.id) + _turn_cost()
        if spent >= float(limits["max_budget_usd"]):
            found = ("max_budget_usd", float(limits["max_budget_usd"]), round(spent, 6))
    if found is None:
        return
    limit, value, observed = found
    record = {
        "limit": limit,
        "value": value,
        "observed": observed,
        "session_id": session_id,
        "reached_at": datetime.now(timezone.utc).isoformat(),
    }
    app.state.sessions.update(root.id, metadata_patch={LIMIT_REACHED_KEY: record})
    raise RunLimitReached(limit, value, observed)


__all__ = ["ERROR_TYPE", "RunLimitReached", "check_before_step"]
