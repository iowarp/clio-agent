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

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent.gact import context as _ctx
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

#: Declaring any of these tools (or being a root agent, per ``auto_tools.py``'s
#: root compatibility) auto-declares this session's producible A2UI catalog
#: skills (S4) -- an expert should never need to also spell out
#: ``skills: [a2ui-catalog-...]`` just to use the tool it already declared.
_A2UI_PRODUCER_TOOL_NAMES = frozenset(
    {
        "create_a2ui_surface",
        "update_a2ui_components",
        "update_a2ui_data_model",
        "delete_a2ui_surface",
    }
)


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


def skill_runtime_spawns_subagents(runtime: SkillRuntime) -> bool:
    """Return whether any resolved skill can create a temporary child task.

    Effect parsing remains authoritative and typed. A malformed effect therefore
    fails module construction instead of quietly producing a launch-only runtime
    that cannot collect the child it creates.
    """

    from clio_agent.gact.agents.skill_effects import (  # noqa: PLC0415
        EFFECT_SPAWN_SUBAGENT,
        parse_skill_effect,
    )

    for resolution in runtime.resolved.values():
        assert resolution.skill is not None
        effect = parse_skill_effect(resolution.skill.meta)
        if effect is not None and effect.kind == EFFECT_SPAWN_SUBAGENT:
            return True
    return False


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


def _is_root_agent(agent_def: "AgentDef") -> bool:
    """Root-compatibility check mirroring ``auto_tools.py``'s own gate."""

    return not (agent_def.parent_id or "")


def _declares_a2ui_producer_tool(agent_def: "AgentDef") -> bool:
    declared_tools = {str(name).strip() for name in (getattr(agent_def, "tools", None) or [])}
    return bool(declared_tools & _A2UI_PRODUCER_TOOL_NAMES)


def _producible_a2ui_catalog_skill_ids(catalog: SkillCatalog) -> list[str]:
    """This session's producible A2UI catalogs, as their generated skill ids.

    Reads ``catalog``'s OWN ``catalog`` scope (:meth:`SkillCatalog.
    _catalog_refs`) rather than re-deriving producibility here -- that is
    already the session-correct set (the agent's declared allowlist, from
    ``activation.resolve_session_catalogs``), computed once and cached on the
    SAME catalog instance :func:`effective_declared_skills` goes on to
    ``resolve_declared`` against. Kept in the agent's declared preference
    order (v15 S8), so the index lists the preferred catalog first; an agent
    that declares no catalogs gets no catalog index lines at all.
    """

    return [ref.id for ref in catalog._catalog_refs()]


