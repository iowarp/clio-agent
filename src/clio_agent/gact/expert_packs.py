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
from pathlib import Path
from typing import Any

from clio_agent.gact.types import AgentDef

_EXPERT_ID_RE = re.compile(r"[A-Za-z0-9_.-]+")


def load_expert_packs(*, home: Path | None = None, cwd: Path | None = None) -> list[AgentDef]:
    rows: dict[str, AgentDef] = {}
    for root, scope in expert_pack_roots(home or Path.home(), cwd or Path(os.getcwd())):
        if not root.exists() or not root.is_dir():
            continue
        for path in _expert_files(root):
            row = parse_expert_file(path, scope=scope)
            rows[row.id] = row
    return list(rows.values())


def expert_pack_roots(home: Path, cwd: Path) -> list[tuple[Path, str]]:
    base = os.environ.get("XDG_CONFIG_HOME")
    config_root = Path(base) / "clio-agent" if base else home / ".config" / "clio-agent"
    return [
        (config_root / "experts", "global"),
        (config_root / "expert-packs", "global"),
        (cwd / ".clio" / "experts", "workspace"),
        (cwd / ".clio" / "expert-packs", "workspace"),
    ]


def parse_expert_file(path: Path, *, scope: str) -> AgentDef:
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
    keywords = _list_field(meta, "keywords", "tags")
    metadata = {
        "expert_path": str(path),
        "expert_scope": scope,
        "expert_layout": "expert_markdown",
    }
    for key in ("fallback_tier", "model_fallback", "delegation_policy"):
        if meta.get(key):
            metadata[key] = str(meta[key]).strip()
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
        prompt_profile=str(meta.get("prompt_profile") or meta.get("profile") or "").strip(),
        default_provider=str(meta.get("provider") or meta.get("default_provider") or "").strip(),
        default_model=str(meta.get("model") or meta.get("default_model") or "").strip(),
        parameters=_parameters_from_meta(meta),
        tools=tools,
        metadata=metadata,
        enabled=enabled,
        validation_errors=errors,
        tier=tier,
        specialization=str(meta.get("specialization") or "").strip(),
        keywords=keywords or _fallback_keywords(expert_id),
    )


def validate_expert_hierarchy(rows: list[AgentDef]) -> list[AgentDef]:
    ids = {row.id for row in rows}
    out: list[AgentDef] = []
    for row in rows:
        errors = list(row.validation_errors)
        if row.parent_id and row.parent_id not in ids:
            errors.append(f"parent_id not found: {row.parent_id}")
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


def _expert_files(root: Path) -> list[Path]:
    return sorted(
        [path for path in root.rglob("*.md") if path.is_file()],
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
    for raw in lines[1:end]:
        if raw.startswith("- "):
            if cur_key and isinstance(meta.get(cur_key), list):
                meta[cur_key].append(raw[2:].strip().strip("\"'"))
            continue
        if ":" not in raw:
            continue
        key, _, value = raw.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value:
            meta[key] = value.strip("\"'")
            cur_key = ""
        else:
            meta[key] = []
            cur_key = key
    return meta, "\n".join(lines[end + 1 :]).strip()


def _coerce_tier(value: Any, errors: list[str]) -> int:
    try:
        tier = int(value or 2)
    except (TypeError, ValueError):
        errors.append("invalid tier; expected integer 1, 2, or 3")
        return 2
    if tier not in {1, 2, 3}:
        errors.append("invalid tier; expected integer 1, 2, or 3")
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
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _parameters_from_meta(meta: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for key, value in meta.items():
        if key.startswith("param_"):
            params[key.removeprefix("param_")] = value
    return params


def _fallback_expert_id(path: Path) -> str:
    return path.stem.replace(" ", "_").lower()


def _fallback_keywords(expert_id: str) -> list[str]:
    return [
        part for part in expert_id.replace("-", " ").replace("_", " ").split() if part.strip()
    ] or [expert_id]

