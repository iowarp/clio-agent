"""Markdown expert-pack discovery for GACT agent catalog.

Expert files are Markdown with a flat frontmatter block:

---
id: ndp_catalog
title: NDP Catalog Expert
parent_id: data
tier: 3
keywords:
- ndp
- earthscope
tools:
- ndp.search
prompt_id: clio.expert.ndp_catalog
prompt_profile: heavy
provider: openai
model: gpt-5.1
---
System prompt body...

The loader is intentionally non-executing and dependency-free. Invalid files are
returned as disabled AgentDef rows with validation_errors so Doctor/TUI can show
users what broke without silently dropping an expert file.
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from clio_agent.gact.types import AgentDef

_EXPERT_ID_RE = re.compile(r"[A-Za-z0-9_.-]+")
_REF_ID_RE = re.compile(r"[A-Za-z0-9_.:/-]+")
_MANIFEST_NAME = "clio-pack.yaml"


@dataclass
class ExpertPackDefinition:
    id: str
    version: str
    title: str
    description: str
    scope: str
    root: Path
    manifest_path: Path | None = None
    enabled: bool = True
    validation_errors: list[str] = field(default_factory=list)
    defaults: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["root"] = str(self.root)
        payload["manifest_path"] = str(self.manifest_path) if self.manifest_path else ""
        payload["definition_path"] = payload["manifest_path"] or payload["root"]
        return payload


def discover_expert_packs(
    *,
    home: Path | None = None,
    cwd: Path | None = None,
) -> list[ExpertPackDefinition]:
    packs: list[ExpertPackDefinition] = []
    for root, scope, layout in expert_pack_roots(home or Path.home(), cwd or Path(os.getcwd())):
        if not root.exists() or not root.is_dir():
            continue
        if layout == "loose":
            if _expert_files(root):
                packs.append(
                    ExpertPackDefinition(
                        id=f"{scope}.experts",
                        version="",
                        title=f"{scope.title()} experts",
                        description=f"Loose {scope} expert Markdown files.",
                        scope=scope,
                        root=root,
                        metadata={"layout": "loose_experts"},
                    )
                )
            continue
        packs.extend(_manifest_packs(root, scope=scope))
    return packs


def load_expert_packs(
    *,
    home: Path | None = None,
    cwd: Path | None = None,
    pack_id: str = "",
) -> list[AgentDef]:
    rows: dict[str, AgentDef] = {}
    overrides: dict[str, list[dict[str, str]]] = {}
    for pack in discover_expert_packs(home=home, cwd=cwd):
        if pack_id and pack.id != pack_id:
            continue
        for row in _load_pack_agents(pack):
            if row.id in rows:
                chain = overrides.setdefault(row.id, [])
                prior = rows[row.id]
                chain.append(
                    {
                        "scope": str(prior.metadata.get("expert_scope") or prior.metadata.get("pack_scope") or ""),
                        "pack_id": str(prior.metadata.get("pack_id") or ""),
                        "definition_path": str(
                            prior.metadata.get("definition_path")
                            or prior.metadata.get("expert_path")
                            or ""
                        ),
                    }
                )
            row_chain = [
                *overrides.get(row.id, []),
                {
                    "scope": str(row.metadata.get("expert_scope") or row.metadata.get("pack_scope") or ""),
                    "pack_id": str(row.metadata.get("pack_id") or ""),
                    "definition_path": str(
                        row.metadata.get("definition_path") or row.metadata.get("expert_path") or ""
                    ),
                },
            ]
            rows[row.id] = row.model_copy(
                update={"metadata": {**row.metadata, "override_chain": row_chain}}
            )
    return list(rows.values())


def load_expert_pack_path(path: Path, *, scope: str = "session") -> list[AgentDef]:
    """Load one explicit pack root as a session/workspace/global override."""
    root = path.expanduser()
    manifest = root / _MANIFEST_NAME if root.is_dir() else root
    if manifest.exists():
        pack = _parse_pack_manifest(manifest, scope=scope)
    else:
        pack = ExpertPackDefinition(
            id=_fallback_expert_id(root),
            version="",
            title=root.name,
            description="",
            scope=scope,
            root=root,
            manifest_path=manifest,
            enabled=False,
            validation_errors=[f"missing pack manifest: {_MANIFEST_NAME}"],
        )
    return _load_pack_agents(pack)


def validate_expert_pack_path(path: Path, *, scope: str = "session") -> dict[str, Any]:
    root = path.expanduser()
    manifest = root / _MANIFEST_NAME if root.is_dir() else root
    if not manifest.exists():
        pack = ExpertPackDefinition(
            id=_fallback_expert_id(root),
            version="",
            title=root.name,
            description="",
            scope=scope,
            root=root,
            manifest_path=manifest,
            enabled=False,
            validation_errors=[f"missing pack manifest: {_MANIFEST_NAME}"],
        )
        rows: list[AgentDef] = []
    else:
        pack = _parse_pack_manifest(manifest, scope=scope)
        rows = validate_expert_hierarchy(
            load_expert_pack_path(root, scope=scope),
            known_parent_ids={"main", "analysis", "data", "visualization"},
        )
    errors = list(pack.validation_errors)
    for row in rows:
        errors.extend(f"{row.id}: {error}" for error in row.validation_errors)
    return {
        "pack": pack.to_wire(),
        "agents": [row.model_dump(exclude_none=True) for row in rows],
        "enabled": pack.enabled and not errors,
        "validation_errors": errors,
    }


def expert_pack_roots(home: Path, cwd: Path) -> list[tuple[Path, str, str]]:
    base = os.environ.get("XDG_CONFIG_HOME")
    config_root = Path(base) / "clio-agent" if base else home / ".config" / "clio-agent"
    return [
        (config_root / "experts", "global", "loose"),
        (config_root / "expert-packs", "global", "packs"),
        (cwd / ".clio" / "experts", "workspace", "loose"),
        (cwd / ".clio" / "expert-packs", "workspace", "packs"),
    ]


def parse_expert_file(
    path: Path,
    *,
    scope: str,
    pack: ExpertPackDefinition | None = None,
) -> AgentDef:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return _invalid_agent(
            path=path,
            scope=scope,
            errors=[f"unable to read expert file: {exc}"],
        )
    meta, body = _parse_frontmatter(text)
    errors: list[str] = []
    expert_id = str(meta.get("id") or meta.get("name") or "").strip()
    if not expert_id:
        expert_id = _fallback_expert_id(path)
        errors.append("missing required frontmatter field: id")
    elif not _EXPERT_ID_RE.fullmatch(expert_id):
        errors.append("invalid expert id; use letters, numbers, dots, underscores, and hyphens")

    tier = _coerce_tier(meta.get("tier"), errors)
    parent_id = str(meta.get("parent_id") or meta.get("parent") or "").strip()
    if tier > 1 and not parent_id:
        errors.append("tier > 1 experts must declare parent_id")

    system_prompt = body.strip()
    prompt_id = str(meta.get("prompt_id") or "").strip()
    if not system_prompt and not prompt_id:
        errors.append("expert must provide a prompt body or prompt_id")

    tools = _list_field(meta, "tools", "allowed_tools", "allowed-tools")
    skills = _list_field(meta, "skills")
    commands = _list_field(meta, "commands")
    capability_refs = _capability_refs_from_meta(meta)
    keywords = _list_field(meta, "keywords", "tags")
    for field_name, values in {
        "tools": tools,
        "skills": skills,
        "commands": commands,
    }.items():
        for value in values:
            if not _REF_ID_RE.fullmatch(value):
                errors.append(f"invalid {field_name} reference: {value}")
    defaults = pack.defaults if pack is not None else {}
    metadata: dict[str, Any] = {
        "expert_path": str(path),
        "expert_scope": scope,
        "expert_layout": "expert_markdown",
        "definition_path": str(path),
    }
    if pack is not None:
        metadata.update(
            {
                "pack_id": pack.id,
                "pack_version": pack.version,
                "pack_scope": pack.scope,
                "pack_title": pack.title,
                "pack_definition_path": str(pack.manifest_path) if pack.manifest_path else str(pack.root),
                "pack_enabled": pack.enabled,
            }
        )
        if pack.validation_errors:
            metadata["pack_validation_errors"] = list(pack.validation_errors)
            errors.extend(pack.validation_errors)
    for key in ("fallback_tier", "model_fallback", "delegation_policy"):
        if meta.get(key):
            metadata[key] = str(meta[key]).strip()
    dspy_semantics = _dspy_semantics_from_meta(meta)
    if dspy_semantics:
        metadata["dspy"] = dspy_semantics
    structured_outputs = _mapping_field(meta, "structured_outputs", "structured-outputs")
    if structured_outputs:
        metadata["structured_outputs"] = structured_outputs
    fanout = _mapping_field(meta, "fanout", "fan_out", "fan-out")
    if fanout:
        metadata["fanout"] = fanout
    enabled_meta = str(meta.get("enabled") or "true").strip().lower()
    enabled = enabled_meta not in {"false", "0", "no", "off"} and not errors
    return AgentDef(
        id=expert_id,
        source="expert_pack",
        title=str(meta.get("title") or expert_id).strip(),
        description=str(meta.get("description") or "").strip(),
        parent_id=parent_id,
        system_prompt=system_prompt,
        prompt_id=prompt_id,
        prompt_profile=str(
            meta.get("prompt_profile")
            or meta.get("profile")
            or defaults.get("prompt_profile")
            or ""
        ).strip(),
        default_provider=str(
            meta.get("provider")
            or meta.get("default_provider")
            or defaults.get("provider")
            or ""
        ).strip(),
        default_model=str(
            meta.get("model") or meta.get("default_model") or defaults.get("model") or ""
        ).strip(),
        parameters=_parameters_from_meta(meta),
        tools=tools,
        skills=skills,
        commands=commands,
        capability_refs=capability_refs,
        metadata=metadata,
        enabled=enabled,
        validation_errors=errors,
        tier=tier,
        specialization=str(meta.get("specialization") or "").strip(),
        keywords=keywords or _fallback_keywords(expert_id),
    )


def validate_expert_hierarchy(
    rows: list[AgentDef],
    *,
    known_parent_ids: set[str] | None = None,
) -> list[AgentDef]:
    ids = {row.id for row in rows}
    valid_parent_ids = ids | (known_parent_ids or set())
    duplicate_ids = {
        row.id for row in rows if sum(1 for candidate in rows if candidate.id == row.id) > 1
    }
    child_parent = {row.id: row.parent_id for row in rows if row.parent_id}
    cycle_ids = _cycle_ids(child_parent)
    out: list[AgentDef] = []
    for row in rows:
        errors = list(row.validation_errors)
        if row.id in duplicate_ids:
            errors.append(f"duplicate expert id after merge: {row.id}")
        if row.parent_id and row.parent_id not in valid_parent_ids:
            errors.append(f"parent_id not found: {row.parent_id}")
        if row.id in cycle_ids:
            errors.append(f"hierarchy cycle includes: {row.id}")
        enabled = row.enabled and not errors
        out.append(row.model_copy(update={"enabled": enabled, "validation_errors": errors}))
    return out


def _invalid_agent(path: Path, *, scope: str, errors: list[str]) -> AgentDef:
    return AgentDef(
        id=_fallback_expert_id(path),
        source="expert_pack",
        title=path.stem,
        metadata={"expert_path": str(path), "expert_scope": scope},
        enabled=False,
        validation_errors=errors,
        tier=2,
    )


def _manifest_packs(root: Path, *, scope: str) -> list[ExpertPackDefinition]:
    candidates: list[Path] = []
    if (root / _MANIFEST_NAME).exists():
        candidates.append(root)
    candidates.extend(path for path in sorted(root.iterdir()) if path.is_dir() and (path / _MANIFEST_NAME).exists())
    packs: list[ExpertPackDefinition] = []
    for pack_root in candidates:
        packs.append(_parse_pack_manifest(pack_root / _MANIFEST_NAME, scope=scope))
    return packs


def _parse_pack_manifest(path: Path, *, scope: str) -> ExpertPackDefinition:
    errors: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return ExpertPackDefinition(
            id=_fallback_expert_id(path.parent),
            version="",
            title=path.parent.name,
            description="",
            scope=scope,
            root=path.parent,
            manifest_path=path,
            enabled=False,
            validation_errors=[f"unable to read pack manifest: {exc}"],
        )
    meta = _parse_yamlish(text)
    pack_id = str(meta.get("id") or "").strip()
    if not pack_id:
        pack_id = _fallback_expert_id(path.parent)
        errors.append("missing required manifest field: id")
    elif not _EXPERT_ID_RE.fullmatch(pack_id):
        errors.append("invalid pack id; use letters, numbers, dots, underscores, and hyphens")
    raw_defaults = meta.get("defaults")
    defaults = raw_defaults if isinstance(raw_defaults, dict) else {}
    return ExpertPackDefinition(
        id=pack_id,
        version=str(meta.get("version") or "").strip(),
        title=str(meta.get("title") or pack_id).strip(),
        description=str(meta.get("description") or "").strip(),
        scope=scope,
        root=path.parent,
        manifest_path=path,
        enabled=not errors,
        validation_errors=errors,
        defaults={str(k): v for k, v in defaults.items()},
        metadata={
            "layout": "manifest_pack",
            "default_root_expert": str(meta.get("default_root_expert") or "").strip(),
            "compatibility": meta.get("compatibility") if isinstance(meta.get("compatibility"), dict) else {},
        },
    )


def _load_pack_agents(pack: ExpertPackDefinition) -> list[AgentDef]:
    root = pack.root / "experts" if (pack.root / "experts").is_dir() else pack.root
    files = _expert_files(root)
    if not files and pack.manifest_path is not None:
        return [
            AgentDef(
                id=f"{pack.id}.empty",
                source="expert_pack",
                title=f"{pack.title} pack",
                description=pack.description,
                metadata={
                    "pack_id": pack.id,
                    "pack_version": pack.version,
                    "pack_scope": pack.scope,
                    "definition_path": str(pack.manifest_path),
                    "expert_scope": pack.scope,
                },
                enabled=False,
                validation_errors=["pack has no expert Markdown files"],
                tier=2,
            )
        ]
    rows = [parse_expert_file(path, scope=pack.scope, pack=pack) for path in files]
    seen: dict[str, int] = {}
    for row in rows:
        seen[row.id] = seen.get(row.id, 0) + 1
    if all(row.enabled for row in rows):
        ids = {row.id for row in rows}
        default_root = str(pack.metadata.get("default_root_expert") or "").strip()
        if default_root and default_root not in ids:
            rows.append(
                AgentDef(
                    id=f"{pack.id}.manifest",
                    source="expert_pack",
                    title=f"{pack.title} manifest",
                    description=pack.description,
                    metadata={
                        "pack_id": pack.id,
                        "pack_version": pack.version,
                        "pack_scope": pack.scope,
                        "definition_path": str(pack.manifest_path or pack.root),
                        "expert_scope": pack.scope,
                    },
                    enabled=False,
                    validation_errors=[f"default_root_expert not found: {default_root}"],
                    tier=2,
                )
            )
    return rows


def _expert_files(root: Path) -> list[Path]:
    return sorted(
        [
            path
            for path in root.rglob("*.md")
            if path.is_file() and "/prompts/" not in path.as_posix() and "/commands/" not in path.as_posix()
        ],
        key=lambda path: str(path).lower(),
    )


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = -1
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end < 0:
        return {}, text
    meta: dict[str, Any] = {}
    cur_key = ""
    cur_map = ""
    cur_map_list = ""
    for raw in lines[1:end]:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        stripped = raw.strip()
        indent = len(raw) - len(raw.lstrip(" "))
        if stripped.startswith("- "):
            value = stripped[2:].strip().strip("\"'")
            if cur_map and cur_map_list:
                container = meta.setdefault(cur_map, {})
                if isinstance(container, dict):
                    items = container.setdefault(cur_map_list, [])
                    if isinstance(items, list):
                        items.append(value)
            elif cur_key and isinstance(meta.get(cur_key), list):
                meta[cur_key].append(value)
            continue
        if ":" not in raw:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if indent and cur_key:
            if not isinstance(meta.get(cur_key), dict):
                meta[cur_key] = {}
            container = meta[cur_key]
            if isinstance(container, dict):
                if value:
                    container[key] = value.strip("\"'")
                    cur_map_list = ""
                else:
                    container[key] = []
                    cur_map = cur_key
                    cur_map_list = key
            continue
        cur_map = ""
        cur_map_list = ""
        if value:
            meta[key] = value.strip("\"'")
            cur_key = ""
        else:
            meta[key] = []
            cur_key = key
    return meta, "\n".join(lines[end + 1 :]).strip()


def _parse_yamlish(text: str) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    cur_map: str = ""
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw.startswith("  ") and cur_map:
            key, sep, value = raw.strip().partition(":")
            if sep:
                container = meta.setdefault(cur_map, {})
                if isinstance(container, dict):
                    container[key.strip()] = value.strip().strip("\"'")
            continue
        cur_map = ""
        if ":" not in raw:
            continue
        key, _, value = raw.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value:
            meta[key] = value.strip("\"'")
        else:
            meta[key] = {}
            cur_map = key
    return meta


def _coerce_tier(value: Any, errors: list[str]) -> int:
    try:
        tier = int(value or 2)
    except (TypeError, ValueError):
        errors.append("invalid tier; expected positive integer")
        return 2
    if tier < 1:
        errors.append("invalid tier; expected positive integer")
        return 2
    return tier


def _list_field(meta: dict[str, Any], *keys: str) -> list[str]:
    value: Any = None
    for key in keys:
        if key in meta:
            value = meta[key]
            break
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            value = value[1:-1]
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _mapping_field(meta: dict[str, Any], *keys: str) -> dict[str, Any]:
    for key in keys:
        value = meta.get(key)
        if isinstance(value, dict):
            return {str(k): v for k, v in value.items()}
    return {}


def _dspy_semantics_from_meta(meta: dict[str, Any]) -> dict[str, Any]:
    raw_dspy = _mapping_field(meta, "dspy")
    module = _mapping_field(meta, "module")
    signature = _mapping_field(meta, "signature")
    if isinstance(meta.get("module"), str):
        module = {"kind": str(meta["module"]).strip()}
    if isinstance(meta.get("signature"), str):
        signature = {"id": str(meta["signature"]).strip()}
    if "module" in raw_dspy and not module:
        raw_module = raw_dspy.get("module")
        if isinstance(raw_module, dict):
            module = {str(k): v for k, v in raw_module.items()}
        elif raw_module:
            module = {"kind": str(raw_module).strip()}
    if "signature" in raw_dspy and not signature:
        raw_signature = raw_dspy.get("signature")
        if isinstance(raw_signature, dict):
            signature = {str(k): v for k, v in raw_signature.items()}
        elif raw_signature:
            signature = {"id": str(raw_signature).strip()}
    if "kind" in raw_dspy and "kind" not in module:
        module["kind"] = str(raw_dspy["kind"]).strip()
    if "inputs" in signature:
        signature["inputs"] = _coerce_string_list(signature["inputs"])
    if "outputs" in signature:
        signature["outputs"] = _coerce_string_list(signature["outputs"])
    out: dict[str, Any] = {}
    if module:
        out["module"] = module
    if signature:
        out["signature"] = signature
    return out


def _coerce_string_list(value: Any) -> Any:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.strip("[]").split(",") if item.strip()]
    return value


def _capability_refs_from_meta(meta: dict[str, Any]) -> list[Any]:
    from clio_agent.gact.types import AgentCapabilityRef

    refs: list[AgentCapabilityRef] = []
    for raw in _list_field(meta, "capability_refs", "capabilities"):
        kind = "tool"
        ref_id = raw
        if ":" in raw:
            kind, _, ref_id = raw.partition(":")
        kind = kind.strip()
        ref_id = ref_id.strip()
        if kind in {"tool", "skill", "command"} and ref_id:
            ref_kind: Literal["tool", "skill", "command"]
            if kind == "skill":
                ref_kind = "skill"
            elif kind == "command":
                ref_kind = "command"
            else:
                ref_kind = "tool"
            refs.append(
                AgentCapabilityRef(
                    kind=ref_kind,
                    id=ref_id,
                    title=ref_id,
                    source="expert_pack",
                )
            )
    return refs


def _parameters_from_meta(meta: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for key, value in meta.items():
        if key.startswith("param_"):
            params[key.removeprefix("param_")] = value
    nested = meta.get("parameters")
    if isinstance(nested, dict):
        params.update(nested)
    return params


def _cycle_ids(parent_by_child: dict[str, str]) -> set[str]:
    cycle: set[str] = set()
    for start in parent_by_child:
        seen: list[str] = []
        current = start
        while current:
            if current in seen:
                cycle.update(seen[seen.index(current) :])
                break
            seen.append(current)
            current = parent_by_child.get(current, "")
    return cycle


def _fallback_expert_id(path: Path) -> str:
    return path.stem.replace(" ", "_").lower()


def _fallback_keywords(expert_id: str) -> list[str]:
    return [
        part for part in expert_id.replace("-", " ").replace("_", " ").split() if part.strip()
    ] or [expert_id]