def effective_declared_skills(
    agent_def: "AgentDef",
    catalog: SkillCatalog,
    *,
    app: "FastAPI | None" = None,
    session_id: str = "",
) -> list[str]:
    """The expert's declared skill ids — plus, for the default-registry ROOT
    expert only, every workspace-scope skill (auto-declaration, §3.6), so
    user-authored skills work in plain chat -- plus, for any expert that
    declares a producer tool or is itself a root agent (S4), this session's
    producible A2UI catalog skill ids (``app``/``session_id`` unavailable —
    e.g. an app-less rebuild — silently contributes none, never an error)."""

    declared = [str(s).strip() for s in agent_def.skills if str(s).strip()]
    meta = agent_def.metadata if isinstance(agent_def.metadata, dict) else {}
    # The EXECUTING seam (load_agent_blueprints via
    # _runtime_active_agent_blueprint_rows, reached only for a session's
    # EXPLICITLY activated blueprint) stamps agent_blueprint_id/
    # agent_blueprint_root_expert. The former parallel "listing seam" stamp
    # (metadata["source_blueprint"] == "default_registry") is deleted along with
    # its only producer, catalog._builtin_agents()'s implicit default-registry
    # load (no session ever activated it, so it never earned auto-declaration).
    from clio_agent.gact.agent_blueprints import DEFAULT_AGENT_BLUEPRINT_ID  # noqa: PLC0415

    is_root = not (agent_def.parent_id or "") or (
        str(meta.get("agent_blueprint_root_expert") or "") == agent_def.id
    )
    is_default = str(meta.get("agent_blueprint_id") or "") == DEFAULT_AGENT_BLUEPRINT_ID
    is_default_root = is_default and is_root
    if is_default_root:
        # Auto-declare the user's workspace skills first (so they lead the surface), then
        # clio's shipped built-in skills (the ``planning`` entry-skill) — both onto the
        # default-registry ROOT expert so plain chat can invoke them without editing the
        # blueprint. Built-ins are appended AFTER workspace so a user skill of the same id
        # (which shadows the built-in in resolution) also leads it in the declared list.
        for wanted_scope in ("workspace", "builtin"):
            for ref in catalog.discover():
                if (
                    ref.scope == wanted_scope
                    and ref.layout != "unreadable"
                    and ref.id not in declared
                ):
                    declared.append(ref.id)
    wants_a2ui_catalogs = _declares_a2ui_producer_tool(agent_def) or _is_root_agent(agent_def)
    if app is not None and session_id and wants_a2ui_catalogs:
        for skill_id in _producible_a2ui_catalog_skill_ids(catalog):
            if skill_id not in declared:
                declared.append(skill_id)
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
        trace.event("SKILLS", "app-less skill resolution for %s uses process cwd", aid or "?")
    catalog_app = app if has_state else None
    catalog_session_id = session_id if has_state else ""
    catalog = SkillCatalog(cwd=cwd, app=catalog_app, session_id=catalog_session_id)
    declared = effective_declared_skills(
        agent_def, catalog, app=catalog_app, session_id=catalog_session_id
    )
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
        "These are procedures/rubrics you are expected to FOLLOW for the tasks they cover. "
        "Call load_skill for ordinary skills before applying them. Skills marked child-task "
        "must be run with spawn_skill_task so delegation remains explicit and observable.",
    ]
    from clio_agent.gact.agents.skill_effects import (  # noqa: PLC0415
        EFFECT_SPAWN_SUBAGENT,
        parse_skill_effect,
    )

    for skill_id, res in resolved.items():
        if res.skill is None:
            continue
        effect = parse_skill_effect(res.skill.meta)
        marker = (
            " [child-task: use spawn_skill_task]"
            if effect is not None and effect.kind == EFFECT_SPAWN_SUBAGENT
            else ""
        )
        description = f": {res.skill.description}" if res.skill.description else ""
        lines.append(f"- {skill_id}{marker}{description}")
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


def _declare_load_skill_structured_content(
    *,
    skill_id: str,
    scope: str,
    path: str,
    text: str,
    file: str = "",
    bundled_files: list[str] | None = None,
) -> None:
    """Declare ``load_skill``'s typed wire payload (P5 wire semantics — the
    ``wait_agent_tasks`` treatment): a ``message`` naming what loaded + its
    line count FIRST, then the id/scope/path facts. The model-facing return also
    names the resolved skill directory so executable helpers are addressable
    without guessing an installation path; this payload curates the UI wire."""

    from clio_agent.gact.agents.tool_instrumentation import (  # noqa: PLC0415
        declare_structured_content,
    )

    lines = len(text.splitlines())
    label = f"file {file!r} from skill {skill_id!r}" if file else f"skill {skill_id!r}"
    payload: dict[str, Any] = {
        "message": f"loaded {label} ({lines} line{'' if lines == 1 else 's'})",
        "skill_id": skill_id,
        "scope": scope,
        "path": path,
        "lines": lines,
        "bytes": len(text.encode("utf-8")),
    }
    if file:
        payload["file"] = file
    if bundled_files:
        payload["bundled_files"] = bundled_files
    declare_structured_content(payload)


