"""Agent / blueprint / expert-pack resolution for the GACT server (#714).

This module owns the stateless *resolution* helpers carved out of
``clio_agent.gact.app``: given a session/workspace, resolve the runtime
``AgentDef`` rows that actually execute -- from the active Agent Blueprint graph,
the expert-pack/builtin hierarchy, and the user/skill registry -- applying the
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

import os
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent.gact.agent_blueprints import (
    DEFAULT_AGENT_BLUEPRINT_ID,
    discover_agent_blueprints,
    load_agent_blueprint_path,
    load_agent_blueprints,
    validate_agent_hierarchy,
)
from clio_agent.gact.agents.composition import (
    _agent_rows_prompt_render_context,
    _apply_prompt_registry_to_agent,
    _prompt_render_context,
)
from clio_agent.gact.catalog import _builtin_agents, _load_skills_from_disk
from clio_agent.gact.expert_packs import load_expert_packs, validate_expert_hierarchy
from clio_agent.gact.types import AgentDef

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


def _resolve_dynamic_agent(
    app: "FastAPI",
    agent_id: str,
    *,
    prompt_registry: "PromptRegistry | None" = None,
) -> "AgentDef | None":
    """Return a registered user/skill/builtin/expert-pack agent definition by id."""
    if not agent_id:
        return None
    row = app.state.user_agents.get(agent_id)
    if row is not None:
        return _apply_prompt_registry_to_agent(
            app,
            AgentDef(**row.to_wire()),
            prompt_registry=prompt_registry,
        )
    for skill in _load_skills_from_disk():
        if skill.id == agent_id:
            return _apply_prompt_registry_to_agent(app, skill, prompt_registry=prompt_registry)
    expert_rows = validate_expert_hierarchy(
        _merge_agent_def_rows(_builtin_agents() + load_expert_packs())
    )
    for expert in expert_rows:
        if expert.id == agent_id and expert.enabled:
            return _apply_prompt_registry_to_agent(app, expert, prompt_registry=prompt_registry)
    return None


def _agent_definition_is_agent_blueprint(agent_def: "AgentDef") -> bool:
    """Return whether an AgentDef came from an Agent Blueprint graph."""

    metadata = agent_def.metadata if isinstance(agent_def.metadata, Mapping) else {}
    return bool(
        metadata.get("agent_blueprint_id") or metadata.get("definition_kind") == "agent_blueprint"
    )


def _legacy_native_expert_runtime_enabled() -> bool:
    return os.environ.get("CLIO_AGENT_ENABLE_LEGACY_NATIVE_EXPERTS", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _agent_definition_uses_blueprint_runtime(agent_def: "AgentDef") -> bool:
    return (
        _agent_definition_is_agent_blueprint(agent_def)
        and not _legacy_native_expert_runtime_enabled()
    )


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
    if _legacy_native_expert_runtime_enabled():
        return ""
    cwd = _runtime_workspace_catalog_cwd(app, session_id=session_id)
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


def _runtime_active_session_expert_pack_path(
    app: "FastAPI", session_id: str = ""
) -> Path | None:
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


def _runtime_active_agent_blueprint_rows(
    app: "FastAPI",
    *,
    session_id: str = "",
    workspace_id: str = "",
    prompt_registry: "PromptRegistry | None" = None,
) -> list["AgentDef"]:
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
    render_context = _prompt_render_context(app)
    render_context.update(_agent_rows_prompt_render_context(rows))
    render_context["session.active_agent_blueprint"] = (
        active_blueprint_id or "(no active agent blueprint)"
    )
    render_context["session.active_pack"] = active_blueprint_id or "(no active expert pack)"
    return [
        _apply_prompt_registry_to_agent(
            app,
            row,
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
    if requested_root and any(row.id == requested_root and row.enabled for row in rows):
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
