"""External prompt registry for CLIO.

Prompt files are Markdown with flat YAML-like frontmatter:

---
id: clio.chat
title: Chat prompt
profile: heavy
provider: anthropic
model: claude-sonnet-4-5
---
Prompt body...

The parser is intentionally small and dependency-free. It supports the
frontmatter shapes CLIO needs for prompt/profile selection without making YAML
execution semantics part of the trusted prompt path.
"""

from __future__ import annotations

import hashlib
import inspect
import os
import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Iterable, Optional


@dataclass
class PromptSource:
    scope: str
    root: Path


@dataclass
class PromptProfile:
    name: str
    text: str
    scope: str
    source_path: str = ""
    provider: str = ""
    model: str = ""
    checksum: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PromptDefinition:
    id: str
    title: str = ""
    description: str = ""
    default_profile: str = "default"
    profiles: dict[str, PromptProfile] = field(default_factory=dict)
    scope: str = "builtin"
    source_path: str = ""
    enabled: bool = True
    validation_errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ResolvedPrompt:
    id: str
    profile: str
    text: str
    title: str = ""
    description: str = ""
    scope: str = ""
    source_path: str = ""
    provider: str = ""
    model: str = ""
    checksum: str = ""
    fallback_profile: str = ""
    validation_errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


_RENDER_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}")
_ALLOWED_RENDER_PLACEHOLDERS = {
    "agents.available_tree",
    "agents.available_flat",
    "tools.available",
    "commands.agent_invocable",
    "memory.policy_summary",
    "permissions.policy_summary",
    "provider.current",
    "session.active_pack",
    "session.active_agent_blueprint",
}


def default_prompt_sources(*, cwd: Optional[Path] = None, config_dir: Optional[Path] = None) -> list[PromptSource]:
    """Return prompt roots in increasing precedence order."""

    cwd = cwd or Path(os.getcwd())
    if config_dir is None:
        from clio_agent import paths  # noqa: PLC0415 - avoid import cycle at module load

        config_dir = paths.user_config_dir()
    return [
        PromptSource("global", config_dir / "prompts"),
        PromptSource("workspace", cwd / ".clio" / "prompts"),
    ]


def _builtin_profile_policies() -> dict[str, str]:
    """Load built-in profile policy text from packaged Markdown files."""

    rows: dict[str, str] = {}
    try:
        root = resources.files("clio_agent.prompt_packs.builtin") / "profiles"
        for name in sorted(path.name for path in root.iterdir() if path.name.endswith(".md")):
            parsed = parse_prompt_text(
                (root / name).read_text(encoding="utf-8"),
                scope="builtin",
                source_path=f"package://clio_agent.prompt_packs.builtin/profiles/{name}",
                fallback_id=name.removesuffix(".md"),
            )
            profile = next(iter(parsed.profiles.values()), None)
            if profile is not None:
                rows[parsed.id] = profile.text
    except Exception:
        return {}
    return rows


def _builtin_alignment_requirements() -> dict[str, tuple[str, ...]]:
    """Load built-in prompt-family requirements from packaged Markdown files."""

    rows: dict[str, tuple[str, ...]] = {}
    try:
        root = resources.files("clio_agent.prompt_packs.builtin") / "requirements"
        for name in sorted(path.name for path in root.iterdir() if path.name.endswith(".md")):
            parsed = parse_prompt_text(
                (root / name).read_text(encoding="utf-8"),
                scope="builtin",
                source_path=f"package://clio_agent.prompt_packs.builtin/requirements/{name}",
                fallback_id=name.removesuffix(".md"),
            )
            profile = next(iter(parsed.profiles.values()), None)
            if profile is None:
                continue
            rows[parsed.id] = tuple(
                line.strip()[2:].strip()
                for line in profile.text.splitlines()
                if line.strip().startswith("- ")
            )
    except Exception:
        return {}
    return rows


