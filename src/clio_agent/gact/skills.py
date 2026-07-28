"""Skill catalog for the GACT server (#916 / #917).

Single owner of SKILL.md **discovery and typed resolution** across the three
skill scopes, in deterministic precedence order:

1. ``pack`` — ``<pack root>/skills/`` of the Agent Blueprint / expert pack the
   declaring expert ships in;
2. ``workspace`` — ``<cwd>/.claude|.codex|.agents/skills/``;
3. ``global`` — ``~/.claude|.codex|.agents/skills/``.

A skill is a directory with a ``SKILL.md`` (Agent Skills open-standard shape:
YAML frontmatter with ``name``/``description``, markdown body = the procedure)
or a flat ``<id>.md`` directly under a skill root. Discovery reads frontmatter
only — bodies stay on disk until a consumer explicitly asks
(:func:`read_skill_body`), which is what makes progressive disclosure (#919)
possible.

Resolution of a declared skill id returns a typed :class:`SkillResolution` —
``resolved`` / ``missing`` / ``ambiguous`` / ``unreadable`` — never a silent
skip. ``ambiguous`` means two different definitions of the same id inside one
scope tier; cross-tier duplicates are not ambiguous (the higher-precedence
tier shadows). :meth:`SkillResolution.to_metadata` renders the
``skill_resolution`` agent-row diagnostic (same family as
``prompt_resolution`` in :mod:`clio_agent.gact.agents.composition`).

This module is a pure leaf: stdlib + no gact imports, so every gact layer
(catalog, expert packs, builders) can depend on it without cycles.
"""

from __future__ import annotations

import hashlib
import os
import os.path
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

SkillScope = Literal["pack", "workspace", "global", "builtin"]
SkillStatus = Literal["resolved", "missing", "ambiguous", "unreadable"]

#: Precedence order for :meth:`SkillCatalog.resolve`. ``builtin`` is LAST (lowest
#: precedence) so a user-authored ``pack``/``workspace``/``global`` skill of the same id
#: always shadows a shipped built-in — the built-ins (e.g. ``planning``) are defaults, not
#: overrides.
_SCOPE_ORDER: tuple[SkillScope, ...] = ("pack", "workspace", "global", "builtin")

#: Root holding clio's shipped built-in skills (the ``planning`` entry-skill lives here).
#: It is package-relative and independent of ``home``/``cwd``, so every catalog scan finds
#: the built-ins regardless of where the daemon was launched.
_BUILTIN_SKILLS_ROOT = Path(__file__).resolve().parent / "builtin_skills"


class SkillNotDelegatableError(RuntimeError):
    """A skill id was used where a delegatable agent id is required (#918).

    Skills stopped materializing as agents in the #916 skill-semantics change:
    a skill is procedural knowledge an expert loads, not an actor to route to.
    """

    def __init__(self, skill_id: str, path: str = "") -> None:
        super().__init__(
            f"{skill_id!r} is a skill, not a delegatable agent — declare it under "
            "`skills:` on the expert that needs it, or invoke the slash command "
            "its frontmatter declares (if any)"
        )
        self.skill_id = skill_id
        self.path = path


class SkillBodyUnreadableError(RuntimeError):
    """A resolved skill's SKILL.md could not be read back for loading."""

    def __init__(self, skill_id: str, path: str, cause: str) -> None:
        super().__init__(
            f"skill {skill_id!r} body at {path} is unreadable: {cause}"
        )
        self.skill_id = skill_id
        self.path = path
        self.cause = cause


@dataclass(frozen=True)
class SkillRef:
    """A discovered skill definition.

    ``body`` and ``checksum`` are captured in the same single read as the
    frontmatter, so consumers that need scan-time consistency (e.g. skill
    command derivation) never race a second read. Consumers that want
    load-time freshness (the #919 ``load_skill`` tool) use
    :func:`read_skill_body` instead, which re-reads from disk.
    """

    id: str
    title: str
    description: str
    path: str
    dir: str
    scope: SkillScope
    source: str
    layout: str
    meta: dict[str, Any]
    body: str = ""
    checksum: str = ""


