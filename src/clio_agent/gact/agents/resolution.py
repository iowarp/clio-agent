"""Agent / blueprint / expert-pack resolution for the GACT server (#714).

This module owns the stateless *resolution* helpers carved out of
``clio_agent.gact.app``: given a session/workspace, resolve the runtime
``AgentDef`` rows that actually execute -- from the active Agent Blueprint graph,
the expert-pack/builtin hierarchy, and the user-agent registry -- applying the
session agent-blueprint overlay and the prompt registry along the way.

Every function here is a *query*: it reads ``app.state`` (sessions, workspaces,
user agents, prompt registry) and the on-disk catalog through the leaf modules
(:mod:`clio_agent.gact.catalog`, :mod:`clio_agent.gact.agent_blueprints`,
:mod:`clio_agent.gact.expert_packs`), takes the ``FastAPI`` app as an explicit
parameter (no closure over ``build_app`` locals, no DSPy), and never mutates
state. It imports only the shared runtime base + gact leaves -- never
``gact.app`` -- so the dependency graph stays acyclic.

Resolution and prompt :mod:`~clio_agent.gact.agents.composition` co-depend
(resolution applies the prompt registry while rendering rows; composition renders
a tree from resolution's merge/child primitives). The cycle is broken here by
importing the concrete composition helpers directly at module load -- composition
in turn reaches back into this module through the package namespace at call time.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from clio_agent.gact import skills as _skills
from clio_agent.gact.agent_blueprints import (
    DEFAULT_AGENT_BLUEPRINT_ID,
    discover_agent_blueprints,
    load_agent_blueprint_path,
    load_agent_blueprints,
    load_mcp_descriptors,
    parse_agent_blueprint_root,
    validate_agent_hierarchy,
)
from clio_agent.gact.agents.composition import (
    _agent_rows_prompt_render_context,
    _apply_prompt_registry_to_agent,
    _prompt_render_context,
)
from clio_agent.gact.catalog import _builtin_agents
from clio_agent.gact.expert_packs import (
    discover_expert_packs,
    load_expert_packs,
    validate_expert_hierarchy,
)
from clio_agent.gact.runtime.app_state import per_app_dict
from clio_agent.gact.types import AgentCapabilityRef, AgentDef
from clio_agent.gact.workflow_state.schema import (
    GENERIC_WORKFLOW_STATE_SCHEMA,
    WorkflowStateSchema,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.prompts import PromptRegistry


def _merge_agent_def_rows(rows: list["AgentDef"]) -> list["AgentDef"]:
    """Resolve agent rows by id while preserving provenance of overridden rows."""

    merged: dict[str, AgentDef] = {}
    chains: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        chain = chains.setdefault(row.id, [])
        if row.id in merged:
            prior = merged[row.id]
            chain.append(
                {
                    "source": prior.source,
                    "scope": str(
                        prior.metadata.get("expert_scope") or prior.metadata.get("pack_scope") or ""
                    ),
                    "pack_id": str(prior.metadata.get("pack_id") or ""),
                    "definition_path": str(
                        prior.metadata.get("definition_path")
                        or prior.metadata.get("expert_path")
                        or ""
                    ),
                }
            )
        current = {
            "source": row.source,
            "scope": str(row.metadata.get("expert_scope") or row.metadata.get("pack_scope") or ""),
            "pack_id": str(row.metadata.get("pack_id") or ""),
            "definition_path": str(
                row.metadata.get("definition_path") or row.metadata.get("expert_path") or ""
            ),
        }
        merged[row.id] = row.model_copy(
            update={"metadata": {**row.metadata, "override_chain": [*chain, current]}}
        )
    return list(merged.values())


def _agent_overlay_patchable_fields() -> set[str]:
    """Expert fields a session agent overlay (the REST overlay API) may patch.

    This is the conservative set surfaced by ``/v1/sessions/{sid}/agent-overlay``:
    it deliberately omits the blueprint-runtime structural fields (``module``,
    ``signature``, ``structured_outputs``, ``fanout``) that
    :func:`_runtime_apply_session_agent_overlay` accepts when assembling the live
    runtime graph. Shared single-source by the overlay-validation +
    overlay-apply paths in :mod:`clio_agent.gact.routes.agents` and
    :mod:`clio_agent.gact.app`.
    """

    return {
        "title",
        "description",
        "system_prompt",
        "prompt_id",
        "prompt_profile",
        "default_provider",
        "default_model",
        "api_base",
        "credential_ref",
        "transport",
        "parent_id",
        "tier",
        "specialization",
        "keywords",
        "tools",
        "skills",
        "commands",
        "parameters",
        "enabled",
    }


def _resolve_dynamic_agent(
    app: "FastAPI",
    agent_id: str,
    *,
    prompt_registry: "PromptRegistry | None" = None,
) -> "AgentDef | None":
    """Return a registered user/builtin/expert-pack agent definition by id."""
    if not agent_id:
        return None
    row = app.state.user_agents.get(agent_id)
    if row is not None:
        return _apply_prompt_registry_to_agent(
            app,
            AgentDef(**row.to_wire()),
            prompt_registry=prompt_registry,
        )
    expert_rows = validate_expert_hierarchy(
        _merge_agent_def_rows(_builtin_agents() + load_expert_packs())
    )
    for expert in expert_rows:
        if expert.id == agent_id and expert.enabled:
            return _apply_prompt_registry_to_agent(app, expert, prompt_registry=prompt_registry)
    # LAST resort, after every real agent namespace (#918): an id that matches a
    # discovered skill gets the typed pointer error instead of a bare not-found.
    # Skills no longer occupy the agent-id namespace, so they never shadow a
    # real agent. Same process-cwd basis as the load_expert_packs() call above.
    skill_hit = _skills.SkillCatalog().resolve(agent_id)
    if skill_hit.status != "missing":
        raise _skills.SkillNotDelegatableError(agent_id, getattr(skill_hit.skill, "path", "") or "")
    return None


def _agent_definition_is_agent_blueprint(agent_def: "AgentDef") -> bool:
    """Return whether an AgentDef came from an Agent Blueprint graph."""

    metadata = agent_def.metadata if isinstance(agent_def.metadata, Mapping) else {}
    return bool(
        metadata.get("agent_blueprint_id") or metadata.get("definition_kind") == "agent_blueprint"
    )


def _agent_definition_uses_blueprint_runtime(agent_def: "AgentDef") -> bool:
    # An Agent Blueprint expert ALWAYS runs on the blueprint runtime: the legacy
    # native-expert runtime it could route to (the deleted Tier-1 planner) is gone
    # (#948 S4b), so there is no configuration under which it routes elsewhere.
    return _agent_definition_is_agent_blueprint(agent_def)


def _runtime_workspace_catalog_cwd(
    app: "FastAPI",
    *,
    workspace_id: str = "",
    session_id: str = "",
) -> Path | None:
    wid = workspace_id
    if session_id:
        sess = app.state.sessions.get(session_id)
        if sess is not None:
            wid = wid or str(getattr(sess, "workspace_id", "") or "")
    if not wid:
        return None
    ws = app.state.workspaces.get(wid)
    if ws is None:
        return None
    root_path = str(getattr(ws, "root_path", "") or "")
    return Path(root_path).expanduser() if root_path else None


def _runtime_active_agent_blueprint_id(app: "FastAPI", session_id: str = "") -> str:
    if not session_id:
        return ""
    sess = app.state.sessions.get(session_id)
    if sess is None:
        return ""
    metadata = getattr(sess, "metadata", {}) or {}
    if not isinstance(metadata, Mapping):
        return ""
    explicit = str(metadata.get("active_agent_blueprint_id") or "").strip()
    if explicit:
        return explicit
    cwd = _runtime_workspace_catalog_cwd(app, session_id=session_id)
    # An explicitly activated session pack, or a workspace/cwd pack discoverable
    # for this session, IS the session's agent set: the implicit default-registry
    # blueprint fallback below must not shadow it (that would drop the pack's
    # experts from both the catalog and the turn executor, breaking #770 C1's
    # list==execute invariant). Only the IMPLICIT default is suppressed here; an
    # EXPLICIT active blueprint (above) still wins. ``_agent_rows`` then composes
    # the builtin/user/pack fallback for these sessions.
    pack_cwd = cwd or Path.cwd()
    if (
        _runtime_active_session_expert_pack_id(app, session_id)
        or _runtime_active_session_expert_pack_path(app, session_id) is not None
        or discover_expert_packs(cwd=pack_cwd)
    ):
        return ""
    if any(
        row.id == DEFAULT_AGENT_BLUEPRINT_ID and row.enabled
        for row in discover_agent_blueprints(cwd=cwd)
    ):
        return DEFAULT_AGENT_BLUEPRINT_ID
    return ""


def _runtime_active_agent_blueprint_path(app: "FastAPI", session_id: str = "") -> Path | None:
    if not session_id:
        return None
    sess = app.state.sessions.get(session_id)
    if sess is None:
        return None
    metadata = getattr(sess, "metadata", {}) or {}
    if not isinstance(metadata, Mapping):
        return None
    raw = str(metadata.get("active_agent_blueprint_path") or "").strip()
    return Path(raw).expanduser() if raw else None


def _active_workflow_state_schema(
    app: "FastAPI | None", session_id: str = ""
) -> "WorkflowStateSchema":
    """THE one seam mapping a session's active blueprint to its typed
    workflow_state schema.

    A valid blueprint-level ``workflow_state`` declaration compiles to the typed
    engine; a bool-only / absent declaration falls back to
    ``GENERIC_WORKFLOW_STATE_SCHEMA`` (domain-free, presence-only). App-less /
    session-less callers get GENERIC too (out-of-band — nothing to attribute).
    The result is cached per (session, active-blueprint identity) on
    ``app.state.workflow_state_schemas`` so a blueprint switch re-resolves.

    Falling back to GENERIC records a loud, queryable
    ``workflow_state_schema_absent`` reason in the dedicated per-app ledger
    (``app.state.workflow_schema_fallbacks``; Slice E, no-silent-fallback rule).
    The cache guarantees the reason is recorded at most once per (session,
    active-blueprint identity). Malformed declarations are rejected at blueprint
    load (the blueprint is disabled with a ``validation_errors`` entry) and so
    never reach here — only an absent / bool-only declaration falls through.
    """

    if app is None or not session_id or getattr(app, "state", None) is None:
        # App-less, session-less, or a state-less app carrier: nothing to attribute
        # a blueprint to, so the generic engine is the honest answer (mirrors the
        # defensive ``getattr(app, "state", None)`` style of the ledger helpers).
        return GENERIC_WORKFLOW_STATE_SCHEMA
    blueprint_id = _runtime_active_agent_blueprint_id(app, session_id)
    blueprint_path = _runtime_active_agent_blueprint_path(app, session_id)
    cache = per_app_dict("workflow_state_schemas", app=app)
    key = (blueprint_id, str(blueprint_path or ""))
    cached = cache.get(session_id)
    if cached is not None and cached[0] == key:
        return cached[1]
    declaration: Any = None
    if blueprint_path is not None or blueprint_id:
        blueprint = (
            parse_agent_blueprint_root(blueprint_path, scope="session")
            if blueprint_path is not None
            else next(
                (
                    row
                    for row in discover_agent_blueprints(
                        cwd=_runtime_workspace_catalog_cwd(app, session_id=session_id)
                    )
                    if row.id == blueprint_id
                ),
                None,
            )
        )
        if blueprint is not None and blueprint.enabled:
            declaration = blueprint.metadata.get("workflow_state")
    if isinstance(declaration, Mapping):
        schema = WorkflowStateSchema.model_validate(declaration)
    else:
        schema = GENERIC_WORKFLOW_STATE_SCHEMA
        # Loud, queryable degradation (no-silent-fallback): the active blueprint
        # declares no workflow_state schema, so the generic presence-only engine
        # runs. Recorded once per (session, blueprint) — the cache below dedupes
        # re-resolutions. Lazy import mirrors the ambient-LM ledger seam and keeps
        # this resolution leaf free of a top-level ``streaming`` dependency.
        from clio_agent.gact.streaming import (  # noqa: PLC0415
            _record_workflow_schema_fallback,
        )

        _record_workflow_schema_fallback(
            app,
            session_id,
            "workflow_state_schema_absent",
            f"active blueprint {blueprint_id or '(none)'} declares no workflow_state schema",
        )
    cache[session_id] = (key, schema)
    return schema


def _runtime_active_session_expert_pack_id(app: "FastAPI", session_id: str = "") -> str:
    """Return a session's active expert-pack id from its metadata (``""`` if none)."""

    if not session_id:
        return ""
    sess = app.state.sessions.get(session_id)
    if sess is None:
        return ""
    metadata = getattr(sess, "metadata", {}) or {}
    if not isinstance(metadata, Mapping):
        return ""
    return str(
        metadata.get("active_expert_pack_id") or metadata.get("expert_pack_id") or ""
    ).strip()