def _collect_ref_targets(node: Any) -> set[str]:
    """Return every ``$ref`` string reachable under ``node`` (any nesting)."""

    found: set[str] = set()
    if isinstance(node, Mapping):
        ref = node.get("$ref")
        if isinstance(ref, str):
            found.add(ref)
        for value in node.values():
            found.update(_collect_ref_targets(value))
    elif isinstance(node, list):
        for item in node:
            found.update(_collect_ref_targets(item))
    return found


def _resolve_json_pointer_fragment(file_path: str, raw_text: str, fragment: str) -> str:
    """Resolve an RFC 6901 JSON Pointer ``fragment`` against a bundled JSON file.

    Returns the resolved node pretty-printed (2-space indent) plus one
    trailing line naming the ``$ref`` targets it uses (e.g.
    ``common_types.json#/$defs/Action``), so the model knows those are
    standard shapes it does not need to load separately.

    Raises:
        ValueError: ``file_path`` does not end in ``.json`` (fragments are
            JSON-only, never silently ignored); ``fragment`` is not an
            absolute pointer (does not start with ``/``); or the pointer does
            not resolve — the message names the available keys at the
            nearest resolvable parent.
    """

    if not file_path.lower().endswith(".json"):
        raise ValueError(
            f"file {file_path!r} does not support a '#' fragment: JSON pointer "
            "fragments are only supported for .json bundled files"
        )
    try:
        document = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"bundled file {file_path!r} is not valid JSON: {exc}") from exc
    if not fragment.startswith("/"):
        raise ValueError(f"fragment {fragment!r} must be an absolute JSON pointer (start with '/')")
    node: Any = document
    walked: list[str] = []
    for raw_part in fragment.split("/")[1:]:
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, Mapping) and part in node:
            node = node[part]
            walked.append(part)
            continue
        if isinstance(node, list):
            index = int(part) if part.isdigit() else -1
            if 0 <= index < len(node):
                node = node[index]
                walked.append(part)
                continue
        if isinstance(node, Mapping):
            available: list[str] = sorted(node.keys())
        elif isinstance(node, list):
            available = [str(i) for i in range(len(node))]
        else:
            available = []
        pointer_so_far = "/" + "/".join(walked)
        raise ValueError(
            f"JSON pointer {fragment!r} does not resolve in {file_path!r}: no "
            f"{part!r} at {pointer_so_far!r}; available keys: {available}"
        )
    rendered = json.dumps(node, indent=2, sort_keys=False)
    refs = sorted(_collect_ref_targets(node))
    # Local refs (bare "#/...", relative to THIS document) are themselves
    # loadable with another load_skill(..., file="catalog.json#/...") call;
    # a ref into an external file (typically common_types.json) names a
    # STANDARD shape this catalog does not define and this call cannot load.
    local_refs = sorted(f"catalog.json{ref}" for ref in refs if ref.startswith("#/"))
    standard_refs = sorted(ref for ref in refs if not ref.startswith("#/"))
    if local_refs:
        rendered += "\n\nLocal refs (load via file=): " + ", ".join(local_refs)
    if standard_refs:
        rendered += "\n\nStandard refs (not loadable here): " + ", ".join(standard_refs)
    return rendered


def _resolve_bundled_file(
    skill_id: str, primary_dir: Path, extra_dirs: tuple[str, ...], file_path: str
) -> Path:
    """Resolve ``file_path`` against ``primary_dir``, then each of ``extra_dirs``.

    Each candidate root is path-locked independently (a traversal outside
    ANY given root is never tolerated just because it lands inside a
    different one). The first root where the resolved path both stays
    within bounds AND the file actually exists wins; a path that is within
    bounds in at least one root but exists in none of them is reported as
    unreadable, never silently swallowed into "outside."
    """

    roots = [primary_dir, *(Path(extra) for extra in extra_dirs)]
    within_any_root = False
    for root in roots:
        candidate = (root / file_path).resolve(strict=False)
        try:
            candidate.relative_to(root.resolve(strict=False))
        except ValueError:
            continue
        within_any_root = True
        if candidate.is_file():
            return candidate
    if not within_any_root:
        raise ValueError(f"file {file_path!r} is outside the {skill_id!r} skill directory")
    raise ValueError(f"bundled file {file_path!r} unreadable: not found")