@dataclass(frozen=True)
class SkillResolution:
    """Typed outcome of resolving one declared skill id."""

    skill_id: str
    status: SkillStatus
    skill: Optional[SkillRef] = None
    candidates: tuple[str, ...] = ()
    detail: str = ""

    def to_metadata(self) -> dict[str, Any]:
        """Render the ``skill_resolution`` diagnostic entry for an agent row."""

        payload: dict[str, Any] = {"id": self.skill_id, "status": self.status}
        if self.skill is not None:
            payload["path"] = self.skill.path
            payload["scope"] = self.skill.scope
            payload["source"] = self.skill.source
            if self.skill.checksum:
                payload["checksum"] = self.skill.checksum
        if self.candidates:
            payload["candidates"] = list(self.candidates)
        if self.detail:
            payload["detail"] = self.detail
        return payload


def read_skill_body(ref: SkillRef) -> str:
    """Return the SKILL.md body (markdown after frontmatter) for a resolved ref.

    Raises :class:`SkillBodyUnreadableError` — a typed error, never ``''`` —
    when the file vanished or is undecodable since discovery.
    """

    try:
        text = Path(ref.path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillBodyUnreadableError(ref.id, ref.path, str(exc)) from exc
    _, body = _parse_skill_frontmatter(text)
    return body


class SkillCatalog:
    """Discovery + resolution over the three skill scopes.

    One instance is one consistent scan: per-root scans are cached on the
    instance, so :meth:`resolve_declared` over N ids reads each skill root
    once. Build a fresh catalog to observe disk changes.
    """

    def __init__(self, *, home: Path | None = None, cwd: Path | None = None) -> None:
        self._home = home or Path.home()
        self._cwd = cwd or Path(os.getcwd())
        self._root_cache: dict[str, list[SkillRef]] = {}
        self.scan_errors: list[dict[str, str]] = []

    # ---- discovery -----------------------------------------------------

    def discover(self) -> list[SkillRef]:
        """All built-in + global + workspace skills, in scan order (no id dedup).

        Callers that need one-ref-per-id apply their own precedence;
        :meth:`resolve` is the canonical way to get "the" skill for an id.
        """

        refs: list[SkillRef] = []
        for root, source, scope in _skill_search_roots(self._home, self._cwd):
            refs.extend(self._scan_root(root, scope=scope, source=source))
        return refs

    def discover_pack(self, pack_root: Path) -> list[SkillRef]:
        """Skills shipped by a pack/blueprint: ``<pack_root>/skills/``."""

        return self._scan_root(
            Path(pack_root) / "skills", scope="pack", source=Path(pack_root).name
        )

    # ---- resolution ----------------------------------------------------

    def resolve(self, skill_id: str, *, pack_root: Path | None = None) -> SkillResolution:
        """Resolve one declared skill id with pack → workspace → global precedence.

        The walk STOPS at the first tier containing any candidate for the id —
        including an unreadable one. A corrupt higher-tier definition surfaces
        as ``unreadable`` rather than silently falling through to a
        lower-tier definition that may say something entirely different
        (surfacing over fallback, per the no-silent-fallback ground rule).
        """

        skill_id = (skill_id or "").strip()
        if not skill_id:
            return SkillResolution(skill_id, "missing", detail="empty skill id")
        for scope in _SCOPE_ORDER:
            candidates = self._tier_candidates(scope, skill_id, pack_root=pack_root)
            if not candidates:
                continue
            readable = [ref for ref in candidates if ref.layout != "unreadable"]
            if len(readable) == 1:
                return SkillResolution(skill_id, "resolved", skill=readable[0])
            if len(readable) > 1:
                return SkillResolution(
                    skill_id,
                    "ambiguous",
                    candidates=tuple(ref.path for ref in readable),
                    detail=f"{len(readable)} definitions of {skill_id!r} in the {scope} scope",
                )
            return SkillResolution(
                skill_id,
                "unreadable",
                candidates=tuple(ref.path for ref in candidates),
                detail="skill file exists but could not be read",
            )
        return SkillResolution(
            skill_id, "missing", detail=f"no skill named {skill_id!r} in any scope"
        )

    def resolve_declared(
        self, skill_ids: list[str], *, pack_root: Path | None = None
    ) -> dict[str, SkillResolution]:
        """Resolve every declared id; one entry per id, order preserved."""

        out: dict[str, SkillResolution] = {}
        for skill_id in skill_ids:
            key = (skill_id or "").strip()
            if key in out:
                continue
            out[key] = self.resolve(skill_id, pack_root=pack_root)
        return out

    def resolve_declared_metadata(
        self, skill_ids: list[str], *, pack_root: Path | None = None
    ) -> dict[str, dict[str, Any]]:
        """The ``skill_resolution`` agent-row diagnostic for declared skills."""

        return {
            skill_id: resolution.to_metadata()
            for skill_id, resolution in self.resolve_declared(
                skill_ids, pack_root=pack_root
            ).items()
        }

    # ---- internals -----------------------------------------------------

    def _tier_candidates(
        self, scope: SkillScope, skill_id: str, *, pack_root: Path | None
    ) -> list[SkillRef]:
        refs: list[SkillRef] = []
        if scope == "pack":
            if pack_root is None:
                return []
            refs = self.discover_pack(pack_root)
        else:
            for root, source, root_scope in _skill_search_roots(self._home, self._cwd):
                if root_scope != scope:
                    continue
                refs.extend(self._scan_root(root, scope=scope, source=source))
        matches: dict[str, SkillRef] = {}
        for ref in refs:
            if ref.id == skill_id:
                matches.setdefault(_dedup_key(ref.path), ref)
        return list(matches.values())

    def _scan_root(self, root: Path, *, scope: SkillScope, source: str) -> list[SkillRef]:
        cache_key = f"{scope}|{source}|{root}"
        cached = self._root_cache.get(cache_key)
        if cached is not None:
            return cached
        refs: list[SkillRef] = []
        if root.exists() and root.is_dir():
            for md in _skill_markdown_files(root):
                layout = "skill_md" if md.name.upper() == "SKILL.MD" else "flat_md"
                skill_dir = str(md.parent if layout == "skill_md" else root)
                # ONE read per file: frontmatter, body, and checksum all come
                # from the same bytes, so a ref can never be internally
                # inconsistent under concurrent edits.
                try:
                    raw = md.read_bytes()
                    text = raw.decode("utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    # Surfaced, never silent: an unreadable file still yields a
                    # ref (layout="unreadable") so resolution of its id reports
                    # `unreadable` instead of `missing`, and the scan error is
                    # queryable on the catalog instance.
                    self.scan_errors.append(
                        {"path": str(md), "scope": scope, "error": str(exc)}
                    )
                    refs.append(
                        SkillRef(
                            id=_default_skill_id(md),
                            title=_default_skill_id(md),
                            description="",
                            path=str(md),
                            dir=skill_dir,
                            scope=scope,
                            source=source,
                            layout="unreadable",
                            meta={},
                        )
                    )
                    continue
                meta, body = _parse_skill_frontmatter(text)
                raw_name = meta.get("name")
                if raw_name is not None and not isinstance(raw_name, str):
                    # A list/mapping `name:` is a typed scan error, not a
                    # str()-coerced garbage id.
                    self.scan_errors.append(
                        {
                            "path": str(md),
                            "scope": scope,
                            "error": f"non-string frontmatter name: {raw_name!r}",
                        }
                    )
                    continue
                sid = str(raw_name or _default_skill_id(md)).strip()
                if not sid:
                    self.scan_errors.append(
                        {"path": str(md), "scope": scope, "error": "empty skill id"}
                    )
                    continue
                description = str(meta.get("description") or "").strip()
                if not description and body:
                    for line in body.splitlines():
                        line = line.strip()
                        if line:
                            description = line[:240]
                            break
                refs.append(
                    SkillRef(
                        id=sid,
                        title=str(meta.get("title") or sid).strip(),
                        description=description,
                        path=str(md),
                        dir=skill_dir,
                        scope=scope,
                        source=source,
                        layout=layout,
                        meta=meta,
                        body=body,
                        checksum=hashlib.sha256(raw).hexdigest(),
                    )
                )
        self._root_cache[cache_key] = refs
        return refs


# ---- shared parsing helpers (moved from gact/catalog.py — single scanner) ----


def _skill_search_roots(home: Path, cwd: Path) -> list[tuple[Path, str, SkillScope]]:
    """Return (root, source, scope) skill roots in scan order — built-in FIRST (lowest
    precedence) then global, then workspace LAST so a project skill with the same id
    overrides a global (or built-in) one for consumers that fold by id in scan order.
    Scope is EXPLICIT, never inferred from path containment (cwd==home or symlinked roots
    would misclassify)."""

    return [
        (_BUILTIN_SKILLS_ROOT, "builtin", "builtin"),
        (home / ".claude" / "skills", "claude", "global"),
        (home / ".codex" / "skills", "codex", "global"),
        (home / ".agents" / "skills", "agents", "global"),
        (cwd / ".claude" / "skills", "claude", "workspace"),
        (cwd / ".codex" / "skills", "codex", "workspace"),
        (cwd / ".agents" / "skills", "agents", "workspace"),
    ]


def _skill_markdown_files(root: Path) -> list[Path]:
    """Return candidate skill markdown files under a known skill root."""

    candidates: dict[str, Path] = {}
    for pattern in ("*.md", "**/SKILL.md"):
        for path in root.glob(pattern):
            if path.is_file():
                candidates[_dedup_key(path)] = path
    return sorted(candidates.values(), key=lambda path: str(path).lower())


def _dedup_key(path: Path | str) -> str:
    """Platform-correct identity key for a path: `normcase` folds case only on
    case-insensitive filesystems (Windows) — lowercasing everywhere would
    silently merge distinct POSIX files."""

    return os.path.normcase(str(Path(path).resolve(strict=False)))


def _default_skill_id(path: Path) -> str:
    """Return a stable skill id when frontmatter does not specify one."""

    if path.name.upper() == "SKILL.MD":
        return path.parent.name
    return path.stem


def _skill_list_field(meta: dict[str, Any], *keys: str) -> list[str]:
    """Coerce comma-separated or frontmatter-list fields into strings."""

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


def _fallback_skill_keywords(skill_id: str) -> list[str]:
    """Return search keywords for minimal skill files without frontmatter tags."""

    return [
        part for part in skill_id.replace("-", " ").replace("_", " ").split() if part.strip()
    ] or [skill_id]


def _parse_skill_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return (frontmatter_dict, body) for a SKILL.md.

    Recognises the standard ``---``-delimited block at the head of the
    file. Falls back to ({}, text) when no frontmatter is present.
    Uses a tiny line-by-line parser instead of pulling PyYAML in as a
    dependency: frontmatter shapes we care about are flat key:value plus
    optional ``- item`` lists, well within hand-rolling distance.
    """

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end < 0:
        return {}, text
    meta: dict[str, Any] = {}
    cur_key: Optional[str] = None
    for raw in lines[1:end]:
        if raw.startswith("- "):
            if cur_key and isinstance(meta.get(cur_key), list):
                meta[cur_key].append(raw[2:].strip())
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
    body = "\n".join(lines[end + 1 :]).strip()
    return meta, body