def _runtime_active_session_expert_pack_path(app: "FastAPI", session_id: str = "") -> Path | None:
    """Return a session's active expert-pack path from its metadata (``None`` if none)."""

    if not session_id:
        return None
    sess = app.state.sessions.get(session_id)
    if sess is None:
        return None
    metadata = getattr(sess, "metadata", {}) or {}
    if not isinstance(metadata, Mapping):
        return None
    raw = str(metadata.get("active_expert_pack_path") or "").strip()
    return Path(raw).expanduser() if raw else None


def _runtime_session_agent_overlay(app: "FastAPI", session_id: str = "") -> dict[str, Any]:
    if not session_id:
        return {}
    sess = app.state.sessions.get(session_id)
    if sess is None:
        return {}
    metadata = getattr(sess, "metadata", {}) or {}
    if not isinstance(metadata, Mapping):
        return {}
    overlay = metadata.get("agent_blueprint_overlay")
    return dict(overlay) if isinstance(overlay, Mapping) else {}


def _runtime_apply_session_agent_overlay(
    app: "FastAPI",
    rows: list["AgentDef"],
    *,
    session_id: str = "",
) -> list["AgentDef"]:
    overlay = _runtime_session_agent_overlay(app, session_id)
    agents = overlay.get("agents") if isinstance(overlay, Mapping) else None
    if not isinstance(agents, Mapping):
        return rows
    patchable = {
        "title",
        "description",
        "system_prompt",
        "prompt_id",
        "prompt_profile",
        "default_provider",
        "default_model",
        "api_base",
        "credential_ref",
        "transport",
        "parent_id",
        "tier",
        "specialization",
        "keywords",
        "tools",
        "skills",
        "commands",
        "parameters",
        "module",
        "signature",
        "structured_outputs",
        "fanout",
        "enabled",
    }
    out: list[AgentDef] = []
    for row in rows:
        raw_patch = agents.get(row.id)
        if not isinstance(raw_patch, Mapping):
            out.append(row)
            continue
        update = {key: value for key, value in raw_patch.items() if key in patchable}
        metadata = {
            **row.metadata,
            "agent_blueprint_overlay": {
                "session_id": session_id,
                "fields": sorted(update),
                "status": "applied",
            },
        }
        out.append(row.model_copy(update={**update, "metadata": metadata}))
    return out