def build_load_skill_tool(agent_def: "AgentDef", runtime: SkillRuntime) -> Any:
    """The tier-2 ``load_skill`` DSPy tool (auto-attached infrastructure)."""

    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415

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

        def _emit_loaded(size: int, bundled_file: str = "") -> None:
            # skill.loaded (#920): typed provenance for every load, on the
            # highway (durable trace + ARC + the served UI wire). Correlated to
            # the turn like every in-loop emitter, and GUARDED: capture must
            # never fail a load that already succeeded (the react-step pattern).
            from clio_agent.gact.runtime.globals import (  # noqa: PLC0415
                _active_semantic_trace_id,
                _active_semantic_turn_id,
                _emit_semantic_event,
            )

            app = _ctx.active_app()
            sid = _ctx.active_session_id()
            if app is None or not sid:
                trace.event("SKILLS", "skill.loaded outside app/session: %s %s", skill_id, ref.path)
                return
            payload: dict[str, Any] = {
                "skill_id": skill_id,
                "scope": ref.scope,
                "path": ref.path,
                "checksum": ref.checksum,
                "size": size,
                "agent_id": agent_id,
            }
            if bundled_file:
                payload["file"] = bundled_file
            try:
                _emit_semantic_event(
                    app,
                    sid,
                    "skill.loaded",
                    turn_id=_active_semantic_turn_id(),
                    trace_id=_active_semantic_trace_id(),
                    status="completed",
                    summary=(
                        f"{agent_id} loaded skill {skill_id}"
                        + (f" file {bundled_file}" if bundled_file else "")
                    ),
                    actor={"agent_id": agent_id, "role": "expert"},
                    subject={"skill_id": skill_id, "scope": ref.scope},
                    payload=payload,
                )
            except Exception as exc:  # noqa: BLE001 - capture never breaks the load
                trace.event("SKILLS", "skill.loaded emit failed for %s: %s", skill_id, exc)

        if ref.layout != "skill_md":
            if file:
                raise ValueError(
                    f"skill {skill_id!r} is a flat .md skill with no bundled directory"
                )
        elif file:
            file_path, has_fragment, fragment = file.partition("#")
            target = _resolve_bundled_file(skill_id, skill_dir, ref.extra_dirs, file_path)
            try:
                content = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise ValueError(f"bundled file {file_path!r} unreadable: {exc}") from exc
            if has_fragment:
                content = _resolve_json_pointer_fragment(file_path, content, fragment)
            trace.event("SKILLS", "agent %s loaded %s file %s", agent_id, skill_id, file)
            _emit_loaded(len(content.encode("utf-8")), bundled_file=file)
            _declare_load_skill_structured_content(
                skill_id=skill_id, scope=ref.scope, path=ref.path, text=content, file=file
            )
            return content
        # P1.0 (#1062): a skill may declare a PRIVILEGED runtime EFFECT in its
        # frontmatter (enter_mode / spawn_subagent_with_skill). Invoking the skill
        # (no bundled ``file=``) PERFORMS the effect via the runtime — never parsed
        # from the body/model output (injection-safe). enter_mode returns a
        # confirmation + the body; spawn returns the task handle (body NOT inlined).
        from clio_agent.gact.agents.skill_effects import (  # noqa: PLC0415
            maybe_apply_skill_effect,
        )

        effect_output = maybe_apply_skill_effect(ref, agent_id=agent_id)
        if effect_output is not None:
            return effect_output
        body = read_skill_body(ref)  # fresh read: edits since scan are honored
        bundled: list[str] = []
        if ref.layout == "skill_md":
            skill_md = Path(ref.path).resolve(strict=False)
            roots = [skill_dir, *(Path(extra) for extra in ref.extra_dirs)]
            capped = False
            for root in roots:
                if capped or not root.is_dir():
                    continue
                for p in sorted(root.rglob("*")):
                    rel = str(p.relative_to(root)).replace("\\", "/")
                    if not p.is_file() or p.resolve(strict=False) == skill_md:
                        continue
                    parts = rel.split("/")
                    if any(part.startswith(".") or part == "__pycache__" for part in parts):
                        continue  # private envs, VCS files, and bytecode caches are not skill assets
                    if p.suffix in {".pyc", ".pyo"}:
                        continue
                    if rel in bundled:
                        continue  # already listed from a higher-precedence root
                    bundled.append(rel)
                    if len(bundled) >= 50:
                        bundled.append("... (listing capped at 50 files)")
                        capped = True
                        break
        trace.event("SKILLS", "agent %s loaded skill %s (%s)", agent_id, skill_id, ref.path)
        _emit_loaded(len(body.encode("utf-8")))
        listing = (
            "\n\nBundled files (load with load_skill(skill_id, file=...)):\n"
            + "\n".join(f"- {name}" for name in bundled)
            if bundled
            else ""
        )
        _declare_load_skill_structured_content(
            skill_id=skill_id, scope=ref.scope, path=ref.path, text=body, bundled_files=bundled
        )
        return f"# Skill: {skill_id}\nSkill directory (use as SKILL_ROOT): {skill_dir}\n{body}{listing}"

    return native_tool(
        load_skill,
        name="load_skill",
        presentation="text",
        domain="skills",
        title="Load skill",
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


def build_spawn_skill_task_tool(agent_def: "AgentDef", runtime: SkillRuntime) -> Any:
    """Build the explicit child-task launcher for spawn-effect skills."""

    from clio_agent.gact.agents.skill_effects import (  # noqa: PLC0415
        EFFECT_SPAWN_SUBAGENT,
        apply_spawn_skill_effect,
        parse_skill_effect,
    )
    from clio_agent.gact.agents.spawn_runtime import emit_spawn_started  # noqa: PLC0415
    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415

    spawnable: dict[str, Any] = {}
    for skill_id, resolution in runtime.resolved.items():
        ref = resolution.skill
        if ref is None:
            continue
        effect = parse_skill_effect(ref.meta)
        if effect is not None and effect.kind == EFFECT_SPAWN_SUBAGENT:
            spawnable[skill_id] = ref
    agent_id = str(getattr(agent_def, "id", "") or "")

    def spawn_skill_task(skill_id: str, task: str) -> str:
        wanted = str(skill_id or "").strip()
        ref = spawnable.get(wanted)
        if ref is None:
            raise ValueError(
                f"skill {wanted!r} is not a declared child-task skill; "
                f"available: {sorted(spawnable)}"
            )
        output, spawned, child_id = apply_spawn_skill_effect(
            ref, agent_id=agent_id, task=str(task or "")
        )
        app = _ctx.active_app()
        session_id = _ctx.active_session_id()
        assert app is not None and session_id
        emit_spawn_started(
            app,
            session_id,
            agent_def,
            child_id,
            str(task or "").strip(),
            int(getattr(spawned, "depth", 0) or 0),
            spawned,
        )
        return output

    return native_tool(
        spawn_skill_task,
        name="spawn_skill_task",
        presentation="specialized",
        # The skill-effect child launcher: gated on a declared skill's own
        # frontmatter, so "skills" fits better than the generic spawn/wait/
        # observe "agents" family (#1350).
        domain="skills",
        title="Start child agent",
        representation="handoff",
        desc=(
            "Run one declared child-task skill in a fresh background agent. "
            "Provide a concrete assignment; collect the returned task with wait_agent_tasks."
        ),
        args={
            "skill_id": {"type": "string", "description": "Declared child-task skill id."},
            "task": {"type": "string", "description": "Concrete assignment for the child."},
        },
    )
