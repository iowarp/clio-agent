"""Skill runtime for dynamic experts — progressive disclosure (#916 S3 / #919).

Owner of the two tiers an expert actually experiences:

* **Tier 1 — the metadata block** (:func:`skills_prompt_block`): an expert that
  declares ``skills:`` gets a compiled block of skill *names + descriptions*
  (~100 tokens per skill) in its system prompt — never bodies (RULE 6:
  context is compiled, not concatenated). ReAct experts load bodies on
  demand; the model decides, via a tool call (⚑ #1).
* **Tier 2 — the ``load_skill`` tool** (:func:`build_load_skill_tool`):
  auto-attached runtime infrastructure (like the generated child-delegation
  tools — NOT part of the 5-7 curated domain-tool budget). Returns the
  SKILL.md body (read fresh from disk at load time) plus a listing of bundled
  files; ``load_skill(skill_id, file=...)`` reads a bundled file, path-locked
  to the skill directory. The returned text is a normal tool observation, so
  it flows into the ARC live plane / working-set fold like any other
  observation — no special context plumbing (RULE 4).

**Predict / chain-of-thought experts** have no tool loop, so metadata-only
disclosure would be a dead end for them: they get the resolved skill *bodies*
compiled into their prompt (:func:`skill_bodies_context`) — the declaration is
explicit and per-expert, so the cost is opted into (declaration-only, §3.6).

**Default-expert auto-declaration** (§3.6): the ROOT expert of the default
registry blueprint auto-declares workspace-scope skills
(:func:`effective_declared_skills`) so user-authored skills work in plain
chat without editing the blueprint.

Resolution here is workspace-correct: the catalog is built with the session's
workspace cwd (via ``resolution._runtime_workspace_catalog_cwd``) and the
declaring expert's own pack root, mirroring how the agent rows were loaded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent.gact.skills import (
    SkillBodyUnreadableError,
    SkillCatalog,
    SkillResolution,
    read_skill_body,
)
from clio_agent.runtime import trace

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.types import AgentDef


@dataclass
class SkillRuntime:
    """One expert's resolved skill surface for a single build."""

    resolutions: dict[str, SkillResolution] = field(default_factory=dict)
    prompt_block: str = ""
    bodies_block: str = ""

    @property
    def resolved(self) -> dict[str, SkillResolution]:
        return {
            skill_id: res
            for skill_id, res in self.resolutions.items()
            if res.status == "resolved" and res.skill is not None
        }


def agent_pack_root(agent_def: "AgentDef") -> Path | None:
    """The declaring expert's pack/blueprint root (for pack-local skills)."""

    meta = agent_def.metadata if isinstance(agent_def.metadata, dict) else {}
    for key in ("agent_blueprint_definition_path", "pack_definition_path"):
        raw = str(meta.get(key) or "").strip()
        if raw:
            path = Path(raw)
            # definition paths point at the manifest .md file; a bare directory
            # (even one with a dot in its name) stays as-is.
            return path.parent if path.suffix.lower() == ".md" or path.is_file() else path
    return None


def effective_declared_skills(agent_def: "AgentDef", catalog: SkillCatalog) -> list[str]:
    """The expert's declared skill ids — plus, for the default-registry ROOT
    expert only, every workspace-scope skill (auto-declaration, §3.6), so
    user-authored skills work in plain chat."""

    declared = [str(s).strip() for s in agent_def.skills if str(s).strip()]
    meta = agent_def.metadata if isinstance(agent_def.metadata, dict) else {}
    # BOTH row seams must match (the listing seam stamps source_blueprint; the
    # EXECUTING seam — load_agent_blueprints via _runtime_active_agent_blueprint_rows
    # — stamps agent_blueprint_id/agent_blueprint_root_expert instead).
    from clio_agent.gact.agent_blueprints import DEFAULT_AGENT_BLUEPRINT_ID  # noqa: PLC0415

    is_root = not (agent_def.parent_id or "") or (
        str(meta.get("agent_blueprint_root_expert") or "") == agent_def.id
    )
    is_default = (
        str(meta.get("source_blueprint") or "") == "default_registry"
        or str(meta.get("agent_blueprint_id") or "") == DEFAULT_AGENT_BLUEPRINT_ID
    )
    is_default_root = is_default and is_root
    if is_default_root:
        for ref in catalog.discover():
            if ref.scope == "workspace" and ref.layout != "unreadable" and ref.id not in declared:
                declared.append(ref.id)
    return declared