def _agent_with_capability_refs(app: "FastAPI", agent_def: "AgentDef") -> "AgentDef":
    """Attach normalized capability metadata to an AgentDef row.

    Projects the agent's declared tools/skills/commands into the
    ``capability_refs`` the TUI renders, folding the backend command table into
    the ``main`` orchestrator and self-registering a skill-sourced agent as its
    own skill. Takes ``app`` explicitly (no ``build_app`` closure) so both the
    ``/v1/agents`` route and the runtime turn path attach identical refs.
    """

    from clio_agent.gact.runtime.commands import BACKEND_COMMANDS  # noqa: PLC0415

    refs: list[AgentCapabilityRef] = [
        AgentCapabilityRef(kind="tool", id=tool_id, title=tool_id, source="builtin")
        for tool_id in agent_def.tools
    ]
    refs.extend(
        AgentCapabilityRef(kind="skill", id=skill_id, title=skill_id, source=agent_def.source)
        for skill_id in agent_def.skills
    )
    refs.extend(
        AgentCapabilityRef(
            kind="command",
            id=command_id,
            title=command_id,
            source="builtin",
        )
        for command_id in agent_def.commands
    )
    refs.extend(agent_def.capability_refs)

    if agent_def.id == "main":
        command_ids = set(agent_def.commands)
        for row in BACKEND_COMMANDS:
            command_id = row["id"]
            if command_id in command_ids:
                continue
            raw_status = row.get("status")
            status: Literal["available", "unavailable", "unknown"] = (
                raw_status if raw_status in {"available", "unavailable", "unknown"} else "available"
            )
            refs.append(
                AgentCapabilityRef(
                    kind="command",
                    id=command_id,
                    title=row.get("title", command_id),
                    description=row.get("description", ""),
                    source=row.get("source", "builtin"),
                    status=status,
                    metadata=({"error": row["error"]} if row.get("error") else {}),
                )
            )
            command_ids.add(command_id)
        agent_def = agent_def.model_copy(update={"commands": sorted(command_ids)})

    deduped: list[AgentCapabilityRef] = []
    seen: set[tuple[str, str]] = set()
    for ref in refs:
        key = (ref.kind, ref.id)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(ref)

    return agent_def.model_copy(update={"capability_refs": deduped})


