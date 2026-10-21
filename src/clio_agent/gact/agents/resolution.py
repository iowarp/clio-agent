"""Which agent a session runs: its pinned plugin resolution.

A session freezes its resolved plugin set at start (``plugins-and-composition.md``,
"Sessions are pinned"). Session metadata carries:

* ``agent_ref`` -- the agent (``clio/standard``, ``user/my-default``, ...);
* ``agent_delta`` -- plugins added or removed inside this live session;
* ``agent_resolution`` -- the digest of the pinned :class:`Resolution`.

Parent updates and newly installed plugin versions reach the next session.
Adding or removing a plugin inside a live session reruns stage 2 and pins the
new result (:func:`pin_session_agent`). A defined subagent's child session pins
its own resolution, computed from the parent's.

Every function here is a query or an explicit pin; nothing falls back silently.
A session whose agent cannot resolve fails with the typed resolution errors.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent.gact.runtime.app_state import per_app_dict
from clio_agent.gact.types import AgentDef
from clio_agent.gact.workflow_state.schema import (
    GENERIC_WORKFLOW_STATE_SCHEMA,
    WorkflowStateSchema,
)
from clio_agent.plugins.resolve import Delta, Resolution, ResolutionError
from clio_agent.plugins.service import DEFAULT_AGENT, PluginService

if TYPE_CHECKING:
    from fastapi import FastAPI

AGENT_REF_KEY = "agent_ref"
AGENT_DELTA_KEY = "agent_delta"
AGENT_RESOLUTION_KEY = "agent_resolution"
SUBAGENT_KEY = "agent_subagent"


class SessionAgentUnavailable(RuntimeError):
    """The session's agent cannot be resolved; ``errors`` are the typed reasons."""

    def __init__(self, ref: str, reason: str, errors: list[dict[str, Any]]) -> None:
        detail = "; ".join(str(row.get("message") or row) for row in errors) or reason
        super().__init__(f"agent {ref!r} cannot run: {detail}")
        self.ref = ref
        self.reason = reason
        self.errors = errors


def plugin_service(app: Any) -> PluginService:
    """The app's plugin service (installed by ``build_app``)."""

    service = getattr(getattr(app, "state", None), "plugin_service", None)
    if service is None:
        raise RuntimeError("plugin service is not installed on this app")
    return service


def _session_metadata(app: Any, session_id: str) -> Mapping[str, Any]:
    sess = app.state.sessions.get(session_id) if session_id else None
    metadata = getattr(sess, "metadata", None) if sess is not None else None
    return metadata if isinstance(metadata, Mapping) else {}


def session_delta(app: Any, session_id: str) -> Delta:
    raw = _session_metadata(app, session_id).get(AGENT_DELTA_KEY) or {}
    if not isinstance(raw, Mapping):
        return Delta()
    return Delta(
        tuple(str(v) for v in raw.get("add") or ()), tuple(str(v) for v in raw.get("remove") or ())
    )


def default_agent_ref(app: Any) -> str:
    store = getattr(getattr(app, "state", None), "session_defaults", None)
    defaults = store.get() if store is not None and hasattr(store, "get") else store
    value = getattr(defaults, "agent_ref", "") if defaults is not None else ""
    return str(value or DEFAULT_AGENT)


def resolve_for_session(app: Any, ref: str, delta: Delta | None = None) -> Resolution:
    """Resolve an agent for a session without touching any session."""

    from clio_agent.plugins.service import AgentNotFound  # noqa: PLC0415

    try:
        return plugin_service(app).resolve(ref, delta=delta or Delta())
    except AgentNotFound as exc:
        raise SessionAgentUnavailable(
            ref, exc.reason, [{"code": exc.reason, "message": str(exc)}]
        ) from exc
    except ResolutionError as exc:
        raise SessionAgentUnavailable(
            ref, "agent_resolution_failed", [row.to_dict() for row in exc.diagnostics]
        ) from exc


def resolution_metadata(resolution: Resolution, *, ref: str, delta: Delta) -> dict[str, Any]:
    """The session metadata patch that pins ``resolution``."""

    return {
        AGENT_REF_KEY: ref,
        AGENT_DELTA_KEY: delta.to_dict(),
        AGENT_RESOLUTION_KEY: resolution.digest,
    }


def prepare_session_agent(app: Any, metadata: dict[str, Any], ref: str = "") -> Resolution:
    """Resolve a new session's agent and add its pin to ``metadata`` (in place).

    ``ref`` (or ``metadata["agent_ref"]``) names the agent; the session
    defaults' agent, else ``clio/standard``, otherwise. A session whose agent
    does not resolve is refused with the typed resolution errors.
    """

    chosen = ref or str(metadata.get(AGENT_REF_KEY) or "") or default_agent_ref(app)
    delta = Delta()
    raw = metadata.get(AGENT_DELTA_KEY)
    if isinstance(raw, Mapping):
        delta = Delta(
            tuple(str(v) for v in raw.get("add") or ()),
            tuple(str(v) for v in raw.get("remove") or ()),
        )
    resolution = resolve_for_session(app, chosen, delta)
    metadata.update(resolution_metadata(resolution, ref=chosen, delta=delta))
    return resolution


