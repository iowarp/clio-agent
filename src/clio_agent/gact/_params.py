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

The module imports only stdlib + the :class:`~clio_agent.gact.types.AgentDef`
type (under ``TYPE_CHECKING``). It never imports :mod:`clio_agent.gact.app`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
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