def _enabled_agent_blueprint_mcp_tool_names(app: "FastAPI", blueprint_id: str = "") -> set[str]:
    """Return the names of MCP tools a ready blueprint server currently exposes."""

    names: set[str] = set()
    for server in (getattr(app.state, "external_mcp_servers", {}) or {}).values():
        if not isinstance(server, Mapping):
            continue
        if str(server.get("status") or "") != "ready":
            continue
        if blueprint_id and str(server.get("agent_blueprint_id") or "") != blueprint_id:
            continue
        for tool in server.get("tools") or []:
            if not isinstance(tool, Mapping):
                continue
            if not bool(tool.get("enabled")) or str(tool.get("status") or "") != "ready":
                continue
            tool_name = str(tool.get("name") or tool.get("id") or "").strip()
            if tool_name:
                names.add(tool_name)
    return names


def _agent_blueprint_descriptor_tools(rows: list["AgentDef"]) -> dict[str, str]:
    """Map each blueprint-declared MCP tool name to its on-disk descriptor id."""

    descriptors_by_tool: dict[str, str] = {}
    roots: dict[str, tuple[str, str]] = {}
    for row in rows:
        root_file = str(row.metadata.get("agent_blueprint_definition_path") or "").strip()
        if not root_file:
            continue
        roots[root_file] = (
            str(row.metadata.get("agent_blueprint_scope") or "session"),
            str(row.metadata.get("agent_blueprint_id") or ""),
        )
    for root_file, (scope, blueprint_id) in sorted(roots.items()):
        root = Path(root_file).expanduser().parent
        try:
            descriptors = load_mcp_descriptors(root, scope=scope, blueprint_id=blueprint_id)
        except Exception:  # noqa: BLE001 - disk read; a broken descriptor gates nothing
            continue
        for descriptor in descriptors:
            descriptor_id = str(descriptor.get("id") or "")
            for tool in descriptor.get("tools") or []:
                if not isinstance(tool, Mapping):
                    continue
                tool_name = str(tool.get("name") or tool.get("id") or "").strip()
                if tool_name:
                    descriptors_by_tool[tool_name] = descriptor_id
    return descriptors_by_tool