def builtin_prompt_definitions() -> dict[str, PromptDefinition]:
    """Return built-in CLIO prompt definitions loaded from package data."""

    rows: dict[str, PromptDefinition] = {}
    alignment_requirements = _builtin_alignment_requirements()

    def add(prompt_id: str, title: str, text: str, *, description: str = "") -> None:
        cleaned = inspect.cleandoc(text or "").strip()
        profiles = _aligned_builtin_profiles(prompt_id, cleaned)
        rows[prompt_id] = PromptDefinition(
            id=prompt_id,
            title=title,
            description=description,
            default_profile="default",
            profiles=profiles,
            scope="builtin",
            source_path=f"package://clio_agent.prompt_packs.builtin/{prompt_id}.md",
            metadata={
                "source": "packaged_prompt_file",
                "alignment": "public_reference_matrix",
                "references": "PROMPT_ALIGNMENT_REFERENCE_MATRIX.md",
                "requirements": list(alignment_requirements.get(prompt_id, ())),
                "profiles": list(profiles),
            },
        )

    try:
        root = resources.files("clio_agent.prompt_packs.builtin")
        prompt_files = [
            (path, path.name)
            for path in root.iterdir()
            if path.name.endswith(".md")
        ]
        runtime_root = root / "runtime"
        if runtime_root.is_dir():
            prompt_files.extend(
                (path, f"runtime/{path.name}")
                for path in runtime_root.iterdir()
                if path.name.endswith(".md")
            )
        for path, source_name in sorted(prompt_files, key=lambda item: item[1]):
            name = path.name
            text = path.read_text(encoding="utf-8")
            parsed = parse_prompt_text(
                text,
                scope="builtin",
                source_path=f"package://clio_agent.prompt_packs.builtin/{source_name}",
                fallback_id=name.removesuffix(".md"),
            )
            base_profile = next(iter(parsed.profiles.values()), None)
            add(parsed.id, parsed.title or parsed.id, base_profile.text if base_profile else "")
            rows[parsed.id].description = parsed.description
            rows[parsed.id].source_path = parsed.source_path
            rows[parsed.id].metadata.update(parsed.metadata)
    except Exception as exc:  # pragma: no cover - defensive import guard
        text = (
            "CLIO built-in prompt files could not be imported. Check packaged "
            f"prompt resources before running prompt alignment. Error: {type(exc).__name__}"
        )
        add("clio.chat", "Chat agent", text, description="fallback import-error prompt")
        rows["clio.chat"].enabled = False
        rows["clio.chat"].validation_errors.append(text)

    return rows


def _aligned_builtin_profiles(prompt_id: str, base_text: str) -> dict[str, PromptProfile]:
    """Return CLIO's aligned built-in prompt profiles for one prompt family."""

    base = base_text.strip()
    alignment_requirements = _builtin_alignment_requirements()
    profile_policies = _builtin_profile_policies()
    requirements = "\n".join(f"- {item}" for item in alignment_requirements.get(prompt_id, ()))
    family_block = f"\n\nPrompt-family requirements:\n{requirements}" if requirements else ""
    profiles: dict[str, PromptProfile] = {}
    for name, policy in profile_policies.items():
        profile_text = "\n\n".join(
            part
            for part in (
                base,
                inspect.cleandoc(policy).strip(),
                family_block.strip(),
            )
            if part
        )
        profiles[name] = PromptProfile(
            name=name,
            text=profile_text,
            scope="builtin",
            checksum=_checksum(profile_text),
            metadata={
                "alignment": "public_reference_matrix",
                "behavior_profile": name,
                "prompt_family": prompt_id,
            },
        )
    return profiles


