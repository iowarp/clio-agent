"""Markdown Agent Blueprint discovery and installation.

Agent Blueprints are the file-backed definition of a complete CLIO Agent. The
canonical root file is ``AGENT.md`` with Markdown frontmatter. Legacy
``clio-pack.yaml`` Expert Packs remain supported by the older loader and are
adapted at the API boundary rather than duplicated here.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

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

_BLUEPRINT_ROOT_NAME = "AGENT.md"
_BLUEPRINT_ID_RE = r"^[A-Za-z0-9_.-]+$"
_BLUEPRINT_FORMAT_V1 = "agent-blueprint-v1"
_SUPPORTED_BLUEPRINT_FIELDS = {
    "id",
    "name",
    "version",
    "title",
    "description",
    "root_expert",
    "root",
    "default_expert",
    "default_root_expert",
    "blueprint",
    "defaults",
    "requires",
    "compatibility",
    "install",
}
_SUPPORTED_EXPERT_FIELDS = {
    "id",
    "name",
    "title",
    "description",
    "parent_id",
    "parent",
    "tier",
    "specialization",
    "keywords",
    "tags",
    "tools",
    "allowed_tools",
    "allowed-tools",
    "skills",
    "commands",
    "capability_refs",
    "capabilities",
    "prompt_id",
    "prompt_profile",
    "profile",
    "provider",
    "default_provider",
    "model",
    "default_model",
    "parameters",
    "enabled",
    "fallback_tier",
    "model_fallback",
    "delegation_policy",
    "metadata_route_type",
    "metadata_future_model_boundary",
}


@dataclass
class AgentBlueprintDefinition:
    id: str
    version: str
    title: str
    description: str
    scope: str
    root: Path
    root_path: Path
    root_expert: str = ""
    enabled: bool = True
    validation_errors: list[str] = field(default_factory=list)
    validation_warnings: list[str] = field(default_factory=list)
    defaults: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["root"] = str(self.root)
        payload["root_path"] = str(self.root_path)
        payload["definition_path"] = str(self.root_path)
        return payload


def agent_blueprint_roots(home: Path, cwd: Path) -> list[tuple[Path, str]]:
    base = os.environ.get("XDG_CONFIG_HOME")
    config_root = Path(base) / "clio-agent" if base else home / ".config" / "clio-agent"
    return [
        (builtin_agent_blueprints_root(), "builtin"),
        (config_root / "agent-blueprints", "global"),
        (cwd / ".clio" / "agent-blueprints", "workspace"),
    ]


def builtin_agent_blueprints_root() -> Path:
    return Path(__file__).resolve().parents[1] / "agent_blueprints" / "builtin"


def discover_agent_blueprints(
    *,
    home: Path | None = None,
    cwd: Path | None = None,
) -> list[AgentBlueprintDefinition]:
    blueprints: list[AgentBlueprintDefinition] = []
    for root, scope in agent_blueprint_roots(home or Path.home(), cwd or Path(os.getcwd())):
        if not root.exists() or not root.is_dir():
            continue
        candidates: list[Path] = []
        if (root / _BLUEPRINT_ROOT_NAME).exists():
            candidates.append(root)
        candidates.extend(
            path for path in sorted(root.iterdir()) if path.is_dir() and (path / _BLUEPRINT_ROOT_NAME).exists()
        )
        for candidate in candidates:
            blueprints.append(parse_agent_blueprint_root(candidate, scope=scope))
    return blueprints


def parse_agent_blueprint_root(root: Path, *, scope: str) -> AgentBlueprintDefinition:
    root = root.expanduser()
    path = root / _BLUEPRINT_ROOT_NAME if root.is_dir() else root
    if not path.exists():
        return AgentBlueprintDefinition(
            id=_fallback_expert_id(root),
            version="",
            title=root.name,
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
    warnings: list[str] = []
    raw_blueprint = meta.get("blueprint")
    blueprint_meta = raw_blueprint if isinstance(raw_blueprint, dict) else {}
    declared_format = str(blueprint_meta.get("format") or meta.get("format") or "").strip()
    strict_v1 = declared_format == _BLUEPRINT_FORMAT_V1
    if raw_blueprint is not None and not isinstance(raw_blueprint, dict):
        errors.append("blueprint field must be a mapping")
    if declared_format and declared_format != _BLUEPRINT_FORMAT_V1:
        errors.append(f"unsupported blueprint format: {declared_format}")
    if not declared_format:
        warnings.append("compatibility mode: declare blueprint.format: agent-blueprint-v1 for the 1.0 contract")
    if strict_v1 and not str(meta.get("version") or "").strip():
        errors.append("missing required blueprint field: version")
    elif not str(meta.get("version") or "").strip():
        warnings.append("missing recommended blueprint field: version")
    if strict_v1 and not str(meta.get("title") or "").strip():
        errors.append("missing required blueprint field: title")
    if strict_v1 and not str(meta.get("root_expert") or meta.get("root") or meta.get("default_expert") or meta.get("default_root_expert") or "").strip():
        errors.append("missing required blueprint field: root_expert")
    if "root" in meta or "default_expert" in meta or "default_root_expert" in meta:
        warnings.append("legacy root expert alias used; prefer root_expert")
    raw_defaults = meta.get("defaults")
    if raw_defaults is not None and not isinstance(raw_defaults, dict):
        errors.append("defaults field must be a mapping")
    defaults = raw_defaults if isinstance(raw_defaults, dict) else {}
    raw_requires = meta.get("requires")
    if raw_requires is not None and not isinstance(raw_requires, dict):
        errors.append("requires field must be a mapping")
    requirements = raw_requires if isinstance(raw_requires, dict) else {}
    for field_name in sorted(set(meta) - _SUPPORTED_BLUEPRINT_FIELDS):
        if not field_name.startswith("x_"):
            warnings.append(f"unknown blueprint field ignored: {field_name}")
    install_metadata = read_install_metadata(path.parent)
    return AgentBlueprintDefinition(
        id=blueprint_id,
        version=str(meta.get("version") or "").strip(),
        title=str(meta.get("title") or blueprint_id).strip(),
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
        validation_warnings=warnings,
        defaults={str(k): v for k, v in defaults.items()},
        metadata={
            "layout": "agent_blueprint",
            "body": body.strip(),
            "format": declared_format or "compatibility",
            "strict_v1": strict_v1,
            "compatibility": meta.get("compatibility") if isinstance(meta.get("compatibility"), dict) else {},
            "requires": requirements,
            "blueprint": blueprint_meta,
            "install": install_metadata
            or (meta.get("install") if isinstance(meta.get("install"), dict) else {}),
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
    hook_descriptors = load_hook_descriptors(
        blueprint.root,
        scope=scope,
        blueprint_id=blueprint.id,
    )
    rows = _validate_agent_tool_references(
        _validate_blueprint_v1_agents(
            validate_agent_hierarchy(_load_blueprint_agents(blueprint), blueprint=blueprint),
            blueprint=blueprint,
        ),
        mcp_descriptors=mcp_descriptors,
    )
    errors = list(blueprint.validation_errors)
    warnings = list(blueprint.validation_warnings)
    for row in rows:
        errors.extend(f"{row.id}: {error}" for error in row.validation_errors)
        warnings.extend(
            f"{row.id}: {warning}"
            for warning in row.metadata.get("validation_warnings", [])
            if isinstance(warning, str)
        )
    for descriptor in mcp_descriptors:
        errors.extend(f"{descriptor.get('id', 'mcp')}: {error}" for error in descriptor.get("validation_errors", []))
        warnings.extend(
            f"{descriptor.get('id', 'mcp')}: {warning}"
            for warning in descriptor.get("validation_warnings", [])
            if isinstance(warning, str)
        )
    for descriptor in hook_descriptors:
        errors.extend(
            f"{descriptor.get('id', 'hook')}: {error}"
            for error in descriptor.get("validation_errors", [])
        )
        warnings.extend(
            f"{descriptor.get('id', 'hook')}: {warning}"
            for warning in descriptor.get("validation_warnings", [])
            if isinstance(warning, str)
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
        extra_errors.setdefault(f"{blueprint.id}.manifest", []).append(f"root_expert not found: {root_expert}")
    elif not root_expert:
        roots = [row.id for row in validated if not row.parent_id]
        if len(roots) != 1:
            target = roots[0] if roots else f"{blueprint.id}.manifest"
            extra_errors.setdefault(target, []).append("blueprint must declare root_expert or contain one root expert")
    out: list[AgentDef] = []
    for row in validated:
        errors = [*row.validation_errors, *extra_errors.get(row.id, [])]
        out.append(row.model_copy(update={"enabled": row.enabled and not errors, "validation_errors": errors}))
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


def _validate_blueprint_v1_agents(
    rows: list[AgentDef],
    *,
    blueprint: AgentBlueprintDefinition,
) -> list[AgentDef]:
    strict_v1 = bool(blueprint.metadata.get("strict_v1"))
    out: list[AgentDef] = []
    for row in rows:
        errors = list(row.validation_errors)
        warnings: list[str] = []
        path_raw = str(row.metadata.get("definition_path") or row.metadata.get("expert_path") or "")
        meta: dict[str, Any] = {}
        if path_raw:
            try:
                meta, _ = _parse_frontmatter(Path(path_raw).read_text(encoding="utf-8"))
            except OSError:
                meta = {}
        if strict_v1 and not str(meta.get("title") or "").strip():
            errors.append("missing required expert field: title")
        if strict_v1 and "tier" not in meta:
            errors.append("missing required expert field: tier")
        if strict_v1 and row.tier > 1 and not str(meta.get("parent_id") or meta.get("parent") or "").strip():
            errors.append("tier > 1 experts must declare parent_id")
        if not str(meta.get("description") or "").strip():
            warnings.append("missing recommended expert field: description")
        if "parent" in meta:
            warnings.append("legacy parent alias used; prefer parent_id")
        for field_name in sorted(set(meta) - _SUPPORTED_EXPERT_FIELDS):
            if field_name.startswith("param_") or field_name.startswith("metadata_") or field_name.startswith("x_"):
                continue
            warnings.append(f"unknown expert field ignored: {field_name}")
        if row.skills:
            warnings.append("skills are resolved at runtime from pack, workspace, and global skill roots")
        metadata = dict(row.metadata)
        if warnings:
            metadata["validation_warnings"] = warnings
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
        install.get("method")
        or install.get("type")
        or install.get("manager")
        or ""
    ).strip()
    package = str(
        install.get("package")
        or install.get("name")
        or install.get("binary")
        or ""
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
        install.get("method")
        or install.get("type")
        or install.get("manager")
        or ""
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
) -> list[AgentDef]:
    builtin_tools = set(TOOL_CATALOG) | _MEMORY_TOOL_NAMES
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
            warnings.append(
                "MCP descriptors are disabled until explicitly enabled and trusted"
            )
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
        warnings = [
            "Blueprint packaged hooks are disabled until explicitly enabled and trusted"
        ]
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
        else:
            source_kind = "git"
            clone_target = tmp_path / "repo"
            cmd = ["git", "clone", "--depth", "1"]
            if ref:
                cmd.extend(["--branch", ref])
            cmd.extend([source, str(clone_target)])
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            resolved_source = clone_target
            commit = subprocess.check_output(
                ["git", "-C", str(clone_target), "rev-parse", "HEAD"],
                text=True,
            ).strip()
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
                "installed_at": datetime.now(UTC).isoformat(),
                "checksum": _tree_checksum(dest),
                "scope": scope,
            }
            _write_install_metadata(dest, metadata)
            installed.append({**parse_agent_blueprint_root(dest, scope=scope).to_wire(), "install": metadata})
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
    base = os.environ.get("XDG_CONFIG_HOME")
    config_root = Path(base) / "clio-agent" if base else home / ".config" / "clio-agent"
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
        path for path in sorted(source.iterdir()) if path.is_dir() and (path / _BLUEPRINT_ROOT_NAME).exists()
    )
    if blueprint_id:
        candidates = [path for path in candidates if parse_agent_blueprint_root(path, scope="install").id == blueprint_id]
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
    files = sorted((expert_root if expert_root.is_dir() else blueprint.root).rglob("*.md"))
    files = [
        path
        for path in files
        if path.name != _BLUEPRINT_ROOT_NAME
        and "/prompts/" not in path.as_posix()
        and "/commands/" not in path.as_posix()
        and "/skills/" not in path.as_posix()
        and "/tools/" not in path.as_posix()
        and "/profiles/" not in path.as_posix()
    ]
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
        metadata = {
            **row.metadata,
            "agent_blueprint_id": blueprint.id,
            "agent_blueprint_version": blueprint.version,
            "agent_blueprint_title": blueprint.title,
            "agent_blueprint_scope": blueprint.scope,
            "agent_blueprint_root": str(blueprint.root),
            "agent_blueprint_root_expert": blueprint.root_expert,
            "agent_blueprint_definition_path": str(blueprint.root_path),
            "definition_kind": "agent_blueprint",
        }
        out.append(row.model_copy(update={"metadata": metadata}))
    if not out:
        out.append(
            AgentDef(
                id=f"{blueprint.id}.empty",
                source="expert_pack",
                title=f"{blueprint.title} Blueprint",
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
