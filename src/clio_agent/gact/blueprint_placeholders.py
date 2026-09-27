"""clio-supplied placeholders for Agent Blueprint MCP server declarations.

A blueprint that ships its own MCP implementation (``spotter-ai``'s ``impl/``
project) must name that implementation's location in its launcher. Before this
module the only way to do that was a deployment environment variable
(``${SPOTTER_IMPL_DIR}``) nobody sets on a normal install, or an install path
hard-coded for one OS (``${LOCALAPPDATA}/clio-agent/agent-blueprints/<id>/impl``).
Both are facts clio already knows, so clio supplies them:

``${CLIO_BLUEPRINT_DIR}``
    The directory holding the blueprint's ``AGENT.md`` (installed or
    path-activated), so ``${CLIO_BLUEPRINT_DIR}/impl`` is portable.
``${CLIO_PROVENANCE_CONFIG}``
    A CLIO YAML file carrying clio's EFFECTIVE provenance configuration
    (:mod:`clio_agent.gact.provenance.handoff`), for servers that query the
    same provenance stores clio writes. Materialized only when a declaration
    references it.

Substitution runs on the raw declaration BEFORE the generic ``${VAR}``
expansion (:func:`clio_agent.tools.mcp_config.expand_env`), so every other
variable keeps its ordinary environment semantics, and a clio-supplied value
can itself be the default of an operator override
(``${SPOTTER_IMPL_DIR:-${CLIO_BLUEPRINT_DIR}/impl}``). Each value is also
exported in the server's declared ``env`` so the subprocess can read it.

When a value cannot be supplied (no running app for the provenance handoff),
the placeholder is left untouched: the generic expansion then reports it as an
unset required variable — the same typed refusal as any other unresolvable
declaration, never a silent empty string.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from clio_agent.runtime import trace

BLUEPRINT_DIR_VAR = "CLIO_BLUEPRINT_DIR"
PROVENANCE_CONFIG_VAR = "CLIO_PROVENANCE_CONFIG"
SUPPLIED_VARIABLES = (BLUEPRINT_DIR_VAR, PROVENANCE_CONFIG_VAR)

_PLACEHOLDER = re.compile(r"\$\{(" + "|".join(SUPPLIED_VARIABLES) + r")(?::-[^}]*)?\}")


def references_variable(value: Any, name: str) -> bool:
    """True when ``value`` (any nesting of str/list/mapping) references ``${name}``."""

    if isinstance(value, str):
        return any(match.group(1) == name for match in _PLACEHOLDER.finditer(value))
    if isinstance(value, Mapping):
        return any(references_variable(item, name) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(references_variable(item, name) for item in value)
    return False


def _substitute(value: Any, values: Mapping[str, str]) -> Any:
    """Replace every supplied placeholder in ``value``; leave the rest verbatim."""

    if isinstance(value, str):
        return _PLACEHOLDER.sub(lambda match: values.get(match.group(1), match.group(0)), value)
    if isinstance(value, Mapping):
        return {key: _substitute(item, values) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute(item, values) for item in value]
    if isinstance(value, tuple):
        return tuple(_substitute(item, values) for item in value)
    return value


def _provenance_config(app: Any, workspace_root: Path | None) -> str:
    """Write the provenance handoff and return its path ('' without an app)."""

    if app is None:
        trace.event(
            "BLUEPRINT-PLACEHOLDER",
            "unsupplied variable=%s reason=no_running_app",
            PROVENANCE_CONFIG_VAR,
        )
        return ""
    from clio_agent.gact.provenance.handoff import write_provenance_handoff  # noqa: PLC0415

    path, handoff = write_provenance_handoff(app, workspace_root=workspace_root)
    if handoff.problems:
        trace.event(
            "BLUEPRINT-PLACEHOLDER",
            "provenance_handoff_incomplete path=%s problems=%s",
            path,
            ",".join(problem.code for problem in handoff.problems),
        )
    return str(path)


def supplied_values(
    blueprint_root: Path,
    declarations: Mapping[str, Any],
    *,
    app: Any = None,
    workspace_root: Path | None = None,
    provenance_config: Callable[[Any, Path | None], str] = _provenance_config,
) -> dict[str, str]:
    """The clio-supplied values ``declarations`` reference (and can be supplied)."""

    values: dict[str, str] = {}
    if references_variable(declarations, BLUEPRINT_DIR_VAR):
        values[BLUEPRINT_DIR_VAR] = str(blueprint_root)
    if references_variable(declarations, PROVENANCE_CONFIG_VAR):
        config = provenance_config(app, workspace_root)
        if config:
            values[PROVENANCE_CONFIG_VAR] = config
    return values


def apply_blueprint_placeholders(declaration: Any, values: Mapping[str, str]) -> Any:
    """Substitute supplied values into one declaration and export them in its env.

    A string declaration (``"cmd arg ${CLIO_BLUEPRINT_DIR}/x"``) is substituted
    in place; a mapping additionally carries the values in its ``env`` so the
    subprocess sees them.
    """

    if not values:
        return declaration
    substituted = _substitute(declaration, values)
    if not isinstance(substituted, Mapping):
        return substituted
    spec = dict(substituted)
    if str(spec.get("url") or "").strip():
        return spec
    raw_env = spec.get("env")
    env = dict(raw_env) if isinstance(raw_env, Mapping) else {}
    for name, value in values.items():
        env.setdefault(name, value)
    spec["env"] = env
    return spec


__all__ = [
    "BLUEPRINT_DIR_VAR",
    "PROVENANCE_CONFIG_VAR",
    "SUPPLIED_VARIABLES",
    "apply_blueprint_placeholders",
    "references_variable",
    "supplied_values",
]
