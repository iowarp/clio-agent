"""Say exactly why a session's activated Agent Blueprint resolved nothing (#1455).

The turn path fails TYPED (``_NoResolvableAgent``) when a session that
explicitly activated an Agent Blueprint resolves no executable agent. The error
used to read "No resolvable Agent Blueprint for this session; install the
default registry or activate an Agent Blueprint" whatever the cause: it named
no blueprint and no fact, so a collaborator's one-off failure (#1455) left
nothing to diagnose it from and suggested an action that did not apply.

This owner module re-reads the same sources the resolver read (the session's
activation, installed-blueprint discovery, the resolution-reason ledger) and
reports which one failed as a typed ``details.reason``. It never guesses: every
message states an observed fact about this session's blueprint.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.types import ErrorInfo

logger = logging.getLogger(__name__)

__all__ = ["UNRESOLVED_BLUEPRINT_REASONS", "diagnose_unresolved_blueprint", "no_resolvable_error"]

#: Closed set of ``details.reason`` values for ``no_resolvable_agent``.
UNRESOLVED_BLUEPRINT_REASONS: dict[str, str] = {
    "blueprint_not_found": "The activated Agent Blueprint is not among the installed blueprints.",
    "blueprint_disabled": "The activated Agent Blueprint is disabled by validation.",
    "blueprint_has_no_enabled_root": "The activated Agent Blueprint has no enabled root agent.",
    "agent_not_in_blueprint": "The requested agent is not part of the activated Agent Blueprint.",
    "blueprint_discovery_failed": "Installed Agent Blueprint discovery raised an error.",
    "blueprint_resolved_on_recheck": (
        "The activated Agent Blueprint resolved no agent when the turn started "
        "but resolves on a re-read right after."
    ),
}

#: Agent ids that mean "the session root", translated to the declared root.
_ROOT_ALIASES = frozenset({"", "main", "default"})


def _blueprint_row(app: "FastAPI", sid: str, blueprint_id: str, path: Path | None) -> Any:
    """The activated blueprint as discovery sees it now, or ``None`` when absent."""

    from clio_agent.gact.agent_blueprints import (  # noqa: PLC0415
        discover_agent_blueprints,
        parse_agent_blueprint_root,
    )
    from clio_agent.gact.agents.resolution import _runtime_workspace_catalog_cwd  # noqa: PLC0415

    if path is not None:
        return parse_agent_blueprint_root(path, scope="session") if path.exists() else None
    cwd = _runtime_workspace_catalog_cwd(app, session_id=sid)
    return next((row for row in discover_agent_blueprints(cwd=cwd) if row.id == blueprint_id), None)


def diagnose_unresolved_blueprint(app: "FastAPI", sid: str, agent_id: str) -> dict[str, Any]:
    """The observed facts behind a session's unresolved Agent Blueprint.

    Args:
        app: The GACT app.
        sid: The session whose turn resolved no executable agent.
        agent_id: The agent id the turn tried to run ("" / "main" / "default"
            for the session's own root).

    Returns:
        ``{"reason", "blueprint_id", "blueprint_path", "validation_errors",
        "resolution_reasons", ...}`` with ``reason`` from
        :data:`UNRESOLVED_BLUEPRINT_REASONS`.
    """

    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        _runtime_active_agent_blueprint_rows,
        _runtime_effective_agent_blueprint_id,
        _runtime_effective_agent_blueprint_path,
    )
    from clio_agent.gact.blueprint_activation import blueprint_resolution_reasons  # noqa: PLC0415

    blueprint_id = _runtime_effective_agent_blueprint_id(app, sid)
    path = _runtime_effective_agent_blueprint_path(app, sid)
    facts: dict[str, Any] = {
        "blueprint_id": blueprint_id,
        "blueprint_path": str(path or ""),
        "agent_id": agent_id,
        "validation_errors": [],
        "resolution_reasons": [row["reason"] for row in blueprint_resolution_reasons(app, sid)],
    }
    try:
        row = _blueprint_row(app, sid, blueprint_id, path)
    except Exception as exc:  # noqa: BLE001 - the discovery failure IS the fact reported
        return {**facts, "reason": "blueprint_discovery_failed", "error": repr(exc)}
    if row is None:
        return {**facts, "reason": "blueprint_not_found"}
    facts["validation_errors"] = list(row.validation_errors)
    if not row.enabled:
        return {**facts, "reason": "blueprint_disabled"}
    agents = _runtime_active_agent_blueprint_rows(app, session_id=sid)
    enabled = sorted(agent.id for agent in agents if agent.enabled)
    facts["enabled_agent_ids"] = enabled
    if enabled and (agent_id in _ROOT_ALIASES or agent_id in enabled):
        # Resolution read nothing moments ago and reads the agent now: say that,
        # instead of inventing a cause.
        return {**facts, "reason": "blueprint_resolved_on_recheck"}
    if enabled:
        # The blueprint resolves agents, just not the one this turn asked for.
        return {**facts, "reason": "agent_not_in_blueprint"}
    return {**facts, "reason": "blueprint_has_no_enabled_root"}


def _message(facts: dict[str, Any]) -> str:
    name = facts["blueprint_id"] or facts["blueprint_path"] or "(unnamed)"
    reason = facts["reason"]
    if reason == "blueprint_not_found":
        return (
            f"This session's Agent Blueprint {name!r} is not installed here. "
            "Install it again or choose another Agent Blueprint for the session."
        )
    if reason == "blueprint_disabled":
        first = next(iter(facts["validation_errors"]), "validation failed")
        return f"This session's Agent Blueprint {name!r} is disabled: {first}"
    if reason == "agent_not_in_blueprint":
        return (
            f"Agent {facts['agent_id'] or 'main'!r} is not part of this session's "
            f"Agent Blueprint {name!r}."
        )
    if reason == "blueprint_resolved_on_recheck":
        return (
            f"This session's Agent Blueprint {name!r} did not load when this message "
            "started, and loads now. Send the message again."
        )
    if reason == "blueprint_discovery_failed":
        return f"CLIO could not read the installed Agent Blueprints: {facts.get('error', '')}"
    return f"This session's Agent Blueprint {name!r} has no enabled root agent."


def no_resolvable_error(app: "FastAPI", sid: str, agent_id: str) -> "ErrorInfo":
    """The typed ``no_resolvable_agent`` error, stating the observed cause.

    Args:
        app: The GACT app.
        sid: The session whose turn resolved no executable agent.
        agent_id: The agent id the turn tried to run.

    Returns:
        The :class:`~clio_agent.gact.types.ErrorInfo` carrying the diagnosis
        (``details.reason`` is one of :data:`UNRESOLVED_BLUEPRINT_REASONS`).
    """

    from clio_agent.gact.types import ErrorInfo  # noqa: PLC0415

    facts = diagnose_unresolved_blueprint(app, sid, agent_id)
    logger.warning(
        "no_resolvable_agent reason=%s session=%s blueprint=%s agent=%s facts=%s",
        facts["reason"],
        sid,
        facts["blueprint_id"],
        agent_id,
        facts,
    )
    return ErrorInfo(
        error="no_resolvable_agent",
        message=_message(facts),
        details={**facts, "recovery_actions": ["activate_agent_blueprint", "retry"]},
        recoverable=True,
    )