class PromptRegistry:
    """Resolve built-in and external prompt definitions with scoped overrides."""

    def __init__(
        self,
        *,
        sources: Optional[Iterable[PromptSource]] = None,
        builtins: Optional[dict[str, PromptDefinition]] = None,
        write_root: Optional[Path] = None,
    ) -> None:
        self.sources = list(sources or default_prompt_sources())
        self.builtins = builtins
        self._builtin_cache: Optional[dict[str, PromptDefinition]] = None
        self.write_root = write_root or (self.sources[0].root if self.sources else None)

    def list(self) -> list[PromptDefinition]:
        definitions = self._load_all()
        return sorted(definitions.values(), key=lambda row: row.id)

    def get(self, prompt_id: str) -> Optional[PromptDefinition]:
        return self._load_all().get(prompt_id)

    def resolve(self, prompt_id: str, *, profile: str = "") -> Optional[ResolvedPrompt]:
        row = self.get(prompt_id)
        if row is None:
            return None
        requested = profile or row.default_profile or "default"
        profile_row = row.profiles.get(requested)
        fallback = ""
        if profile_row is None and requested != row.default_profile:
            profile_row = row.profiles.get(row.default_profile)
            fallback = row.default_profile
        if profile_row is None:
            profile_row = next(iter(row.profiles.values()), None)
            fallback = profile_row.name if profile_row is not None else ""
        if profile_row is None:
            return ResolvedPrompt(
                id=row.id,
                profile=requested,
                text="",
                title=row.title,
                description=row.description,
                scope=row.scope,
                source_path=row.source_path,
                fallback_profile=fallback,
                validation_errors=row.validation_errors or ["prompt has no profiles"],
                metadata=row.metadata,
            )
        return ResolvedPrompt(
            id=row.id,
            profile=profile_row.name,
            text=profile_row.text,
            title=row.title,
            description=row.description,
            scope=profile_row.scope or row.scope,
            source_path=profile_row.source_path or row.source_path,
            provider=profile_row.provider,
            model=profile_row.model,
            checksum=profile_row.checksum,
            fallback_profile=fallback,
            validation_errors=row.validation_errors,
            metadata={**row.metadata, **profile_row.metadata},
        )

    def render(
        self,
        prompt_id: str,
        *,
        profile: str = "",
        context: Optional[dict[str, str]] = None,
    ) -> Optional[ResolvedPrompt]:
        resolved = self.resolve(prompt_id, profile=profile)
        if resolved is None:
            return None
        rendered_text, used, errors = render_prompt_text(resolved.text, context or {})
        metadata = dict(resolved.metadata)
        metadata["render"] = {
            "placeholders_used": used,
            "context_keys": sorted((context or {}).keys()),
        }
        return ResolvedPrompt(
            **{
                **as_resolved_dict(resolved),
                "text": rendered_text,
                "validation_errors": list(dict.fromkeys(resolved.validation_errors + errors)),
                "metadata": metadata,
            }
        )

    def save(
        self,
        prompt_id: str,
        *,
        text: str,
        profile: str = "default",
        title: str = "",
        description: str = "",
        provider: str = "",
        model: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> PromptDefinition:
        if self.write_root is None:
            raise ValueError("prompt registry has no writable root")
        clean_id = _validate_prompt_id(prompt_id)
        clean_profile = _validate_profile(profile or "default")
        self.write_root.mkdir(parents=True, exist_ok=True)
        path = self.write_root / f"{clean_id}--{clean_profile}.md"
        frontmatter: dict[str, Any] = {
            "id": clean_id,
            "profile": clean_profile,
            "title": title or clean_id,
        }
        if description:
            frontmatter["description"] = description
        if provider:
            frontmatter["provider"] = provider
        if model:
            frontmatter["model"] = model
        if metadata:
            for key, value in metadata.items():
                if _is_frontmatter_scalar(value):
                    frontmatter[f"x_{key}"] = str(value)
        path.write_text(_render_prompt_file(frontmatter, text), encoding="utf-8")
        row = self.get(clean_id)
        if row is None:  # pragma: no cover - save then read should normally succeed
            raise ValueError(f"prompt was not saved: {clean_id}")
        return row

    def reload(self) -> dict[str, Any]:
        """Clear cached packaged prompts and return a fresh registry summary."""
        self._builtin_cache = None
        definitions = self._load_all()
        return {
            "prompt_count": len(definitions),
            "prompt_ids": sorted(definitions),
            "sources": [{"scope": source.scope, "root": str(source.root)} for source in self.sources],
        }

    def _load_all(self) -> dict[str, PromptDefinition]:
        definitions = {pid: _clone_definition(row) for pid, row in self._builtins().items()}
        for source in self.sources:
            for path in _prompt_files(source.root):
                parsed = parse_prompt_file(path, scope=source.scope)
                existing = definitions.get(parsed.id)
                if existing is None:
                    definitions[parsed.id] = parsed
                    continue
                definitions[parsed.id] = _merge_definition(existing, parsed)
        return definitions

    def _builtins(self) -> dict[str, PromptDefinition]:
        if self.builtins is not None:
            return self.builtins
        if self._builtin_cache is None:
            self._builtin_cache = builtin_prompt_definitions()
        return self._builtin_cache


def parse_prompt_file(path: Path, *, scope: str) -> PromptDefinition:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return PromptDefinition(
            id=_fallback_prompt_id(path),
            scope=scope,
            source_path=str(path),
            enabled=False,
            validation_errors=[f"unable to read prompt file: {exc}"],
        )
    return parse_prompt_text(text, scope=scope, source_path=str(path), fallback_id=_fallback_prompt_id(path))


def parse_prompt_text(
    text: str,
    *,
    scope: str,
    source_path: str = "",
    fallback_id: str = "",
) -> PromptDefinition:
    errors: list[str] = []
    meta, body = _parse_frontmatter(text)
    prompt_id = str(meta.get("id") or "").strip()
    if not prompt_id:
        prompt_id = fallback_id
        errors.append("missing required frontmatter field: id")
    elif not _PROMPT_ID_RE.fullmatch(prompt_id):
        errors.append("invalid prompt id; use letters, numbers, dots, underscores, and hyphens")
    profile_name = str(meta.get("profile") or meta.get("default_profile") or "default").strip()
    if not _PROFILE_RE.fullmatch(profile_name):
        errors.append("invalid profile; use letters, numbers, underscores, and hyphens")
        profile_name = "default"
    if not body.strip():
        errors.append("prompt body is empty")
    placeholder_errors = _placeholder_validation_errors(body)
    errors.extend(placeholder_errors)
    title = str(meta.get("title") or prompt_id).strip()
    description = str(meta.get("description") or "").strip()
    provider = str(meta.get("provider") or meta.get("default_provider") or "").strip()
    model = str(meta.get("model") or meta.get("default_model") or "").strip()
    profile = PromptProfile(
        name=profile_name,
        text=body,
        scope=scope,
        source_path=source_path,
        provider=provider,
        model=model,
        checksum=_checksum(body),
        metadata=_metadata_from_frontmatter(meta),
    )
    return PromptDefinition(
        id=prompt_id,
        title=title,
        description=description,
        default_profile=str(meta.get("default_profile") or profile_name).strip() or profile_name,
        profiles={profile_name: profile},
        scope=scope,
        source_path=source_path,
        enabled=not errors,
        validation_errors=errors,
        metadata=_metadata_from_frontmatter(meta),
    )


_PROMPT_ID_RE = re.compile(r"[A-Za-z0-9_.-]+")
_PROFILE_RE = re.compile(r"[A-Za-z0-9_-]+")


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text.strip()
    end = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end < 0:
        return {}, text.strip()
    meta: dict[str, Any] = {}
    cur_key: Optional[str] = None
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
        if not value:
            meta[key] = []
            cur_key = key
        else:
            meta[key] = value.strip("\"'")
            cur_key = None
    return meta, "\n".join(lines[end + 1 :]).strip()


def _prompt_files(root: Path) -> list[Path]:
    if not root.exists() or not root.is_dir():
        return []
    return sorted(
        [path for path in root.rglob("*.md") if path.is_file()],
        key=lambda path: str(path).lower(),
    )


def _merge_definition(base: PromptDefinition, override: PromptDefinition) -> PromptDefinition:
    merged = _clone_definition(base)
    if not override.enabled:
        merged.validation_errors = list(
            dict.fromkeys(merged.validation_errors + override.validation_errors)
        )
        invalid_sources = list(merged.metadata.get("invalid_sources") or [])
        if override.source_path:
            invalid_sources.append(override.source_path)
        if invalid_sources:
            merged.metadata["invalid_sources"] = invalid_sources
        return merged
    if override.title:
        merged.title = override.title
    if override.description:
        merged.description = override.description
    if override.default_profile:
        merged.default_profile = override.default_profile
    merged.scope = override.scope or merged.scope
    merged.source_path = override.source_path or merged.source_path
    merged.enabled = merged.enabled and override.enabled
    merged.validation_errors = list(dict.fromkeys(merged.validation_errors + override.validation_errors))
    merged.metadata.update(override.metadata)
    merged.profiles.update(override.profiles)
    return merged


def _clone_definition(row: PromptDefinition) -> PromptDefinition:
    return PromptDefinition(
        id=row.id,
        title=row.title,
        description=row.description,
        default_profile=row.default_profile,
        profiles={
            name: PromptProfile(
                name=profile.name,
                text=profile.text,
                scope=profile.scope,
                source_path=profile.source_path,
                provider=profile.provider,
                model=profile.model,
                checksum=profile.checksum,
                metadata=dict(profile.metadata),
            )
            for name, profile in row.profiles.items()
        },
        scope=row.scope,
        source_path=row.source_path,
        enabled=row.enabled,
        validation_errors=list(row.validation_errors),
        metadata=dict(row.metadata),
    )


def _render_prompt_file(frontmatter: dict[str, Any], text: str) -> str:
    lines = ["---"]
    for key, value in frontmatter.items():
        lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append(text.rstrip())
    lines.append("")
    return "\n".join(lines)


def _validate_prompt_id(prompt_id: str) -> str:
    clean = prompt_id.strip()
    if not _PROMPT_ID_RE.fullmatch(clean):
        raise ValueError("invalid prompt id")
    return clean


def _validate_profile(profile: str) -> str:
    clean = profile.strip()
    if not _PROFILE_RE.fullmatch(clean):
        raise ValueError("invalid prompt profile")
    return clean


def _fallback_prompt_id(path: Path) -> str:
    return path.stem.replace("--", ".")


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _metadata_from_frontmatter(meta: dict[str, Any]) -> dict[str, Any]:
    metadata = {
        key[2:]: value
        for key, value in meta.items()
        if key.startswith("x_") and _is_frontmatter_scalar(value)
    }
    requires = meta.get("requires")
    if isinstance(requires, list):
        metadata["requires"] = [str(item).strip() for item in requires if str(item).strip()]
    placeholders = meta.get("render_placeholders")
    if isinstance(placeholders, list):
        metadata["render_placeholders"] = [
            str(item).strip() for item in placeholders if str(item).strip()
        ]
    return metadata


def _is_frontmatter_scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool))


