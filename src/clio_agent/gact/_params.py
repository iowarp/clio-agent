"""User-agent generation-parameter parsing for the GACT server (#714).

Behavior-preserving extraction from :mod:`clio_agent.gact.app`. This module owns
the cohesive cluster that reads a single tunable off a user agent's declared
``parameters`` mapping and coerces it to a typed value with an explicit error
(never a silent fallback). It is the single source of truth for:

* :func:`_user_agent_param` -- the raw lookup of one parameter by name.
* :func:`_user_agent_int_param` / :func:`_user_agent_float_param` -- numeric
  coercion that raises :class:`ValueError` on malformed input.
* :func:`_user_agent_bool_param` -- lenient truthy/falsy string coercion.

These read the agent's *own declared* parameters, distinct from
:mod:`clio_agent.conf` (file→env→default process tunables); they are not
interchangeable, so they live here rather than folding into ``conf``.

It also owns :func:`_gact_turn_timeout_s`, the per-turn no-progress timeout
resolver (a runtime/process tunable read via :mod:`clio_agent.conf`) — the one
other single-value parameter read on the turn path (#714 relocation).

The module imports only stdlib + :mod:`clio_agent.conf` + the
:class:`~clio_agent.gact.types.AgentDef` / :class:`~fastapi.FastAPI` types
(under ``TYPE_CHECKING``). It never imports :mod:`clio_agent.gact.app`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Optional

from clio_agent import conf
from clio_agent.gact.semantic_events import DEFAULT_DETAIL_LEVEL

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.types import AgentDef


def _user_agent_param(agent_def: AgentDef, name: str) -> Any:
    """Return one user-agent generation parameter, if present."""
    params = agent_def.parameters if isinstance(agent_def.parameters, Mapping) else {}
    return params.get(name)


def _user_agent_int_param(agent_def: AgentDef, name: str, default: int) -> int:
    """Parse an integer user-agent parameter with an explicit error."""
    value = _user_agent_param(agent_def, name)
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"user agent parameter {name!r} must be an integer") from exc


def _user_agent_bool_param(agent_def: AgentDef, name: str, default: bool = False) -> bool:
    """Parse a boolean user-agent parameter."""

    value = _user_agent_param(agent_def, name)
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "allow", "allowed"}:
        return True
    if normalized in {"0", "false", "no", "off", "deny", "denied"}:
        return False
    return default


def _user_agent_float_param(agent_def: AgentDef, name: str, default: float) -> float:
    """Parse a float user-agent parameter with an explicit error."""
    value = _user_agent_param(agent_def, name)
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"user agent parameter {name!r} must be a number") from exc


def _gact_turn_timeout_s(app: Optional["FastAPI"] = None) -> float:
    """Return the per-turn no-progress timeout in seconds; <=0 disables it.

    Precedence: a RUNTIME value set via ``PUT /v1/providers/lm`` (``turn_timeout_s``,
    stored on ``app.state.lm_config``) wins, so a client configures this on the
    SAME channel it configures the LM — no disconnected server-launch env. When
    unset (0/absent), fall back to the conf pathway (file → ``CLIO_GACT_TURN_TIMEOUT_S``
    → 900s default).
    """
    if app is not None:
        cfg = getattr(getattr(app, "state", None), "lm_config", None)
        if isinstance(cfg, Mapping):
            try:
                runtime = conf.as_float(cfg.get("turn_timeout_s") or 0)
            except (ValueError, TypeError):
                runtime = 0.0
            if runtime > 0:
                return runtime
    try:
        return conf.resolve(
            "limits.turn_timeout_s",
            env="CLIO_GACT_TURN_TIMEOUT_S",
            default=900.0,
            cast=conf.as_float,
        )
    except (ValueError, TypeError):
        return 900.0


def _semantic_trace_detail_level() -> str:
    """Semantic-trace detail level (``trace.detail_level`` / ``CLIO_SEMANTIC_TRACE_DETAIL``).

    Sibling of ``trace.backend``. Resolved file → env → default like every other knob; a
    blank value falls through to the shipped ``DEFAULT_DETAIL_LEVEL``. ``SemanticEventSink``
    normalizes an unknown level back to the default, so this only chooses the raw string.
    """
    return (
        conf.resolve(
            "trace.detail_level",
            env="CLIO_SEMANTIC_TRACE_DETAIL",
            default=DEFAULT_DETAIL_LEVEL,
            cast=conf.as_str,
        ).strip()
        or DEFAULT_DETAIL_LEVEL
    )
