"""Detached-executor context binding for child task specifications (#1122)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.turn_spawn import TaskSpec

_SESSION_SCOPE_PREFIXES = ("active_agent_blueprint_", "active_expert_pack_")
_SESSION_SCOPE_KEYS = ("expert_pack_id",)
_SESSION_MODES = frozenset({"plan", "edit", "architect"})


def inherited_session_scope_metadata(parent: Any) -> dict[str, Any]:
    """Return the parent's blueprint/expert-pack activation keys verbatim."""

    metadata = getattr(parent, "metadata", None)
    if not isinstance(metadata, Mapping):
        return {}
    return {
        key: value
        for key, value in metadata.items()
        if key.startswith(_SESSION_SCOPE_PREFIXES) or key in _SESSION_SCOPE_KEYS
    }


def resolve_spawn_bindings(parent: Any, spec: "TaskSpec") -> tuple[str, str, dict[str, Any]]:
    """Resolve child context with field-wise spec-first, parent-second precedence."""

    from clio_agent.gact.turn_spawn import SpawnError  # noqa: PLC0415

    workspace_id = (
        spec.workspace_id
        if spec.workspace_id is not None
        else getattr(parent, "workspace_id", None)
    )
    session_mode = (
        spec.session_mode if spec.session_mode is not None else getattr(parent, "mode", None)
    )
    scope = (
        spec.session_scope_metadata
        if spec.session_scope_metadata is not None
        else (inherited_session_scope_metadata(parent) if parent is not None else None)
    )
    missing = [
        name
        for name, value in (
            ("workspace_id", workspace_id),
            ("session_mode", session_mode),
            ("session_scope_metadata", scope),
        )
        if value is None
    ]
    if missing:
        raise SpawnError(
            "spawn has no live parent session and lacks explicit binding(s): " + ", ".join(missing),
            reason="spawn_parent_bindings_unavailable",
        )
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        raise SpawnError(
            "spawn workspace_id binding must be a non-empty string",
            reason="spawn_bindings_invalid",
        )
    if session_mode not in _SESSION_MODES:
        raise SpawnError(
            f"spawn session_mode binding is invalid: {session_mode!r}",
            reason="spawn_bindings_invalid",
        )
    if not isinstance(scope, Mapping):
        raise SpawnError(
            "spawn session_scope_metadata binding must be a mapping",
            reason="spawn_bindings_invalid",
        )
    return (
        workspace_id,
        session_mode,
        {
            key: value
            for key, value in scope.items()
            if isinstance(key, str)
            and (key.startswith(_SESSION_SCOPE_PREFIXES) or key in _SESSION_SCOPE_KEYS)
        },
    )


def validate_task_spec(app: "FastAPI", spec: "TaskSpec") -> tuple[str, str, dict[str, Any]]:
    """Validate one local or detached spawn and return its resolved bindings.

    Both invoker implementations call this owner so depth, declaration, and
    self-contained binding failures retain the same typed SpawnError.
    """

    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        _runtime_declared_child_ids,
    )
    from clio_agent.gact.turn_spawn import (  # noqa: PLC0415
        MAX_SPAWN_DEPTH,
        SpawnError,
    )

    if spec.depth > MAX_SPAWN_DEPTH:
        raise SpawnError(
            f"spawn depth {spec.depth} exceeds max {MAX_SPAWN_DEPTH}",
            reason="spawn_depth_exceeded",
        )
    parent = app.state.sessions.get(spec.parent_session_id)
    workspace_id, parent_mode, session_scope_metadata = resolve_spawn_bindings(parent, spec)
    spec_has_bindings = any(
        value is not None
        for value in (spec.workspace_id, spec.session_mode, spec.session_scope_metadata)
    )
    parent_bindings_match = parent is not None and (
        workspace_id == getattr(parent, "workspace_id", None)
        and parent_mode == getattr(parent, "mode", None)
        and session_scope_metadata == inherited_session_scope_metadata(parent)
    )
    if not spec.skip_declared_check:
        if spec_has_bindings and not parent_bindings_match:
            declared = declared_child_ids_from_bindings(
                app,
                spec.requesting_expert_id,
                workspace_id=workspace_id,
                session_scope_metadata=session_scope_metadata,
            )
        else:
            declared = _runtime_declared_child_ids(
                app, spec.requesting_expert_id, session_id=spec.parent_session_id
            )
        if spec.child_expert_id not in declared:
            raise SpawnError(
                f"{spec.child_expert_id!r} is not a declared child of "
                f"{spec.requesting_expert_id!r} (declared: {sorted(declared)})",
                reason="undeclared_child",
            )
    return workspace_id, parent_mode, session_scope_metadata


def bind_task_spec_to_parent(app: "FastAPI", spec: "TaskSpec") -> "TaskSpec":
    """Populate a production spec from its live parent without overriding fields."""

    parent = app.state.sessions.get(spec.parent_session_id)
    if parent is None or not hasattr(parent, "workspace_id") or not hasattr(parent, "mode"):
        return spec
    workspace_id, session_mode, scope = resolve_spawn_bindings(parent, spec)
    return replace(
        spec,
        workspace_id=workspace_id,
        session_mode=session_mode,
        session_scope_metadata=scope,
    )


def declared_child_ids_from_bindings(
    app: "FastAPI",
    parent_id: str,
    *,
    workspace_id: str,
    session_scope_metadata: Mapping[str, Any],
) -> set[str]:
    """Resolve declared children from detached workspace and scope bindings."""

    from clio_agent.gact.agent_blueprints import (  # noqa: PLC0415
        load_agent_blueprint_path,
        load_agent_blueprints,
        validate_agent_hierarchy,
    )
    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        _apply_agent_blueprint_mcp_descriptor_validation,
        _apply_enabled_agent_blueprint_mcp_tools,
        _merge_agent_def_rows,
        _runtime_workspace_catalog_cwd,
    )
    from clio_agent.gact.catalog import _builtin_agents  # noqa: PLC0415
    from clio_agent.gact.expert_packs import (  # noqa: PLC0415
        load_expert_pack_path,
        load_expert_packs,
        validate_expert_hierarchy,
    )

    cwd = _runtime_workspace_catalog_cwd(app, workspace_id=workspace_id)
    blueprint_path = str(session_scope_metadata.get("active_agent_blueprint_path") or "").strip()
    blueprint_id = str(session_scope_metadata.get("active_agent_blueprint_id") or "").strip()
    if blueprint_path:
        rows = load_agent_blueprint_path(Path(blueprint_path).expanduser(), scope="session")
    elif blueprint_id:
        rows = load_agent_blueprints(cwd=cwd, blueprint_id=blueprint_id)
    else:
        rows = []
    if rows:
        rows = validate_agent_hierarchy(_merge_agent_def_rows(rows))
        rows = _apply_agent_blueprint_mcp_descriptor_validation(app, rows)
        rows = _apply_enabled_agent_blueprint_mcp_tools(app, rows)
        children = {row.id for row in rows if row.enabled and row.parent_id == parent_id}
        if children or any(row.id == parent_id and row.enabled for row in rows):
            return children

    pack_path = str(session_scope_metadata.get("active_expert_pack_path") or "").strip()
    pack_rows = (
        load_expert_pack_path(Path(pack_path).expanduser(), scope="session")
        if pack_path
        else load_expert_packs(cwd=cwd)
    )
    expert_rows = validate_expert_hierarchy(_merge_agent_def_rows(_builtin_agents() + pack_rows))
    return {row.id for row in expert_rows if row.enabled and row.parent_id == parent_id}