def skill_runtime_for_agent(
    app: "FastAPI | None", agent_def: "AgentDef", *, session_id: str = ""
) -> SkillRuntime:
    """Resolve the expert's skill surface against the session workspace."""

    from clio_agent.gact.runtime.app_state import per_app_dict  # noqa: PLC0415

    aid = str(getattr(agent_def, "id", "") or "")
    has_state = app is not None and getattr(app, "state", None) is not None
    if not has_state:
        # App-less rebuild (the sync fallback build): reuse the surface computed
        # on a context-bearing build of THIS app — same pattern as the
        # orchestrator briefing — so the react prompt prefix stays byte-stable
        # across build paths. per_app_dict resolves the live turn's app.
        cached = per_app_dict("skill_runtime_cache", app=app).get(aid)
        if cached is not None:
            return cached
    cwd: Path | None = None
    if app is not None and has_state:
        from clio_agent.gact.agents import resolution as _resolution  # noqa: PLC0415

        cwd = _resolution._runtime_workspace_catalog_cwd(app, session_id=session_id)
    elif getattr(agent_def, "skills", None):
        # No app, no cache: resolution falls back to the process cwd — typed,
        # never silent (workspace-tier skills may differ on this basis).
        trace.event(
            "SKILLS", "app-less skill resolution for %s uses process cwd", aid or "?"
        )
    catalog = SkillCatalog(cwd=cwd)
    declared = effective_declared_skills(agent_def, catalog)
    if not declared:
        return SkillRuntime()
    resolutions = catalog.resolve_declared(declared, pack_root=agent_pack_root(agent_def))
    runtime = SkillRuntime(resolutions=resolutions)
    runtime.prompt_block = skills_prompt_block(runtime)
    runtime.bodies_block = skill_bodies_context(runtime)
    if aid:
        per_app_dict("skill_runtime_cache", app=app)[aid] = runtime
    for skill_id, res in resolutions.items():
        if res.status != "resolved":
            # Structured reason (no-silent-fallback): the block silently omits
            # nothing — every unresolved declaration is queryable.
            trace.event(
                "SKILLS",
                "declared skill %r unresolved for agent %s: %s (%s)",
                skill_id,
                getattr(agent_def, "id", "?"),
                res.status,
                res.detail,
            )
    return runtime


def skills_prompt_block(runtime: SkillRuntime) -> str:
    """Tier-1 metadata block: names + descriptions, never bodies (RULE 6)."""

    resolved = runtime.resolved
    if not resolved:
        return ""
    lines = [
        "## Skills available to you",
        "These are procedures/rubrics you are expected to FOLLOW for the tasks "
        "they cover. BEFORE doing such a task, call the load_skill tool with the "
        "skill's id and apply the loaded procedure.",
    ]
    lines.extend(
        f"- {skill_id}: {res.skill.description}" if res.skill.description else f"- {skill_id}"
        for skill_id, res in resolved.items()
        if res.skill is not None
    )
    return "\n".join(lines)


def skill_bodies_context(runtime: SkillRuntime) -> str:
    """Full bodies for tool-less (predict/CoT) experts — their only tier."""

    resolved = runtime.resolved
    if not resolved:
        return ""
    parts: list[str] = ["## Skills declared by this expert (follow these procedures)"]
    for skill_id, res in resolved.items():
        if res.skill is None:
            continue
        try:
            body = read_skill_body(res.skill)
        except SkillBodyUnreadableError as exc:
            trace.event("SKILLS", "skill body unreadable at prompt build: %s", exc)
            continue
        parts.append(f"### Skill: {skill_id}\n{body}")
    return "\n\n".join(parts) if len(parts) > 1 else ""


def build_load_skill_tool(agent_def: "AgentDef", runtime: SkillRuntime) -> Any:
    """The tier-2 ``load_skill`` DSPy tool (auto-attached infrastructure)."""

    import dspy  # noqa: PLC0415

    resolved = runtime.resolved
    agent_id = getattr(agent_def, "id", "?")

    def load_skill(skill_id: str, file: str = "") -> str:
        skill_id = (skill_id or "").strip()
        res = resolved.get(skill_id)
        if res is None or res.skill is None:
            raise ValueError(
                f"unknown skill {skill_id!r} — this expert declares: "
                + (", ".join(sorted(resolved)) or "(none)")
            )
        ref = res.skill
        skill_dir = Path(ref.dir)
        if ref.layout != "skill_md":
            if file:
                raise ValueError(
                    f"skill {skill_id!r} is a flat .md skill with no bundled directory"
                )
        elif file:
            target = (skill_dir / file).resolve(strict=False)
            try:
                target.relative_to(skill_dir.resolve(strict=False))
            except ValueError:
                raise ValueError(
                    f"file {file!r} is outside the {skill_id!r} skill directory"
                ) from None
            try:
                content = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise ValueError(f"bundled file {file!r} unreadable: {exc}") from exc
            trace.event("SKILLS", "agent %s loaded %s file %s", agent_id, skill_id, file)
            return content
        body = read_skill_body(ref)  # fresh read: edits since scan are honored
        bundled: list[str] = []
        if ref.layout == "skill_md":
            skill_md = Path(ref.path).resolve(strict=False)
            for p in sorted(skill_dir.rglob("*")):
                rel = str(p.relative_to(skill_dir)).replace("\\", "/")
                if not p.is_file() or p.resolve(strict=False) == skill_md:
                    continue
                if any(part.startswith(".") for part in rel.split("/")):
                    continue  # dotfiles/.git etc. are not part of the skill surface
                bundled.append(rel)
                if len(bundled) >= 50:
                    bundled.append("... (listing capped at 50 files)")
                    break
        trace.event("SKILLS", "agent %s loaded skill %s (%s)", agent_id, skill_id, ref.path)
        listing = (
            "\n\nBundled files (load with load_skill(skill_id, file=...)):\n"
            + "\n".join(f"- {name}" for name in bundled)
            if bundled
            else ""
        )
        return f"# Skill: {skill_id}\n{body}{listing}"

    return dspy.Tool(
        func=load_skill,
        name="load_skill",
        desc=(
            "Load the full procedure of one of this expert's declared skills "
            "(see 'Skills available to you'). Call BEFORE performing the task "
            "the skill covers; pass file=<bundled path> to read a bundled file."
        ),
        args={
            "skill_id": {"type": "string", "description": "Declared skill id to load."},
            "file": {
                "type": "string",
                "description": "Optional bundled file path inside the skill directory.",
            },
        },
    )