def _apply_agent_blueprint_mcp_descriptor_validation(
    app: "FastAPI",
    rows: list["AgentDef"],
) -> list["AgentDef"]:
    """Disable experts whose declared MCP tools require (absent) explicit enablement."""

    descriptor_tools = _agent_blueprint_descriptor_tools(rows)
    if not descriptor_tools:
        return rows
    out: list[AgentDef] = []
    for row in rows:
        enabled_tools = _enabled_agent_blueprint_mcp_tool_names(
            app, str(row.metadata.get("agent_blueprint_id") or "").strip()
        )
        errors = list(row.validation_errors)
        diagnostics = list(row.metadata.get("tool_diagnostics", []))
        for tool_name in row.tools:
            if tool_name not in descriptor_tools or tool_name in enabled_tools:
                continue
            descriptor_id = descriptor_tools[tool_name]
            message = f"MCP tool requires explicit enablement: {tool_name}" + (
                f" (descriptor: {descriptor_id})" if descriptor_id else ""
            )
            if message not in errors:
                errors.append(message)
            if not any(
                isinstance(diag, Mapping)
                and str(diag.get("tool") or "") == tool_name
                and str(diag.get("source") or "") == "agent_blueprint_mcp_descriptor"
                for diag in diagnostics
            ):
                diagnostics.append(
                    {
                        "tool": tool_name,
                        "status": "disabled",
                        "source": "agent_blueprint_mcp_descriptor",
                        "descriptor_id": descriptor_id,
                    }
                )
        metadata = dict(row.metadata)
        if diagnostics:
            metadata["tool_diagnostics"] = diagnostics
        if errors != list(row.validation_errors):
            metadata["mcp_descriptor_validation_disabled"] = True
        out.append(
            row.model_copy(
                update={
                    "enabled": row.enabled and not errors,
                    "validation_errors": errors,
                    "metadata": metadata,
                }
            )
        )
    return out


