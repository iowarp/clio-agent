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

from clio_agent.runtime import trace
from clio_agent.tools.execution import get_active_tool_blueprint_path

_BLUEPRINT_RESOLUTION_REASON_DEFINITIONS: dict[str, dict[str, str]] = {
    "active_blueprint_path_lookup_failed": {
        "category": "state_unavailable",
        "description": "The active session Agent Blueprint path could not be resolved.",
    },
    "active_blueprint_path_parse_failed": {
        "category": "configuration_invalid",
        "description": "The explicitly activated Agent Blueprint could not be parsed.",
    },
    "active_blueprint_identity_mismatch": {
        "category": "configuration_invalid",
        "description": "The explicitly activated Agent Blueprint id did not match the session.",
    },
    "active_blueprint_disabled": {
        "category": "configuration_invalid",
        "description": "The explicitly activated Agent Blueprint is disabled.",
    },
    "installed_blueprint_discovery_failed": {
        "category": "capability_unavailable",
        "description": "Installed Agent Blueprint discovery failed.",
    },
}


def _reason_catalog(app: Any) -> dict[str, list[dict[str, str]]]:
    """Return the per-session structured blueprint-resolution reason catalog."""

    reasons = getattr(app.state, "blueprint_resolution_reasons", None)
    if not isinstance(reasons, dict):
        reasons = {}
        app.state.blueprint_resolution_reasons = reasons
    return reasons


def blueprint_resolution_reasons(app: Any, sid: str) -> list[dict[str, str]]:
    """Return typed Agent Blueprint resolution degradations recorded for ``sid``."""

    return list(_reason_catalog(app).get(sid, []))


def _record_resolution_reason(
    reason: str,
    blueprint_id: str,
    *,
    exception: Exception | None = None,
) -> None:
    """Record one closed-set resolution reason on trace and the live session API."""

    definition = _BLUEPRINT_RESOLUTION_REASON_DEFINITIONS.get(reason)
    if definition is None:
        raise ValueError(f"Unknown Agent Blueprint resolution reason: {reason}")
    from clio_agent.gact import context as gact_context  # noqa: PLC0415

    row = {"reason": reason, **definition, "blueprint_id": blueprint_id}
    app = gact_context.active_app()
    sid = gact_context.active_session_id()
    if app is not None and sid:
        _reason_catalog(app).setdefault(sid, []).append(row)
        from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415

        _emit_semantic_event(
            app,
            sid,
            "blueprint.resolution.degraded",
            turn_id=gact_context.active_turn_id(),
            trace_id=gact_context.active_trace_id(),
            status="failed",
            summary=definition["description"],
            blueprint={"id": blueprint_id},
            payload=row,
        )
    trace.event(
        "BLUEPRINT-RESOLUTION",
        "degraded reason=%s blueprint_id=%s exception_type=%s exception=%s",
        reason,
        blueprint_id,
        type(exception).__name__ if exception is not None else "",
        str(exception or ""),
    )


def blueprint_mcp_servers(
    blueprint_id: str,
    *,
    cwd: Path | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
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
        blueprints = (
            discover_agent_blueprints(cwd=cwd) if cwd is not None else discover_agent_blueprints()
        )
    except Exception as exc:  # noqa: BLE001 - degradation is typed and served
        _record_resolution_reason(
            "installed_blueprint_discovery_failed", blueprint_id, exception=exc
        )
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
        except Exception as exc:  # noqa: BLE001 - degradation is typed and served
            _record_resolution_reason(
                "active_blueprint_path_lookup_failed", blueprint_id, exception=exc
            )
            if verbose:
                print(f"[ClioAgent] active session blueprint path lookup failed: {exc}")
    if not blueprint_path:
        return None
    try:
        blueprint = parse_agent_blueprint_root(Path(blueprint_path), scope="session")
    except Exception as exc:  # noqa: BLE001 - degradation is typed and served
        _record_resolution_reason("active_blueprint_path_parse_failed", blueprint_id, exception=exc)
        if verbose:
            print(f"[ClioAgent] active blueprint path parse failed: {exc}")
        return {}
    if blueprint.id != blueprint_id:
        _record_resolution_reason("active_blueprint_identity_mismatch", blueprint_id)
        return {}
    if not blueprint.enabled:
        _record_resolution_reason("active_blueprint_disabled", blueprint_id)
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
