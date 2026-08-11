"""Doctor check: MCP launcher commands declared by loaded packs must be on PATH.

Marketplace packs declare their MCP servers as stdio launcher commands in their
``AGENT.md`` frontmatter (``mcp_servers:``). If a declared launcher is not installed
on PATH, the failure otherwise surfaces only at *first spawn* — deep inside the ReAct
loop, three layers downstream as an opaque ``_UnsupportedSessionAgent`` / "tools
unavailable". :func:`clio_agent.tools.mcp_config.transport_for` already raises a precise
:class:`~clio_agent.tools.mcp_config.MCPSpawnError` at spawn time; this module surfaces
the same reality *before* the first spawn, from the doctor, with an actionable
remediation.

The canonical clio-kit tool launcher (``clio-kit mcp-server <name>``) is provisioned by
``uv tool install clio-kit==2.2.3``; when that launcher is the one missing, the
remediation names that exact command plus the ``uv tool dir --bin`` PATH step.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping
from pathlib import Path

from clio_agent.runtime.status import IntegrationState, IntegrationStatus
from clio_agent.tools.mcp_config import MCPServerSpec

WhichChecker = Callable[[str], str | None]

# The clio-kit tool launcher and its exact provisioning command. Kept in lockstep with
# install/install.sh + install/install.ps1 (which run this same ``uv tool install``) and
# the marketplace packs that launch ``clio-kit mcp-server <name>``.
_CLIO_KIT_LAUNCHER = "clio-kit"
_CLIO_KIT_REMEDIATION = (
    "uv tool install clio-kit==2.2.3, then ensure the directory reported by "
    "`uv tool dir --bin` is on PATH for the clio-agent process."
)


def discover_declared_mcp_servers(
    *, env: Mapping[str, str] | None = None
) -> dict[str, MCPServerSpec]:
    """Discover the MCP servers declared across loaded packs + user/workspace config.

    Mirrors ``ClioAgent._build_tool_gateway`` / the ``/v1/mcp`` route: merge each
    discovered blueprint's ``mcp_servers`` frontmatter (pack scope) with the
    user/workspace ``mcp.yaml`` scopes via
    :func:`clio_agent.tools.mcp_config.load_mcp_servers`.

    Args:
        env: Environment mapping used for ``${VAR}`` expansion in declarations
            (defaults to the process environment when ``None``).

    Returns:
        A ``{name: MCPServerSpec}`` mapping of every declared server.
    """
    from clio_agent.gact.agent_blueprints import discover_agent_blueprints
    from clio_agent.tools.mcp_config import load_mcp_servers

    pack_servers: dict[str, dict[str, object]] = {}
    for blueprint in discover_agent_blueprints():
        servers = blueprint.metadata.get("mcp_servers")
        if isinstance(servers, Mapping) and servers:
            pack_servers[blueprint.id] = {str(k): v for k, v in servers.items()}
    return load_mcp_servers(pack_servers=pack_servers, env=env)


def _remediation_for(command: str) -> str:
    """Return the actionable remediation string for a missing launcher ``command``."""
    if command == _CLIO_KIT_LAUNCHER:
        return _CLIO_KIT_REMEDIATION
    return (
        f"Install the launcher {command!r} and ensure it is on PATH for the "
        "clio-agent process (it backs a declared MCP server)."
    )


def probe_mcp_launchers(
    *,
    specs: Mapping[str, MCPServerSpec] | None = None,
    which: WhichChecker = shutil.which,
    env: Mapping[str, str] | None = None,
) -> list[IntegrationStatus]:
    """Report a structured finding for each declared stdio MCP launcher missing on PATH.

    A doctor sub-check: for every stdio MCP server declared by a loaded pack (or the
    user/workspace ``mcp.yaml``) whose launcher command does not resolve on PATH, emit
    one :class:`~clio_agent.runtime.status.IntegrationStatus` naming the command and its
    remediation. This surfaces — before the first spawn — the exact precondition
    :func:`clio_agent.tools.mcp_config.transport_for` would otherwise only raise at spawn
    time. A launcher that *is* on PATH yields no status (it is not a problem to report).

    Args:
        specs: Declared servers to check; discovered via
            :func:`discover_declared_mcp_servers` when ``None`` (injected for tests).
        which: PATH resolver (defaults to :func:`shutil.which`; injected for tests).
        env: Environment used for declaration discovery when ``specs`` is ``None``.

    Returns:
        Zero or more findings — one per distinct missing launcher command, plus a single
        non-required degraded status if declaration discovery itself failed (a structured
        reason, never a silent swallow).
    """
    if specs is None:
        try:
            specs = discover_declared_mcp_servers(env=env)
        except Exception as exc:  # noqa: BLE001 - surfaced as a structured degraded row
            return [
                IntegrationStatus(
                    name="mcp_launchers",
                    state=IntegrationState.DEGRADED,
                    summary=f"Could not discover declared MCP servers to check launchers: {exc}",
                    config_source="packs+mcp.yaml",
                    next_action=(
                        "Inspect pack/blueprint discovery; MCP launcher provisioning was "
                        "not verified."
                    ),
                    required=False,
                )
            ]

    findings: list[IntegrationStatus] = []
    seen_commands: set[str] = set()
    for spec in specs.values():
        if spec.transport != "stdio" or not spec.command:
            continue
        # A spec that already failed to parse is surfaced by its own validation errors;
        # its command is not a real launcher to probe.
        if spec.validation_errors:
            continue
        if spec.command in seen_commands:
            continue
        if which(spec.command) is not None:
            continue
        seen_commands.add(spec.command)
        findings.append(
            IntegrationStatus(
                name=f"mcp_launcher:{spec.command}",
                state=IntegrationState.UNAVAILABLE,
                summary=(
                    f"MCP launcher {spec.command!r} is declared but not found on PATH; "
                    "the stdio MCP server(s) using it cannot spawn."
                ),
                config_source=spec.source or "packs+mcp.yaml",
                next_action=_remediation_for(spec.command),
                fallback="none",
                required=True,
            )
        )
    return findings


def probe_mcp_yaml_declarations(*, env: Mapping[str, str] | None = None) -> list[IntegrationStatus]:
    """Report a structured finding for every unreadable/malformed ``mcp.yaml`` file (#1201).

    ``mcp_config.py::_read_mcp_yaml`` raises a typed ``MCPConfigError`` on a
    genuine read/parse failure; ``load_mcp_servers`` (called below, via
    :func:`discover_declared_mcp_servers`) catches it per-file and degrades to
    a loud ``logger.warning`` rather than crash boot (RULE 2) -- but a log
    line alone is invisible here. This surfaces the SAME degradation as a
    typed doctor row instead, so a config typo does not silently look like
    "no servers declared" to anyone checking ``clio doctor``.

    Args:
        env: Environment used for declaration discovery (defaults to the
            process environment).

    Returns:
        Zero or more findings -- one per unreadable file.
    """
    from clio_agent.tools.mcp_config import unreadable_mcp_yaml_snapshot

    discover_declared_mcp_servers(env=env)
    return [
        IntegrationStatus(
            name=f"mcp_yaml:{Path(row['path']).name}",
            state=IntegrationState.DEGRADED,
            summary=f"mcp.yaml declaration file unreadable: {row['error']}",
            config_source=row["path"],
            next_action="Fix the YAML syntax (or file permissions) at the path above; the "
            "servers it would have declared are absent until then.",
            required=False,
        )
        for row in unreadable_mcp_yaml_snapshot()
    ]