def _apply_enabled_agent_blueprint_mcp_tools(
    app: "FastAPI",
    rows: list["AgentDef"],
) -> list["AgentDef"]:
    """Re-enable experts whose declared MCP tools became ready on a live server."""

    out: list[AgentDef] = []
    cache: dict[str, set[str]] = {}
    for row in rows:
        blueprint_id = str(row.metadata.get("agent_blueprint_id") or "").strip()
        enabled_tools = cache.setdefault(
            blueprint_id,
            _enabled_agent_blueprint_mcp_tool_names(app, blueprint_id),
        )
        if not enabled_tools:
            out.append(row)
            continue
        row_tools = {str(tool).strip() for tool in row.tools if str(tool).strip()}
        resolved_tools = row_tools & enabled_tools
        if not resolved_tools:
            out.append(row)
            continue
        errors = [
            error
            for error in row.validation_errors
            if not any(
                error.startswith(f"MCP tool requires explicit enablement: {tool}")
                for tool in resolved_tools
            )
        ]
        diagnostics = [
            diag
            for diag in row.metadata.get("tool_diagnostics", [])
            if not (
                isinstance(diag, Mapping)
                and str(diag.get("source") or "") == "agent_blueprint_mcp_descriptor"
                and str(diag.get("tool") or "") in resolved_tools
            )
        ]
        metadata = dict(row.metadata)
        if diagnostics:
            metadata["tool_diagnostics"] = diagnostics
        else:
            metadata.pop("tool_diagnostics", None)
        disabled_by_mcp_validation = bool(metadata.pop("mcp_descriptor_validation_disabled", False))
        out.append(
            row.model_copy(
                update={
                    "enabled": row.enabled or (disabled_by_mcp_validation and not errors),
                    "validation_errors": errors,
                    "metadata": metadata,
                }
            )
        )
    return out


def _runtime_active_agent_blueprint_rows(
    app: "FastAPI",
    *,
    session_id: str = "",
    workspace_id: str = "",
    prompt_registry: "PromptRegistry | None" = None,
) -> list["AgentDef"]:
    """Resolve the effective blueprint AgentDef rows for a session.

    This is the ONE seam both ``GET /v1/agents`` (via ``_agent_rows`` in
    :mod:`clio_agent.gact.app`) and the runtime turn path share. It applies, in
    order: the default-blueprint fallback (when a session pinned no explicit
    blueprint but a discoverable ``DEFAULT_AGENT_BLUEPRINT_ID`` exists), the
    session agent overlay, hierarchy validation, MCP tool-gating (descriptor
    validation + live-server re-enable), capability-ref projection, and the
    prompt registry -- so the route and the executing agent can never disagree
    on what an agent resolves to.
    """

    if not session_id:
        return []
    cwd = _runtime_workspace_catalog_cwd(app, workspace_id=workspace_id, session_id=session_id)
    active_blueprint_id = _runtime_active_agent_blueprint_id(app, session_id)
    active_blueprint_path = _runtime_active_agent_blueprint_path(app, session_id)
    if active_blueprint_path is not None:
        rows = load_agent_blueprint_path(active_blueprint_path, scope="session")
    elif active_blueprint_id:
        rows = load_agent_blueprints(cwd=cwd, blueprint_id=active_blueprint_id)
    else:
        rows = []
    if not rows:
        return []
    rows = _runtime_apply_session_agent_overlay(app, rows, session_id=session_id)
    rows = validate_agent_hierarchy(_merge_agent_def_rows(rows))
    rows = _apply_agent_blueprint_mcp_descriptor_validation(app, rows)
    rows = _apply_enabled_agent_blueprint_mcp_tools(app, rows)
    render_context = _prompt_render_context(app)
    render_context.update(_agent_rows_prompt_render_context(rows))
    render_context["session.active_agent_blueprint"] = (
        active_blueprint_id or "(no active agent blueprint)"
    )
    render_context["session.active_pack"] = active_blueprint_id or "(no active expert pack)"
    return [
        _apply_prompt_registry_to_agent(
            app,
            _agent_with_capability_refs(app, row),
            prompt_registry=prompt_registry,
            render_context=render_context,
        )
        for row in rows
    ]


