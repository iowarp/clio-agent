"""Fresh, isolated MCP readiness checks for an explicitly requested Reload."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from anyio import BrokenResourceError, EndOfStream
from fastmcp.exceptions import ClientError, FastMCPError, MCPError
from httpx import HTTPError

from clio_agent.gact.blueprint_install_files import tree_checksum


@dataclass
class PreparedRuntime:
    """Hold verified descriptor projections until the complete source revision commits."""

    app: Any
    descriptors: dict[str, tuple[dict[str, Any], dict[str, Any] | None]] = field(
        default_factory=dict
    )

    def apply(self) -> None:
        """Swap verified descriptor rows after their installed files commit."""
        servers = getattr(self.app.state, "external_mcp_servers", {})
        for server_id, (_, replacement) in self.descriptors.items():
            if replacement is None:
                servers.pop(server_id, None)
            else:
                servers[server_id] = replacement


_PREPARATION: ContextVar[PreparedRuntime | None] = ContextVar("blueprint_preparation", default=None)


@contextmanager
def runtime_preparation(app: Any, *, enabled: bool) -> Iterator[PreparedRuntime]:
    """Enable real MCP preparation only for an explicit runtime lifecycle action."""
    prepared = PreparedRuntime(app)
    token = _PREPARATION.set(prepared if enabled else None)
    try:
        yield prepared
    finally:
        _PREPARATION.reset(token)


def validation_tool_names(app: Any | None = None) -> frozenset[str]:
    """Use the same mounted host tools for installation and interactive validation."""
    from clio_agent.gact.agent_blueprints import runtime_tool_names_for_validation

    prepared = _PREPARATION.get()
    owner = app if app is not None else prepared.app if prepared is not None else None
    return runtime_tool_names_for_validation(owner)


def prepare_blueprint_runtime(
    staged: Path, *, destination: Path, scope: str, cwd: Path
) -> list[dict[str, Any]]:
    """Initialize and list declared MCPs using fresh clients, never cached readiness.

    Pack placeholders resolve into the staged tree while subprocess working
    directories and config overrides resolve against the actual workspace.
    Listing owns and closes its transports; it cannot disconnect a live turn's
    executor. No tool is invoked by this check.
    """
    prepared = _PREPARATION.get()
    if prepared is None:
        return []
    app = prepared.app
    from clio_agent.gact.agent_blueprints import parse_agent_blueprint_root
    from clio_agent.gact.blueprint_descriptor_reload import prepare_enabled_descriptors

    blueprint = parse_agent_blueprint_root(staged, scope=scope)
    before = tree_checksum(staged)
    checks: list[dict[str, Any]] = []
    for workspace in runtime_workspaces(app, destination, fallback=cwd):
        checks.extend(_prepare_declared(blueprint, app=app, cwd=workspace))
    descriptor_checks, updates = prepare_enabled_descriptors(
        app, staged, destination, scope=scope, cwd=cwd
    )
    checks.extend(descriptor_checks)
    prepared.descriptors.update(updates)
    if tree_checksum(staged) != before:
        raise ValueError("MCP preparation modified staged source files; Reload was not applied")
    return checks


def runtime_workspaces(app: Any, destination: Path, *, fallback: Path) -> list[Path]:
    """Include each workspace currently using this installation's effective MCP configuration."""
    from clio_agent.gact.agents.resolution import _runtime_workspace_catalog_cwd

    store = getattr(app.state, "sessions", None)
    roots: set[Path] = set()
    for session in store.list() if store is not None else []:
        metadata = session.metadata or {}
        active = metadata.get("active_agent_blueprint_path") or metadata.get(
            "active_agent_blueprint_definition_path"
        )
        if not active:
            continue
        path = Path(active)
        path = path.parent if path.name == "AGENT.md" else path
        if path.resolve() == destination.resolve():
            workspace = _runtime_workspace_catalog_cwd(app, session_id=session.id)
            if workspace is None or not workspace.is_dir():
                raise ValueError(
                    f"Workspace for session {session.id} is unavailable; Reload was not applied"
                )
            roots.add(workspace.resolve())
    return sorted(roots or {fallback.resolve()}, key=str)


def _prepare_declared(blueprint: Any, *, app: Any, cwd: Path) -> list[dict[str, Any]]:
    from clio_agent.gact.blueprint_activation import blueprint_server_map
    from clio_agent.tools.gateway import _list_declared_tools
    from clio_agent.tools.mcp_config import load_mcp_servers, specs_from_mapping

    declarations = blueprint_server_map(blueprint, app=app, workspace_root=cwd)
    declared = specs_from_mapping(declarations, source=f"pack:{blueprint.id}")
    for name, declaration in declared.items():
        if not declaration.usable:
            raise ValueError(
                f"MCP {name} declaration is invalid: {'; '.join(declaration.validation_errors)}"
            )
    effective = load_mcp_servers(cwd=cwd, pack_servers={blueprint.id: declarations})
    checks: list[dict[str, Any]] = []
    for name in declared:
        spec = effective.get(name)
        if spec is None:
            checks.append({"namespace": name, "status": "disabled_by_configuration"})
            continue
        if not spec.usable:
            raise ValueError(f"MCP {name} effective configuration is invalid")
        try:
            tools = _list_declared_tools(spec, cwd=str(cwd))
        except (
            OSError,
            ValueError,
            RuntimeError,
            ExceptionGroup,
            FastMCPError,
            MCPError,
            ClientError,
            HTTPError,
            BrokenResourceError,
            EndOfStream,
        ) as exc:
            # Transport errors can contain credential-bearing URLs/headers.
            # Keep the public operation receipt restricted to a typed outcome.
            raise ValueError(
                f"MCP {name} could not initialize/list tools ({type(exc).__name__}); "
                "the previous blueprint revision was retained"
            ) from None
        checks.append({"namespace": name, "status": "ready", "tool_count": len(tools)})
    return [{**row, "workspace": str(cwd)} for row in checks]


def require_unchanged_runtime() -> None:
    """Refuse activation if a user reconfigured an enabled descriptor during preparation."""
    prepared = _PREPARATION.get()
    if prepared is None:
        return
    servers = getattr(prepared.app.state, "external_mcp_servers", {})
    if any(servers.get(key) != original for key, (original, _) in prepared.descriptors.items()):
        raise ValueError(
            "MCP configuration changed during Reload; retry with the current configuration"
        )