def pin_session_agent(
    app: Any, session_id: str, *, ref: str | None = None, delta: Delta | None = None
) -> Resolution:
    """Rerun stage 2 for a session and pin the result to it."""

    metadata = _session_metadata(app, session_id)
    if metadata.get(SUBAGENT_KEY):
        raise SessionAgentUnavailable(
            str(metadata.get(AGENT_REF_KEY) or ""),
            "subagent_plugins_fixed",
            [
                {
                    "code": "subagent_plugins_fixed",
                    "message": "a defined subagent's plugins come from its definition",
                }
            ],
        )
    chosen = ref or str(metadata.get(AGENT_REF_KEY) or "") or default_agent_ref(app)
    applied = delta if delta is not None else session_delta(app, session_id)
    resolution = resolve_for_session(app, chosen, applied)
    patch = resolution_metadata(resolution, ref=chosen, delta=applied)
    app.state.sessions.update(session_id, metadata_patch=patch, agent={"id": resolution.agent["id"]})
    return resolution


def session_resolution(app: Any, session_id: str) -> Resolution:
    """The resolution pinned to ``session_id``; pins one at first use."""

    metadata = _session_metadata(app, session_id)
    digest = str(metadata.get(AGENT_RESOLUTION_KEY) or "")
    if digest:
        cache = per_app_dict("pinned_resolutions", app=app)
        cached = cache.get(digest)
        if cached is not None:
            return cached
        pinned = plugin_service(app).pinned(digest)
        if pinned is None:
            raise SessionAgentUnavailable(
                str(metadata.get(AGENT_REF_KEY) or ""),
                "agent_resolution_missing",
                [
                    {
                        "code": "agent_resolution_missing",
                        "message": f"pinned resolution {digest} is not stored",
                    }
                ],
            )
        cache[digest] = pinned
        return pinned
    return pin_session_agent(app, session_id)


def resolution_agent_def(resolution: Resolution) -> AgentDef:
    """The executable ``AgentDef`` of a resolved agent or subagent."""

    agent = resolution.agent
    metadata = {
        "definition_kind": "plugin_agent",
        "agent_ref": agent.get("ref", ""),
        "agent_kind": agent.get("kind", "agent"),
        "resolution_digest": resolution.digest,
        "flow": dict(agent.get("flow") or {"id": "react", "options": {}}),
        "plugins": [row["id"] for row in resolution.plugins],
        "mcp_namespaces": sorted(resolution.mcp_servers),
        "prefers": dict(resolution.model.get("prefers") or {}),
        "skill_folders": sorted(
            {(row["plugin"], str(Path(row["path"]).parent.parent)) for row in resolution.skills}
        ),
    }
    structured = dict(agent.get("structured_outputs") or {})
    if resolution.workflow_state is not None:
        # A plugin declaring a workflow_state vocabulary makes its agents emit it.
        structured.setdefault("workflow_state", True)
    parameters: dict[str, Any] = {}
    if int(agent.get("max_iters") or 0) > 0:
        parameters["max_iters"] = int(agent["max_iters"])
    return AgentDef(
        id=str(agent["id"]),
        source="plugin",
        title=str(agent.get("name") or agent["id"]),
        description=str(agent.get("description") or ""),
        parent_id=str(agent.get("parent") or "") if agent.get("kind") == "subagent" else "",
        system_prompt=resolution.prompt_text,
        module={"kind": "react"},
        structured_outputs=structured,
        tools=[row["name"] for row in resolution.tools],
        skills=[row["id"] for row in resolution.skills],
        parameters=parameters,
        metadata=metadata,
        tier=2 if agent.get("kind") == "subagent" else 1,
    )


def session_agent_def(app: Any, session_id: str) -> AgentDef:
    """The session's agent, with the zero-tool allowlist applied."""

    agent_def = resolution_agent_def(session_resolution(app, session_id))
    return apply_session_tool_allowlist(app, agent_def, session_id=session_id)


def resolve_session_agent(app: Any, agent_id: str, *, session_id: str) -> AgentDef | None:
    """The session's agent, or one of its defined subagents, by id (None if neither)."""

    try:
        resolution = session_resolution(app, session_id)
    except SessionAgentUnavailable:
        return None
    if not agent_id or agent_id in (resolution.agent.get("id"), resolution.agent.get("ref")):
        return apply_session_tool_allowlist(
            app, resolution_agent_def(resolution), session_id=session_id
        )
    if not any(row["id"] == agent_id for row in resolution.subagents):
        return None
    child = plugin_service(app).resolve_subagent(resolution, agent_id)
    return resolution_agent_def(child)


