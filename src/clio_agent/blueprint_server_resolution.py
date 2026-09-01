"""Resolve MCP servers declared by one active Agent Blueprint."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def discover_blueprint_servers(
    blueprint_id: str,
    *,
    cwd: str | None,
    verbose: bool,
) -> dict[str, dict[str, Any]]:
    """Return declared servers for the active blueprint, or none when inactive."""

    from clio_agent.gact.blueprint_activation import blueprint_mcp_servers  # noqa: PLC0415

    return blueprint_mcp_servers(
        blueprint_id,
        cwd=Path(cwd) if cwd else None,
        verbose=verbose,
    )
