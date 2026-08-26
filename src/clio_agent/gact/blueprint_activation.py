"""Active-blueprint MCP-server resolution — path activation + cache identity.

Extracted from ``ClioAgent._discover_pack_servers`` (no-accretion rule:
agent.py is a runtime host, and these are gact-owned activation decisions).

Two concerns live here:

* :func:`resolve_active_blueprint_servers` — the ACTIVE session's explicitly
  activated blueprint may be identified by an on-disk path (path-activated
  packs, e.g. a marketplace pack launched via ``${SPOTTER_IMPL_DIR}``) rather
  than by an installed-registry id. When such a path is bound (tool contextvar
  first, live-app session resolution as fallback), it decides the declared
  ``mcp_servers`` outright; only when NO path is active does the caller fall
  back to installed-blueprint discovery.
* :func:`blueprint_server_map` — projects one blueprint's ``mcp_servers``
  declarations, stamping the install checksum into each server's declared
  ``env`` (``CLIO_BLUEPRINT_INSTALL_CHECKSUM``). Listing-cache keys include
  the declared environment, and an installed-blueprint update can change tool
  schemas without changing its launcher — the non-secret identity forces the
  first post-update listing to refresh instead of serving a stale cache.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from clio_agent.tools.execution import get_active_tool_blueprint_path


def blueprint_mcp_servers(blueprint_id: str, *, verbose: bool = False) -> dict[str, Any]:
    """Declared MCP servers for ONE activated blueprint — path-first, then installed.

    The complete decision behind ``ClioAgent._discover_pack_servers``: an
    active explicit blueprint path decides outright; otherwise the installed
    registry is consulted for exactly the named blueprint. Empty id or any
    discovery failure degrades to ``{}`` (built-ins only).
    """

    if not blueprint_id:
        return {}
    resolved = resolve_active_blueprint_servers(blueprint_id, verbose=verbose)
    if resolved is not None:
        return resolved
    from clio_agent.gact.agent_blueprints import discover_agent_blueprints  # noqa: PLC0415

    try:
        blueprints = discover_agent_blueprints()
    except Exception as exc:  # noqa: BLE001 - discovery is best-effort
        if verbose:
            print(f"[ClioAgent] blueprint discovery failed: {exc}")
        return {}
    for blueprint in blueprints:
        if blueprint.id != blueprint_id:
            continue
        servers = blueprint_server_map(blueprint)
        if servers:
            return {blueprint.id: servers}
        return {}
    return {}


def resolve_active_blueprint_servers(
    blueprint_id: str, *, verbose: bool = False
) -> dict[str, Any] | None:
    """Resolve declared MCP servers via the active session's explicit blueprint path.

    Returns ``None`` when no explicit path is active — the caller falls back
    to installed-blueprint discovery. Otherwise returns the DECIDED mapping:
    ``{blueprint_id: servers}``, or ``{}`` when the path fails to parse,
    names a different/disabled blueprint, or declares no servers.
    """

    from clio_agent.gact.agent_blueprints import parse_agent_blueprint_root  # noqa: PLC0415

    blueprint_path = get_active_tool_blueprint_path().strip()
    if not blueprint_path:
        try:
            from clio_agent.gact import context as gact_context  # noqa: PLC0415
            from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
                _runtime_active_agent_blueprint_path,
            )

            app = gact_context.active_app()
            session_id = gact_context.active_session_id()
            active_path = (
                _runtime_active_agent_blueprint_path(app, session_id)
                if app is not None and session_id
                else None
            )
            blueprint_path = str(active_path or "").strip()
        except Exception as exc:  # noqa: BLE001 - installed discovery remains available
            if verbose:
                print(f"[ClioAgent] active session blueprint path lookup failed: {exc}")
    if not blueprint_path:
        return None
    try:
        blueprint = parse_agent_blueprint_root(Path(blueprint_path), scope="session")
    except Exception as exc:  # noqa: BLE001 - explicit path degrades to no servers
        if verbose:
            print(f"[ClioAgent] active blueprint path parse failed: {exc}")
        return {}
    if blueprint.id != blueprint_id or not blueprint.enabled:
        return {}
    servers = blueprint_server_map(blueprint)
    if servers:
        return {blueprint.id: servers}
    return {}


def blueprint_server_map(blueprint: Any) -> dict[str, Any]:
    """Return one blueprint's MCP declarations with install-aware cache identity."""

    raw_servers = blueprint.metadata.get("mcp_servers")
    if not isinstance(raw_servers, Mapping):
        return {}
    install = blueprint.metadata.get("install")
    checksum = str(install.get("checksum") or "") if isinstance(install, Mapping) else ""
    servers: dict[str, Any] = {}
    for name, raw_spec in raw_servers.items():
        if not checksum or not isinstance(raw_spec, Mapping):
            servers[str(name)] = raw_spec
            continue
        spec = dict(raw_spec)
        raw_env = spec.get("env")
        env = dict(raw_env) if isinstance(raw_env, Mapping) else {}
        env["CLIO_BLUEPRINT_INSTALL_CHECKSUM"] = checksum
        spec["env"] = env
        servers[str(name)] = spec
    return servers