def _placeholder_validation_errors(text: str) -> list[str]:
    unknown = sorted(
        {
            name
            for name in _RENDER_PLACEHOLDER_RE.findall(text)
            if name not in _ALLOWED_RENDER_PLACEHOLDERS
        }
    )
    return [f"unknown render placeholder: {name}" for name in unknown]


def render_prompt_text(text: str, context: dict[str, str]) -> tuple[str, list[str], list[str]]:
    used: list[str] = []
    errors: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in _ALLOWED_RENDER_PLACEHOLDERS:
            errors.append(f"unknown render placeholder: {name}")
            return match.group(0)
        used.append(name)
        return str(context.get(name, f"[missing render context: {name}]"))

    rendered = _RENDER_PLACEHOLDER_RE.sub(replace, text)
    return rendered, sorted(set(used)), sorted(set(errors))


def as_resolved_dict(row: ResolvedPrompt) -> dict[str, Any]:
    return {
        "id": row.id,
        "profile": row.profile,
        "text": row.text,
        "title": row.title,
        "description": row.description,
        "scope": row.scope,
        "source_path": row.source_path,
        "provider": row.provider,
        "model": row.model,
        "checksum": row.checksum,
        "fallback_profile": row.fallback_profile,
        "validation_errors": list(row.validation_errors),
        "metadata": dict(row.metadata),
    }
