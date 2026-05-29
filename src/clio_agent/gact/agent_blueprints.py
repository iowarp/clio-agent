"""Markdown Agent Blueprint discovery and installation.

Agent Blueprints are the file-backed definition of a complete CLIO Agent. The
canonical root file is ``AGENT.md`` with Markdown frontmatter. Legacy
``clio-pack.yaml`` Expert Packs remain supported by the older loader and are
adapted at the API boundary rather than duplicated here.
"""

from __future__ import annotations

import hashlib
import os
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

_BLUEPRINT_ROOT_NAME = "AGENT.md"
_BLUEPRINT_ID_RE = r"^[A-Za-z0-9_.-]+$"


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
    elif not __import__("re").fullmatch(_BLUEPRINT_ID_RE, blueprint_id):
        errors.append("invalid blueprint id; use letters, numbers, dots, underscores, and hyphens")
    raw_defaults = meta.get("defaults")
    defaults = raw_defaults if isinstance(raw_defaults, dict) else {}
    requirements = meta.get("requires") if isinstance(meta.get("requires"), dict) else {}
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
        defaults={str(k): v for k, v in defaults.items()},
        metadata={
            "layout": "agent_blueprint",
            "body": body.strip(),
            "compatibility": meta.get("compatibility") if isinstance(meta.get("compatibility"), dict) else {},
            "requires": requirements,
            "blueprint": meta.get("blueprint") if isinstance(meta.get("blueprint"), dict) else {},
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
    rows = validate_agent_hierarchy(_load_blueprint_agents(blueprint), blueprint=blueprint)
    mcp_descriptors = load_mcp_descriptors(blueprint.root, scope=scope, blueprint_id=blueprint.id)
    errors = list(blueprint.validation_errors)
    for row in rows:
        errors.extend(f"{row.id}: {error}" for error in row.validation_errors)
    for descriptor in mcp_descriptors:
        errors.extend(f"{descriptor.get('id', 'mcp')}: {error}" for error in descriptor.get("validation_errors", []))
    return {
        "agent_blueprint": blueprint.to_wire(),
        "agents": [row.model_dump(exclude_none=True) for row in rows],
        "mcp_descriptors": mcp_descriptors,
        "enabled": blueprint.enabled and not errors,
        "validation_errors": errors,
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
        rows.append(
            {
                "id": descriptor_id,
                "name": str(meta.get("name") or descriptor_id),
                "title": str(meta.get("title") or descriptor_id),
                "description": str(meta.get("description") or body.strip()),
                "transport": transport,
                "command": str(meta.get("command") or ""),
                "args": _list_field(meta, "args"),
                "url": str(meta.get("url") or ""),
                "enabled": False,
                "status": "disabled",
                "source": "agent_blueprint",
                "scope": scope,
                "agent_blueprint_id": blueprint_id,
                "definition_path": str(path),
                "validation_errors": errors,
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