def _runtime_active_agent_blueprint_agent_ids(app: "FastAPI", session_id: str = "") -> set[str]:
    return {
        row.id
        for row in _runtime_active_agent_blueprint_rows(app, session_id=session_id)
        if row.enabled
    }


def _runtime_child_agent_rows(
    app: "FastAPI",
    parent_id: str,
    *,
    session_id: str = "",
) -> list["AgentDef"]:
    """Return the enabled child experts declared for ``parent_id``.

    Child discovery must mirror the resolution path that actually executes the
    delegated agents. ``_runtime_active_agent_blueprint_rows`` only covers agents
    sourced from an active Agent Blueprint graph; experts resolved through the
    expert-pack/builtin hierarchy (``_resolve_dynamic_agent``) are invisible to
    it. When the running parent is not part of the active blueprint, fall back to
    the same hierarchy so its declared children are still discoverable for
    synchronous delegation, continuation contracts, and child-tool wiring.
    """

    if not parent_id:
        return []
    blueprint_rows = _runtime_active_agent_blueprint_rows(app, session_id=session_id)
    children = [row for row in blueprint_rows if row.enabled and row.parent_id == parent_id]
    if children:
        return children
    if any(row.id == parent_id and row.enabled for row in blueprint_rows):
        # The parent lives in the active blueprint and simply has no children.
        return []
    cwd = _runtime_workspace_catalog_cwd(app, session_id=session_id)
    expert_rows = validate_expert_hierarchy(
        _merge_agent_def_rows(_builtin_agents() + load_expert_packs(cwd=cwd))
    )
    return [row for row in expert_rows if row.enabled and row.parent_id == parent_id]


def _runtime_declared_child_ids(
    app: "FastAPI",
    parent_id: str,
    *,
    session_id: str = "",
) -> set[str]:
    """Return the set of enabled child expert ids declared for ``parent_id``."""

    return {row.id for row in _runtime_child_agent_rows(app, parent_id, session_id=session_id)}


def _runtime_active_agent_blueprint_root_id(app: "FastAPI", session_id: str = "") -> str:
    rows = _runtime_active_agent_blueprint_rows(app, session_id=session_id)
    if not rows:
        return ""
    requested_root = str(rows[0].metadata.get("agent_blueprint_root_expert") or "").strip()
    if requested_root and any(row.id == requested_root for row in rows):
        # The DECLARED root is the root, enabled or not. A disabled root is a
        # fact the turn path fails TYPED on (_BlueprintRootDisabled) — silently
        # substituting another enabled expert as root ran a leaf as the
        # orchestrator on the live gate (#948 S4). Substitution below applies
        # only when the manifest declares no resolvable root at all.
        return requested_root
    roots = [row for row in rows if row.enabled and not row.parent_id]
    if len(roots) == 1:
        return roots[0].id
    enabled = [row for row in rows if row.enabled]
    if not enabled:
        return ""
    return sorted(enabled, key=lambda row: (row.tier, row.id))[0].id


def _resolve_runtime_dynamic_agent(
    app: "FastAPI",
    agent_id: str,
    *,
    session_id: str = "",
    workspace_id: str = "",
    prompt_registry: "PromptRegistry | None" = None,
) -> "AgentDef | None":
    if session_id:
        for row in _runtime_active_agent_blueprint_rows(
            app,
            session_id=session_id,
            workspace_id=workspace_id,
            prompt_registry=prompt_registry,
        ):
            if row.id == agent_id and row.enabled:
                return row
    return _resolve_dynamic_agent(app, agent_id, prompt_registry=prompt_registry)