def apply_session_tool_allowlist(
    app: Any, agent_def: AgentDef, *, session_id: str = ""
) -> AgentDef:
    """Force an agent to ZERO tools when its session carries an empty ``tool_allowlist``.

    The agent-driven elicitation answer child turn must have no tool surface at
    all, so a server's elicitation message cannot drive a tool-capable turn.
    ``tool_allowlist`` is stamped when the child session is minted. Only the
    explicit empty list is handled; an absent or non-empty allowlist is a no-op.
    """

    allowlist = _session_metadata(app, session_id).get("tool_allowlist")
    if not isinstance(allowlist, (list, tuple)) or allowlist:
        return agent_def
    return agent_def.model_copy(
        update={"tools": [], "skills": [], "module": {"kind": "chain_of_thought"}}
    )


def _runtime_workspace_catalog_cwd(
    app: "FastAPI",
    *,
    workspace_id: str = "",
    session_id: str = "",
) -> Path | None:
    """The bound workspace root of a session (or workspace), if any."""

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


def session_workflow_state_schema(
    app: "FastAPI | None", session_id: str = ""
) -> WorkflowStateSchema:
    """The typed ``workflow_state`` vocabulary a session's plugins declare.

    A plugin declares it under ``ai.iowarp.clio.workflow_state``. When no
    resolved plugin declares one, the generic presence-only engine runs and a
    ``workflow_state_schema_absent`` reason is recorded once per pinned
    resolution (no silent fallback).
    """

    if app is None or not session_id or getattr(app, "state", None) is None:
        return GENERIC_WORKFLOW_STATE_SCHEMA
    try:
        resolution = session_resolution(app, session_id)
    except SessionAgentUnavailable:
        return GENERIC_WORKFLOW_STATE_SCHEMA
    cache = per_app_dict("workflow_state_schemas", app=app)
    cached = cache.get(session_id)
    if cached is not None and cached[0] == resolution.digest:
        return cached[1]
    declaration = resolution.workflow_state
    if isinstance(declaration, Mapping):
        schema = WorkflowStateSchema.model_validate(declaration)
    else:
        schema = GENERIC_WORKFLOW_STATE_SCHEMA
        from clio_agent.gact.streaming import _record_workflow_schema_fallback  # noqa: PLC0415

        _record_workflow_schema_fallback(
            app,
            session_id,
            "workflow_state_schema_absent",
            f"agent {resolution.agent.get('ref')} has no plugin declaring a workflow_state schema",
        )
    cache[session_id] = (resolution.digest, schema)
    return schema


def spawn_targets(resolution: Resolution) -> list[dict[str, str]]:
    """Agents this agent may start as children: its defined subagents.

    General subagents (installed agents started with only a task prompt) are
    available through the ``delegation`` plugin and are listed by the spawn
    tool itself.
    """

    return [
        {"id": row["id"], "plugin": row["plugin"], "description": row["description"]}
        for row in resolution.subagents
    ]


def supplied_server_values(
    app: Any, mcp_servers: Mapping[str, Mapping[str, Any]], *, workspace_root: Path | None
) -> dict[str, str]:
    """The CLIO-supplied values a resolution's MCP servers reference.

    ``${CLIO_PROVENANCE_CONFIG}`` writes the provenance handoff for the
    session's workspace. An incomplete handoff is traced, not hidden; the
    server reads the problems from the file it was given.
    """

    from clio_agent.plugins.servers import (  # noqa: PLC0415
        PROVENANCE_CONFIG_VAR,
        references_variable,
    )

    specs = {name: row.get("spec") for name, row in mcp_servers.items()}
    if not references_variable(specs, PROVENANCE_CONFIG_VAR):
        return {}
    from clio_agent.gact.provenance.handoff import write_provenance_handoff  # noqa: PLC0415
    from clio_agent.runtime import trace  # noqa: PLC0415

    path, handoff = write_provenance_handoff(app, workspace_root=workspace_root)
    if handoff.problems:
        trace.event(
            "PLUGIN-SERVERS",
            "provenance_handoff_incomplete path=%s problems=%s",
            path,
            ",".join(problem.code for problem in handoff.problems),
        )
    return {PROVENANCE_CONFIG_VAR: str(path)}


def session_server_set(app: Any, session_id: str) -> str:
    """The plugin MCP server set of a session's agent ("" when it has none).

    An unresolvable agent mounts no plugin servers; its turn fails with the
    typed resolution errors on its own path.
    """

    from clio_agent.plugins.servers import register_server_set  # noqa: PLC0415

    if app is None or not session_id:
        return ""
    try:
        resolution = session_resolution(app, session_id)
    except SessionAgentUnavailable:
        return ""
    supplied = supplied_server_values(
        app,
        resolution.mcp_servers,
        workspace_root=_runtime_workspace_catalog_cwd(app, session_id=session_id),
    )
    return register_server_set(resolution.mcp_servers, supplied)


def agent_server_set(app: Any, ref: str) -> str:
    """The plugin MCP server set an agent would mount (draft warm-up, no session)."""

    from clio_agent.plugins.servers import register_server_set  # noqa: PLC0415

    try:
        resolution = plugin_service(app).resolve(ref, pin=False)
    except Exception:  # noqa: BLE001 - warm-up is best effort; the turn reports typed errors
        return ""
    supplied = supplied_server_values(app, resolution.mcp_servers, workspace_root=None)
    return register_server_set(resolution.mcp_servers, supplied)
