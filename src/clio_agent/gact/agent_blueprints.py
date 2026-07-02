"""Markdown Agent Blueprint discovery and installation.

Agent Blueprints are the file-backed definition of a complete CLIO Agent. The
canonical root file is ``AGENT.md`` with Markdown frontmatter. Legacy
``clio-pack.yaml`` Expert Packs remain supported by the older loader and are
adapted at the API boundary rather than duplicated here.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from clio_agent import conf
from clio_agent.gact.expert_packs import (
    ExpertPackDefinition,
    _fallback_expert_id,
    _list_field,
    _parse_frontmatter,
    parse_expert_file,
    validate_expert_hierarchy,
)
from clio_agent.gact.types import AgentDef
from clio_agent.tools.catalog import TOOL_CATALOG

logger = logging.getLogger(__name__)

_BLUEPRINT_ROOT_NAME = "AGENT.md"
_BLUEPRINT_ID_RE = r"^[A-Za-z0-9_.-]+$"
# Keyless https remote so first-run bootstrap works without any SSH identity
# (iowarp/clio-agent#764). Override via config file or env; see
# ``default_registry_url``.
DEFAULT_REGISTRY_URL = "https://github.com/JaimeCernuda/clio-agent-marketplace.git"
_REGISTRY_URL_CONF_KEY = "gact.blueprint_registry.url"
_REGISTRY_URL_ENV = "CLIO_BLUEPRINT_REGISTRY_URL"
DEFAULT_REGISTRY_REF = "main"
# Empty commit => follow the registry ref (main) HEAD instead of a frozen pin.
DEFAULT_REGISTRY_COMMIT = ""
DEFAULT_AGENT_BLUEPRINT_ID = "earthscope-gnss-region"
DEFAULT_REGISTRY_SUBMODULE_PATH = "external/clio-agent-marketplace"
_DEFAULT_BOOTSTRAP_ENV = "CLIO_AGENT_DISABLE_DEFAULT_REGISTRY_BOOTSTRAP"
_DEFAULT_BOOTSTRAP_TIMEOUT_S = 20


@dataclass
class AgentBlueprintDefinition:
    id: str
    version: str
    title: str
    display_name: str
    description: str
    scope: str
    root: Path
    root_path: Path
    root_expert: str = ""
    enabled: bool = True
    validation_errors: list[str] = field(default_factory=list)
    defaults: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["root"] = str(self.root)
        payload["root_path"] = str(self.root_path)
        payload["definition_path"] = str(self.root_path)
        # kind discriminator (iowarp/clio-agent#663): a *blueprint* is a
        # structured workflow with a root orchestrator (root_expert set); a
        # *pack* is a loose collection of experts with no orchestrator root.
        # Same install/update/delete lifecycle; the kind is a property of the
        # installed artifact, surfaced so the UI can render and filter them.
        payload["kind"] = "blueprint" if str(self.root_expert).strip() else "pack"
        payload["name"] = self.display_name or self.title or self.id
        return payload


def agent_blueprint_roots(home: Path, cwd: Path) -> list[tuple[Path, str]]:
    from clio_agent import paths  # noqa: PLC0415 - avoid import cycle at module load

    config_root = paths.user_config_dir_for(home, os.environ)
    return [
        (config_root / "agent-blueprints", "global"),
        (cwd / ".clio" / "agent-blueprints", "workspace"),
    ]


def builtin_agent_blueprints_root() -> Path:
    """Return the retired pre-#629 in-repo blueprint root.

    Kept only for migration diagnostics. Runtime discovery intentionally does
    not include this path; default agents must be installed from the pinned
    default registry and therefore carry registry install provenance.
    """

    return Path(__file__).resolve().parents[1] / "agent_blueprints" / "builtin"


def default_registry_url() -> str:
    """Resolve the default blueprint registry URL.

    Precedence follows :func:`clio_agent.conf.resolve` (config file over env
    over in-code default): the ``gact.blueprint_registry.url`` key in
    ``config.yaml``, then the ``CLIO_BLUEPRINT_REGISTRY_URL`` environment
    variable, then :data:`DEFAULT_REGISTRY_URL`. A configured-but-blank value
    is a degraded path: it falls back to the default and logs a structured
    warning rather than attempting a clone from an empty remote.
    """

    resolved = str(
        conf.resolve(
            _REGISTRY_URL_CONF_KEY,
            env=_REGISTRY_URL_ENV,
            default=DEFAULT_REGISTRY_URL,
            cast=conf.as_str,
        )
    ).strip()
    if not resolved:
        logger.warning(
            "blueprint_registry_url_fallback reason=blank_configured_value key=%s env=%s "
            "falling back to default %s",
            _REGISTRY_URL_CONF_KEY,
            _REGISTRY_URL_ENV,
            DEFAULT_REGISTRY_URL,
        )
        return DEFAULT_REGISTRY_URL
    return resolved


def default_registry_metadata() -> dict[str, str]:
    """Return the pinned default registry bootstrap contract."""

    return {
        "source": default_registry_url(),
        "ref": DEFAULT_REGISTRY_REF,
        "commit": DEFAULT_REGISTRY_COMMIT,
        "default_agent_blueprint_id": DEFAULT_AGENT_BLUEPRINT_ID,
        "submodule_path": DEFAULT_REGISTRY_SUBMODULE_PATH,
    }


def default_registry_install_source() -> str:
    """Return the preferred local source for the pinned default registry.

    Development checkouts may carry the marketplace as a git submodule. When
    present, use it as the install source so first-run bootstrap does not depend
    on network access. Packaged installs without the submodule still clone the
    pinned registry URL. An explicit registry override (config file or
    ``CLIO_BLUEPRINT_REGISTRY_URL``) always wins over the local submodule.
    """

    url = default_registry_url()
    if url != DEFAULT_REGISTRY_URL:
        return url
    repo_root = Path(__file__).resolve().parents[3]
    submodule = repo_root / DEFAULT_REGISTRY_SUBMODULE_PATH
    if submodule.is_dir():
        return str(submodule)
    return url


def discover_agent_blueprints(
    *,
    home: Path | None = None,
    cwd: Path | None = None,
) -> list[AgentBlueprintDefinition]:
    home = home or Path.home()
    cwd = cwd or Path(os.getcwd())
    bootstrap_diagnostic = ensure_default_registry_bootstrap(home=home, cwd=cwd)
    blueprints: list[AgentBlueprintDefinition] = []
    for root, scope in agent_blueprint_roots(home, cwd):
        if not root.exists() or not root.is_dir():
            continue
        candidates: list[Path] = []
        if (root / _BLUEPRINT_ROOT_NAME).exists():
            candidates.append(root)
        candidates.extend(
            path
            for path in sorted(root.iterdir())
            if path.is_dir() and (path / _BLUEPRINT_ROOT_NAME).exists()
        )
        for candidate in candidates:
            blueprints.append(parse_agent_blueprint_root(candidate, scope=scope))
    if bootstrap_diagnostic and not any(row.id == DEFAULT_AGENT_BLUEPRINT_ID for row in blueprints):
        install_root = (
            _install_root(home=home, cwd=cwd, scope="global") / DEFAULT_AGENT_BLUEPRINT_ID
        )
        blueprints.append(
            AgentBlueprintDefinition(
                id=DEFAULT_AGENT_BLUEPRINT_ID,
                version="",
                title="Default registry Agent Blueprint",
                display_name="Default registry Agent Blueprint",
                description="Pinned default registry bootstrap did not produce an installed blueprint.",
                scope="global",
                root=install_root,
                root_path=install_root / _BLUEPRINT_ROOT_NAME,
                enabled=False,
                validation_errors=[bootstrap_diagnostic],
                metadata={
                    "layout": "agent_blueprint",
                    "install": default_registry_metadata(),
                    "bootstrap": {"status": "failed", "diagnostic": bootstrap_diagnostic},
                },
            )
        )
    return blueprints


def ensure_default_registry_bootstrap(
    *,
    home: Path | None = None,
    cwd: Path | None = None,
) -> str:
    """Install the pinned default registry snapshot if the default agent is absent.

    Returns an empty string on success or when bootstrap is disabled, otherwise
    a human-readable diagnostic. Discovery surfaces the diagnostic as a disabled
    blueprint row instead of silently falling back to bundled domain experts.
    """

    if str(os.environ.get(_DEFAULT_BOOTSTRAP_ENV) or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return ""
    home = home or Path.home()
    cwd = cwd or Path(os.getcwd())
    pinned = DEFAULT_REGISTRY_COMMIT.strip()
    root = _install_root(home=home, cwd=cwd, scope="global") / DEFAULT_AGENT_BLUEPRINT_ID
    if (root / _BLUEPRINT_ROOT_NAME).exists():
        # HEAD-following mode (no pinned commit): any installed snapshot is
        # acceptable; we track the registry ref rather than a frozen commit.
        if not pinned:
            return ""
        metadata = read_install_metadata(root)
        installed_commit = str(metadata.get("commit") or "").strip()
        if installed_commit == pinned:
            return ""
        if installed_commit:
            return (
                f"default registry pin mismatch for {DEFAULT_AGENT_BLUEPRINT_ID}: "
                f"expected {pinned}, found {installed_commit}"
            )
        return f"default registry install metadata missing pinned commit for {DEFAULT_AGENT_BLUEPRINT_ID}"
    try:
        install_agent_blueprint(
            source=default_registry_install_source(),
            scope="global",
            cwd=cwd,
            home=home,
            ref=DEFAULT_REGISTRY_REF,
            blueprint_id=DEFAULT_AGENT_BLUEPRINT_ID,
            pinned_commit=pinned,
        )
    except Exception as exc:  # noqa: BLE001
        target = pinned or DEFAULT_REGISTRY_REF
        return f"unable to install default registry {default_registry_url()}@{target}: {exc}"
    return ""


def parse_agent_blueprint_root(root: Path, *, scope: str) -> AgentBlueprintDefinition:
    root = root.expanduser()
    path = root / _BLUEPRINT_ROOT_NAME if root.is_dir() else root
    if not path.exists():
        return AgentBlueprintDefinition(
            id=_fallback_expert_id(root),
            version="",
            title=root.name,
            display_name=root.name,
            description="",
            scope=scope,
            root=root,
            root_path=path,
            enabled=False,
            validation_errors=[f"missing blueprint root: {_BLUEPRINT_ROOT_NAME}"],
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return AgentBlueprintDefinition(
            id=_fallback_expert_id(root),
            version="",
            title=root.name,
            display_name=root.name,
            description="",
            scope=scope,
            root=root,
            root_path=path,
            enabled=False,
            validation_errors=[f"unable to read blueprint root: {exc}"],
        )
    meta, body = _parse_frontmatter(text)
    errors: list[str] = []
    blueprint_id = str(meta.get("id") or meta.get("name") or "").strip()
    if not blueprint_id:
        blueprint_id = _fallback_expert_id(root)
        errors.append("missing required blueprint field: id")
    elif not re.fullmatch(_BLUEPRINT_ID_RE, blueprint_id):
        errors.append("invalid blueprint id; use letters, numbers, dots, underscores, and hyphens")
    raw_defaults = meta.get("defaults")
    defaults = raw_defaults if isinstance(raw_defaults, dict) else {}
    requirements = meta.get("requires") if isinstance(meta.get("requires"), dict) else {}
    install_metadata = read_install_metadata(path.parent)
    title = str(meta.get("title") or blueprint_id).strip()
    display_name = str(meta.get("display_name") or title).strip()
    return AgentBlueprintDefinition(
        id=blueprint_id,
        version=str(meta.get("version") or "").strip(),
        title=title,
        display_name=display_name,
        description=str(meta.get("description") or "").strip(),
        scope=scope,
        root=path.parent,
        root_path=path,
        root_expert=str(
            meta.get("root_expert")
            or meta.get("root")
            or meta.get("default_expert")
            or meta.get("default_root_expert")
            or ""
        ).strip(),
        enabled=not errors,
        validation_errors=errors,
        defaults={str(k): v for k, v in defaults.items()},
        metadata={
            "layout": "agent_blueprint",
            "body": body.strip(),
            "compatibility": meta.get("compatibility")
            if isinstance(meta.get("compatibility"), dict)
            else {},
            "requires": requirements,
            "mcp_servers": meta.get("mcp_servers")
            if isinstance(meta.get("mcp_servers"), dict)
            else {},
            "includes": _list_field(meta, "includes"),
            "blueprint": meta.get("blueprint") if isinstance(meta.get("blueprint"), dict) else {},
            "install": install_metadata
            or (meta.get("install") if isinstance(meta.get("install"), dict) else {}),
            "default_registry": default_registry_metadata()
            if blueprint_id == DEFAULT_AGENT_BLUEPRINT_ID
            else {},
        },
    )


def load_agent_blueprint_path(path: Path, *, scope: str = "session") -> list[AgentDef]:
    blueprint = parse_agent_blueprint_root(path, scope=scope)
    return _load_blueprint_agents(blueprint)


def load_agent_blueprints(
    *,
    home: Path | None = None,
    cwd: Path | None = None,
    blueprint_id: str = "",
) -> list[AgentDef]:
    rows: list[AgentDef] = []
    for blueprint in discover_agent_blueprints(home=home, cwd=cwd):
        if blueprint_id and blueprint.id != blueprint_id:
            continue
        rows.extend(_load_blueprint_agents(blueprint))
    return rows


def validate_agent_blueprint_path(path: Path, *, scope: str = "session") -> dict[str, Any]:
    blueprint = parse_agent_blueprint_root(path, scope=scope)
    mcp_descriptors = load_mcp_descriptors(blueprint.root, scope=scope, blueprint_id=blueprint.id)
    hook_descriptors = load_hook_descriptors(blueprint.root, scope=scope, blueprint_id=blueprint.id)
    declared_servers = blueprint.metadata.get("mcp_servers")
    declared_server_names = (
        list(declared_servers.keys()) if isinstance(declared_servers, dict) else []
    )
    rows = _validate_agent_tool_references(
        validate_agent_hierarchy(_load_blueprint_agents(blueprint), blueprint=blueprint),
        mcp_descriptors=mcp_descriptors,
        declared_server_names=declared_server_names,
    )
    errors = list(blueprint.validation_errors)
    warnings: list[str] = []
    for row in rows:
        errors.extend(f"{row.id}: {error}" for error in row.validation_errors)
    for descriptor in mcp_descriptors:
        errors.extend(
            f"{descriptor.get('id', 'mcp')}: {error}"
            for error in descriptor.get("validation_errors", [])
        )
        warnings.extend(
            f"{descriptor.get('id', 'mcp')}: {warning}"
            for warning in descriptor.get("validation_warnings", [])
        )
    for descriptor in hook_descriptors:
        errors.extend(
            f"{descriptor.get('id', 'hook')}: {error}"
            for error in descriptor.get("validation_errors", [])
        )
        warnings.extend(
            f"{descriptor.get('id', 'hook')}: {warning}"
            for warning in descriptor.get("validation_warnings", [])
        )
    return {
        "agent_blueprint": blueprint.to_wire(),
        "agents": [row.model_dump(exclude_none=True) for row in rows],
        "mcp_descriptors": mcp_descriptors,
        "hook_descriptors": hook_descriptors,
        "enabled": blueprint.enabled and not errors,
        "validation_errors": errors,
        "validation_warnings": warnings,
    }


def validate_agent_hierarchy(
    rows: list[AgentDef],
    *,
    blueprint: AgentBlueprintDefinition | None = None,
) -> list[AgentDef]:
    validated = validate_expert_hierarchy(rows, known_parent_ids=set())
    if blueprint is None:
        return validated
    ids = {row.id for row in validated}
    root_expert = blueprint.root_expert
    extra_errors: dict[str, list[str]] = {}
    if root_expert and root_expert not in ids:
        extra_errors.setdefault(f"{blueprint.id}.manifest", []).append(
            f"root_expert not found: {root_expert}"
        )
    elif not root_expert:
        roots = [row.id for row in validated if not row.parent_id]
        if len(roots) != 1:
            target = roots[0] if roots else f"{blueprint.id}.manifest"
            extra_errors.setdefault(target, []).append(
                "blueprint must declare root_expert or contain one root expert"
            )
    out: list[AgentDef] = []
    for row in validated:
        errors = [*row.validation_errors, *extra_errors.get(row.id, [])]
        out.append(
            row.model_copy(
                update={"enabled": row.enabled and not errors, "validation_errors": errors}
            )
        )
    if f"{blueprint.id}.manifest" in extra_errors:
        out.append(
            AgentDef(
                id=f"{blueprint.id}.manifest",
                source="expert_pack",
                title=f"{blueprint.title} manifest",
                description=blueprint.description,
                enabled=False,
                validation_errors=extra_errors[f"{blueprint.id}.manifest"],
                metadata={
                    "agent_blueprint_id": blueprint.id,
                    "agent_blueprint_scope": blueprint.scope,
                    "definition_path": str(blueprint.root_path),
                },
            )
        )
    return out


_MEMORY_TOOL_NAMES = {
    "memory_search_sessions",
    "memory_read_session_summary",
    "memory_read_context_frame",
}

_HOOK_EVENT_NAMES = {
    "pre_tool",
    "post_tool",
    "pre_message",
    "post_message",
    "semantic_event",
    "on_error",
}


def _mapping_field(meta: dict[str, Any], *keys: str) -> dict[str, Any]:
    for key in keys:
        value = meta.get(key)
        if isinstance(value, dict):
            return {str(k): v for k, v in value.items()}
    return {}


def _mcp_stdio_spec_from_metadata(
    meta: dict[str, Any],
    *,
    root: Path,
) -> tuple[str, list[str], list[str]]:
    """Build a stdio command from direct or self-contained install metadata."""

    warnings: list[str] = []
    runtime = _mapping_field(meta, "runtime", "run")
    install = _mapping_field(meta, "install", "deployment")
    command = str(meta.get("command") or runtime.get("command") or "").strip()
    args = _list_field(meta, "args") or _list_field(runtime, "args")
    if command:
        return command, args, warnings

    method = str(
        install.get("method") or install.get("type") or install.get("manager") or ""
    ).strip()
    package = str(
        install.get("package") or install.get("name") or install.get("binary") or ""
    ).strip()
    if method in {"uvx", "npx"} and package:
        return method, [package, *args], warnings
    if method in {"binary", "command"} and package:
        return package, args, warnings
    if method in {"local", "pack-local", "python"}:
        script = _local_mcp_script_from_metadata(meta)
        if script:
            resolved = root / script
            return "python", [str(resolved), *args], warnings
    if method:
        warnings.append(f"unsupported MCP install method for stdio launch derivation: {method}")
    return "", args, warnings


def _local_mcp_script_from_metadata(meta: dict[str, Any]) -> str:
    install = _mapping_field(meta, "install", "deployment")
    runtime = _mapping_field(meta, "runtime", "run")
    method = str(
        install.get("method") or install.get("type") or install.get("manager") or ""
    ).strip()
    if method not in {"local", "pack-local", "python"}:
        return ""
    return str(
        install.get("path")
        or install.get("script")
        or runtime.get("path")
        or runtime.get("script")
        or ""
    ).strip()


def _validate_agent_tool_references(
    rows: list[AgentDef],
    *,
    mcp_descriptors: list[dict[str, Any]],
    declared_server_names: Iterable[str] = (),
) -> list[AgentDef]:
    # Built-in tools are the universal in-process defaults (fs/shell) plus the
    # memory tools. Everything else is a declared MCP tool: a reference is valid
    # iff the pack declares its server namespace via ``mcp_servers`` (declaration
    # is the enablement). Legacy ``tools/*.md`` descriptors remain explicitly
    # gated until enabled/trusted.
    builtin_tools = set(TOOL_CATALOG) | _MEMORY_TOOL_NAMES
    declared_namespaces = {str(n).strip() for n in declared_server_names if str(n).strip()}
    descriptor_tools: dict[str, dict[str, Any]] = {}
    for descriptor in mcp_descriptors:
        for tool in descriptor.get("tools") or []:
            if not isinstance(tool, dict):
                continue
            tool_id = str(tool.get("id") or tool.get("name") or "").strip()
            if tool_id:
                descriptor_tools[tool_id] = descriptor
    out: list[AgentDef] = []
    for row in rows:
        errors = list(row.validation_errors)
        diagnostics = list(row.metadata.get("tool_diagnostics", []))
        for tool_name in row.tools:
            if tool_name in builtin_tools:
                continue
            namespace = tool_name.split("_", 1)[0] if "_" in tool_name else tool_name
            if namespace in declared_namespaces:
                # Declared via the pack's mcp_servers map; declaration enables it.
                continue
            descriptor_match = descriptor_tools.get(tool_name)
            if descriptor_match is not None:
                descriptor_id = str(descriptor_match.get("id") or "")
                errors.append(
                    f"MCP tool requires explicit enablement: {tool_name}"
                    + (f" (descriptor: {descriptor_id})" if descriptor_id else "")
                )
                diagnostics.append(
                    {
                        "tool": tool_name,
                        "status": "disabled",
                        "source": "agent_blueprint_mcp_descriptor",
                        "descriptor_id": descriptor_id,
                    }
                )
                continue
            errors.append(f"unknown tool reference: {tool_name}")
            diagnostics.append(
                {
                    "tool": tool_name,
                    "status": "missing",
                    "source": "agent_blueprint",
                }
            )
        metadata = dict(row.metadata)
        if diagnostics:
            metadata["tool_diagnostics"] = diagnostics
        out.append(
            row.model_copy(
                update={
                    "metadata": metadata,
                    "enabled": row.enabled and not errors,
                    "validation_errors": errors,
                }
            )
        )
    return out


def load_mcp_descriptors(
    root: Path,
    *,
    scope: str,
    blueprint_id: str,
) -> list[dict[str, Any]]:
    tools_root = root / "tools"
    if not tools_root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(tools_root.rglob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
            meta, body = _parse_frontmatter(text)
        except OSError as exc:
            rows.append(
                {
                    "id": _fallback_expert_id(path),
                    "name": path.stem,
                    "status": "unavailable",
                    "enabled": False,
                    "source": "agent_blueprint",
                    "validation_errors": [f"unable to read MCP descriptor: {exc}"],
                }
            )
            continue
        descriptor_id = str(meta.get("id") or meta.get("name") or path.stem).strip()
        transport = str(meta.get("transport") or "").strip()
        errors: list[str] = []
        if not descriptor_id:
            errors.append("missing required MCP descriptor field: id")
            descriptor_id = _fallback_expert_id(path)
        if not transport:
            errors.append("missing required MCP descriptor field: transport")
        if transport not in {"", "stdio", "http", "streamable-http"}:
            errors.append(f"unsupported MCP descriptor transport: {transport}")
        command, args, install_warnings = _mcp_stdio_spec_from_metadata(meta, root=root)
        local_script = _local_mcp_script_from_metadata(meta)
        if local_script and not (root / local_script).is_file():
            errors.append(f"pack-local MCP launch path not found: {local_script}")
        if transport == "stdio" and not command:
            errors.append("stdio MCP descriptors require command")
        if transport in {"http", "streamable-http"} and not str(meta.get("url") or "").strip():
            errors.append(f"{transport} MCP descriptors require url")
        warnings: list[str] = []
        warnings.extend(install_warnings)
        if transport:
            warnings.append("MCP descriptors are disabled until explicitly enabled and trusted")
        install_metadata = _mapping_field(meta, "install", "deployment")
        runtime_metadata = _mapping_field(meta, "runtime", "run")
        trust_metadata = _mapping_field(meta, "trust")
        env_policy = _mapping_field(meta, "env_policy", "env-policy", "environment")
        verification = _mapping_field(meta, "verification", "verify", "probe")
        rows.append(
            {
                "id": descriptor_id,
                "name": str(meta.get("name") or descriptor_id),
                "title": str(meta.get("title") or descriptor_id),
                "description": str(meta.get("description") or body.strip()),
                "transport": transport,
                "command": command,
                "args": args,
                "install": install_metadata,
                "runtime": runtime_metadata,
                "trust": {
                    "policy": str(trust_metadata.get("policy") or "explicit"),
                    "trusted": False,
                    **trust_metadata,
                },
                "env_policy": env_policy,
                "verification": verification,
                "tools": [
                    {
                        "id": tool_name,
                        "name": tool_name,
                        "status": "disabled",
                        "enabled": False,
                        "source": "agent_blueprint_mcp_descriptor",
                    }
                    for tool_name in _list_field(meta, "tools")
                ],
                "url": str(meta.get("url") or ""),
                "enabled": False,
                "status": "disabled",
                "source": "agent_blueprint",
                "scope": scope,
                "agent_blueprint_id": blueprint_id,
                "definition_path": str(path),
                "validation_errors": errors,
                "validation_warnings": warnings,
            }
        )
    return rows


def load_hook_descriptors(
    root: Path,
    *,
    scope: str,
    blueprint_id: str,
) -> list[dict[str, Any]]:
    """Discover disabled packaged Python hooks in an Agent Blueprint."""

    hooks_root = root / "hooks"
    if not hooks_root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(hooks_root.glob("*.py")):
        event = path.stem.strip()
        errors: list[str] = []
        warnings = ["Blueprint packaged hooks are disabled until explicitly enabled and trusted"]
        if event not in _HOOK_EVENT_NAMES:
            errors.append(f"unsupported hook event: {event}")
        try:
            data = path.read_bytes()
        except OSError as exc:
            data = b""
            errors.append(f"unable to read hook file: {exc}")
        rows.append(
            {
                "id": event,
                "name": event,
                "title": event.replace("_", " ").title(),
                "event": event,
                "status": "disabled",
                "enabled": False,
                "source": "agent_blueprint",
                "scope": scope,
                "agent_blueprint_id": blueprint_id,
                "definition_path": str(path),
                "checksum": hashlib.sha256(data).hexdigest() if data else "",
                "trust": {
                    "policy": "explicit",
                    "trusted": False,
                },
                "validation_errors": errors,
                "validation_warnings": warnings,
            }
        )
    return rows


def install_agent_blueprint(
    *,
    source: str,
    scope: Literal["global", "workspace"],
    cwd: Path,
    home: Path | None = None,
    ref: str = "",
    blueprint_id: str = "",
    pinned_commit: str = "",
) -> dict[str, Any]:
    home = home or Path.home()
    install_root = _install_root(home=home, cwd=cwd, scope=scope)
    install_root.mkdir(parents=True, exist_ok=True)
    source_path = Path(source).expanduser()
    with tempfile.TemporaryDirectory(prefix="clio-agent-blueprint-") as tmp:
        tmp_path = Path(tmp)
        resolved_source: Path
        source_kind = "path"
        commit = ""
        if source_path.exists():
            resolved_source = source_path
            try:
                commit = subprocess.check_output(
                    ["git", "-C", str(source_path), "rev-parse", "HEAD"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                ).strip()
            except Exception:
                commit = ""
            if pinned_commit and commit and commit != pinned_commit:
                raise ValueError(f"registry pin mismatch: expected {pinned_commit}, found {commit}")
        else:
            source_kind = "git"
            clone_target = tmp_path / "repo"
            cmd = ["git", "clone", "--depth", "1"]
            if ref:
                cmd.extend(["--branch", ref])
            cmd.extend([source, str(clone_target)])
            env = {
                **os.environ,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
            }
            subprocess.run(
                cmd,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=_DEFAULT_BOOTSTRAP_TIMEOUT_S,
                env=env,
            )
            resolved_source = clone_target
            commit = subprocess.check_output(
                ["git", "-C", str(clone_target), "rev-parse", "HEAD"],
                text=True,
            ).strip()
            if pinned_commit and commit != pinned_commit:
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(clone_target),
                        "fetch",
                        "--depth",
                        "1",
                        "origin",
                        pinned_commit,
                    ],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=_DEFAULT_BOOTSTRAP_TIMEOUT_S,
                    env=env,
                )
                subprocess.run(
                    ["git", "-C", str(clone_target), "checkout", "--detach", pinned_commit],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=_DEFAULT_BOOTSTRAP_TIMEOUT_S,
                    env=env,
                )
                commit = subprocess.check_output(
                    ["git", "-C", str(clone_target), "rev-parse", "HEAD"],
                    text=True,
                ).strip()
            if pinned_commit and commit != pinned_commit:
                raise ValueError(f"registry pin mismatch: expected {pinned_commit}, found {commit}")
        candidates = _install_candidates(resolved_source, blueprint_id=blueprint_id)
        if not candidates:
            raise ValueError("source contains no Agent Blueprint folders with AGENT.md")
        installed: list[dict[str, Any]] = []
        for candidate in candidates:
            parsed = parse_agent_blueprint_root(candidate, scope=scope)
            if not parsed.enabled:
                raise ValueError("; ".join(parsed.validation_errors))
            dest = install_root / parsed.id
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(candidate, dest)
            metadata = {
                "source": source,
                "source_kind": source_kind,
                "ref": ref,
                "commit": commit,
                "pinned_commit": pinned_commit,
                "installed_at": datetime.now(UTC).isoformat(),
                "checksum": _tree_checksum(dest),
                "scope": scope,
            }
            _write_install_metadata(dest, metadata)
            installed.append(
                {**parse_agent_blueprint_root(dest, scope=scope).to_wire(), "install": metadata}
            )
        return {"installed": installed}


def update_installed_agent_blueprint(
    *,
    blueprint_id: str,
    scope: Literal["global", "workspace"],
    cwd: Path,
    home: Path | None = None,
) -> dict[str, Any]:
    root = _install_root(home=home or Path.home(), cwd=cwd, scope=scope) / blueprint_id
    metadata = read_install_metadata(root)
    source = str(metadata.get("source") or "").strip()
    if not source:
        raise ValueError(f"agent blueprint {blueprint_id!r} has no install source metadata")
    return install_agent_blueprint(
        source=source,
        scope=scope,
        cwd=cwd,
        home=home,
        ref=str(metadata.get("ref") or ""),
        blueprint_id=blueprint_id,
    )


def uninstall_agent_blueprint(
    *,
    blueprint_id: str,
    scope: Literal["global", "workspace"],
    cwd: Path,
    home: Path | None = None,
) -> dict[str, Any]:
    root = _install_root(home=home or Path.home(), cwd=cwd, scope=scope) / blueprint_id
    if not root.exists():
        raise FileNotFoundError(f"installed agent blueprint not found: {blueprint_id}")
    shutil.rmtree(root)
    return {"uninstalled": {"id": blueprint_id, "scope": scope, "root": str(root)}}


def read_install_metadata(root: Path) -> dict[str, str]:
    path = root / ".clio-install.md"
    if not path.exists():
        return {}
    rows: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw or raw.startswith("#") or ":" not in raw:
            continue
        key, _, value = raw.partition(":")
        rows[key.strip()] = value.strip()
    return rows


def _install_root(*, home: Path, cwd: Path, scope: str) -> Path:
    from clio_agent import paths  # noqa: PLC0415 - avoid import cycle at module load

    config_root = paths.user_config_dir_for(home, os.environ)
    if scope == "global":
        return config_root / "agent-blueprints"
    if scope == "workspace":
        return cwd / ".clio" / "agent-blueprints"
    raise ValueError("scope must be global or workspace")


def _install_candidates(source: Path, *, blueprint_id: str = "") -> list[Path]:
    candidates: list[Path] = []
    if (source / _BLUEPRINT_ROOT_NAME).exists():
        candidates.append(source)
    candidates.extend(
        path
        for path in sorted(source.iterdir())
        if path.is_dir() and (path / _BLUEPRINT_ROOT_NAME).exists()
    )
    if blueprint_id:
        candidates = [
            path
            for path in candidates
            if parse_agent_blueprint_root(path, scope="install").id == blueprint_id
        ]
    return candidates


def _write_install_metadata(root: Path, metadata: dict[str, Any]) -> None:
    lines = ["# CLIO Agent Blueprint install metadata", ""]
    for key, value in metadata.items():
        lines.append(f"{key}: {value}")
    (root / ".clio-install.md").write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def _tree_checksum(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if path.name == ".clio-install.md":
            continue
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _load_blueprint_agents(blueprint: AgentBlueprintDefinition) -> list[AgentDef]:
    expert_root = blueprint.root / "experts"
    search_roots = [expert_root if expert_root.is_dir() else blueprint.root]
    included_roots: dict[Path, str] = {}
    root_resolved = blueprint.root.resolve()
    for raw_include in blueprint.metadata.get("includes") or []:
        include_path = (blueprint.root / str(raw_include)).resolve()
        try:
            include_path.relative_to(root_resolved)
        except ValueError:
            continue
        if include_path.exists():
            search_roots.append(include_path)
            included_roots[include_path] = str(raw_include)
    seen: set[Path] = set()
    files: list[Path] = []
    for search_root in search_roots:
        candidates = [search_root] if search_root.is_file() else sorted(search_root.rglob("*.md"))
        for path in candidates:
            normalized = path.resolve()
            if normalized in seen:
                continue
            seen.add(normalized)
            relative = "/" + normalized.relative_to(root_resolved).as_posix()
            if (
                path.name == _BLUEPRINT_ROOT_NAME
                or "/prompts/" in relative
                or "/commands/" in relative
                or "/skills/" in relative
                or "/tools/" in relative
                or "/profiles/" in relative
            ):
                continue
            files.append(path)
    pack = ExpertPackDefinition(
        id=blueprint.id,
        version=blueprint.version,
        title=blueprint.title,
        description=blueprint.description,
        scope=blueprint.scope,
        root=blueprint.root,
        manifest_path=blueprint.root_path,
        enabled=blueprint.enabled,
        validation_errors=list(blueprint.validation_errors),
        defaults=dict(blueprint.defaults),
        metadata={"default_root_expert": blueprint.root_expert, "layout": "agent_blueprint"},
    )
    rows = [parse_expert_file(path, scope=blueprint.scope, pack=pack) for path in files]
    out: list[AgentDef] = []
    for row in rows:
        row_path = Path(str(row.metadata.get("definition_path") or "")).resolve()
        include_source = ""
        for include_root, raw_include in included_roots.items():
            try:
                row_path.relative_to(include_root)
            except ValueError:
                continue
            include_source = raw_include
            break
        metadata = {
            **row.metadata,
            "agent_blueprint_id": blueprint.id,
            "agent_blueprint_version": blueprint.version,
            "agent_blueprint_title": blueprint.title,
            "agent_blueprint_display_name": blueprint.display_name,
            "agent_blueprint_scope": blueprint.scope,
            "agent_blueprint_root_expert": blueprint.root_expert,
            "agent_blueprint_definition_path": str(blueprint.root_path),
            "definition_kind": "agent_blueprint",
            "install": dict(blueprint.metadata.get("install") or {}),
        }
        if include_source:
            metadata["agent_blueprint_include"] = include_source
        out.append(row.model_copy(update={"metadata": metadata}))
    if not out:
        out.append(
            AgentDef(
                id=f"{blueprint.id}.empty",
                source="expert_pack",
                # Don't double the suffix when the title already ends in
                # "Blueprint" (iowarp/clio-agent#649).
                title=(
                    blueprint.title
                    if blueprint.title.rstrip().endswith("Blueprint")
                    else f"{blueprint.title} Blueprint"
                ),
                description=blueprint.description,
                metadata={
                    "agent_blueprint_id": blueprint.id,
                    "agent_blueprint_scope": blueprint.scope,
                    "definition_path": str(blueprint.root_path),
                },
                enabled=False,
                validation_errors=["agent blueprint has no expert Markdown files"],
            )
        )
    return out
