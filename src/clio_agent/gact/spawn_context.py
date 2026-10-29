"""Resolve a child agent and prepare its session; no execution or lifecycle ownership.

A child is one of the parent agent's defined subagents (shipped by the parent's
resolved plugins), an installed agent started as a general subagent through the
``delegation`` plugin, or ``self``: the parent's own agent, which a flow run
uses. A ``fork:<session>`` target is a fork of that session: its exact
resolution, delta, role and flow, on its model, with its conversation copied in
before the fork's first turn (:mod:`~clio_agent.gact.task_forks`). Either way the
child session pins its own resolution, computed here outside task locking, before
the assignment is accepted.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from clio_agent.arc.task_types import argument_digest
from clio_agent.gact.sessions import Session as StoredSession
from clio_agent.gact.sessions import SessionStore

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.plugins.resolve import Resolution

CreatePreparedChild = Callable[[str, str, dict[str, Any], str | None], StoredSession]
PrepareChild = Callable[[str, str, dict[str, Any] | None, str], CreatePreparedChild]

#: A spawn target ``fork:<session>`` forks that session (the ``fork`` tool's target).
FORK_TARGET = "fork:"


def _launch_context(parent: StoredSession) -> dict[str, Any]:
    return {
        "workspace_id": parent.workspace_id,
        "model": parent.model,
        "agent": parent.agent,
        "mode": parent.mode,
        "edit_mode": parent.edit_mode,
        "approval_mode": parent.approval_mode,
        "approval_profile": parent.approval_profile,
        "metadata": {
            key: value
            for key, value in parent.metadata.items()
            if key in {"owner_human_id", "workflow_id", "workflow_step", "agent_resolution"}
        },
    }


def resolve_child_agent(
    app: "FastAPI", parent_id: str, agent_id: str
) -> tuple["Resolution", dict[str, Any]]:
    """The child's resolution and the session metadata that pins it.

    Raises ``ValueError`` naming why when ``agent_id`` is neither a defined
    subagent of the parent's agent nor an installed agent.
    """

    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        AGENT_DELTA_KEY,
        AGENT_REF_KEY,
        AGENT_RESOLUTION_KEY,
        SUBAGENT_KEY,
        SessionAgentUnavailable,
        plugin_service,
        resolve_for_session,
        session_resolution,
    )
    from clio_agent.plugins.resolve import ResolutionError, delegable_subagents  # noqa: PLC0415

    if agent_id.startswith(FORK_TARGET):
        # A fork runs its source's exact resolution, delta, role and flow.
        source = agent_id.removeprefix(FORK_TARGET)
        forked = session_resolution(app, source)
        metadata = dict(getattr(app.state.sessions.get(source), "metadata", None) or {})
        pin = {
            key: metadata[key]
            for key in (AGENT_REF_KEY, AGENT_DELTA_KEY, SUBAGENT_KEY, "agent_flow")
            if key in metadata
        }
        return forked, {**pin, AGENT_RESOLUTION_KEY: forked.digest}
    parent = session_resolution(app, parent_id)
    if agent_id == "self":
        # A flow run (``run_flow``, ``run_workflow``, ``ctx.call`` on a flow) runs the
        # parent's own agent, pinned to the parent's exact resolution and delta.
        session = app.state.sessions.get(parent_id)
        metadata = dict(getattr(session, "metadata", None) or {})
        pin = {
            key: metadata[key]
            for key in (AGENT_REF_KEY, AGENT_DELTA_KEY, SUBAGENT_KEY)
            if key in metadata
        }
        return parent, {**pin, AGENT_RESOLUTION_KEY: parent.digest}
    if any(row["id"] == agent_id for row in delegable_subagents(parent)):
        try:
            child = plugin_service(app).resolve_subagent(parent, agent_id)
        except ResolutionError as exc:
            raise ValueError(f"subagent {agent_id!r} cannot resolve: {exc}") from exc
        return child, {
            AGENT_REF_KEY: child.agent["ref"],
            AGENT_DELTA_KEY: {"add": [], "remove": []},
            AGENT_RESOLUTION_KEY: child.digest,
            SUBAGENT_KEY: agent_id,
        }
    try:
        child = resolve_for_session(app, agent_id)
    except SessionAgentUnavailable as exc:
        defined = ", ".join(row["id"] for row in delegable_subagents(parent)) or "none"
        raise ValueError(
            f"Requested child agent {agent_id!r} is neither a subagent of "
            f"{parent.agent.get('ref')} (subagents: {defined}) nor a resolvable installed agent: {exc}"
        ) from exc
    return child, {
        AGENT_REF_KEY: agent_id,
        AGENT_DELTA_KEY: {"add": [], "remove": []},
        AGENT_RESOLUTION_KEY: child.digest,
    }


def prepare_child_session(
    app: FastAPI,
    parent_id: str,
    agent_id: str,
    target_scope: dict[str, Any] | None,
    model: str = "",
) -> CreatePreparedChild:
    """Resolve the child and choose its model outside task locking; return finite
    acceptance metadata work.

    Raises:
        ModelSelectionRefused: The named model (or the caller's) cannot run the child.
    """
    from clio_agent.gact.agent_task_limits import max_spawn_depth  # noqa: PLC0415
    from clio_agent.gact.fleet.child_model import choose_child_model  # noqa: PLC0415
    from clio_agent.gact.fleet.selection import MODEL_CHOICE_KEY  # noqa: PLC0415

    store: SessionStore = app.state.sessions
    if store.snapshot(parent_id) is None:
        raise ValueError("Parent session is unavailable")
    from clio_agent.gact.agents.resolution import session_resolution  # noqa: PLC0415

    # Pin a never-pinned parent before the launch snapshot, so the snapshot carries
    # the pin; a change during child discovery then fails the digest check below.
    session_resolution(app, parent_id)
    parent = store.snapshot(parent_id)
    if parent is None:
        raise ValueError("Parent session is unavailable")
    launch = _launch_context(parent)
    digest = argument_digest(launch)
    child, pin = resolve_child_agent(app, parent_id, agent_id)
    source = (
        store.snapshot(agent_id.removeprefix(FORK_TARGET))
        if agent_id.startswith(FORK_TARGET)
        else None
    )
    if source is not None and not model:
        # A fork runs on the model its source runs on.
        model = "/".join(
            str(source.model.get(key) or "") for key in ("provider_id", "model_id")
        ).strip("/")
    choice = choose_child_model(
        app, parent, child, explicit=model, same_agent=agent_id == "self" or source is not None
    )
    pin = {**pin, "spawn_target": agent_id, MODEL_CHOICE_KEY: choice.to_dict()}
    title = (
        f"{source.title} (fork)"
        if source is not None
        else str(child.agent.get("name") or child.agent["id"])
    )
    # A role declared ``effects="read"`` runs under the side sessions' read-only
    # profile: the same gate, the same typed deny reason, no spawn or workflow tools.
    read_only = child.agent.get("effects") == "read"

    def create(
        creation_id: str,
        creation_digest: str,
        initial_metadata: dict[str, Any],
        approval_profile: str | None,
    ) -> StoredSession:
        current = store.snapshot(parent_id)
        if current is None or argument_digest(_launch_context(current)) != digest:
            raise ValueError("Parent execution configuration changed during child preparation")
        history = app.state.task_history
        cursor, depth = history.latest_assignment(parent_id), 1
        while cursor is not None and cursor.owner.parent_handle is not None:
            depth += 1
            cursor = history.task_snapshot(cursor.owner.parent_handle)
        limit = max_spawn_depth()
        if limit and depth > limit:
            raise ValueError("Subagent spawn depth exceeds the configured safety boundary")
        inherited = {**launch["metadata"], **(target_scope or {}), **initial_metadata, **pin}
        # An isolated run's node folder: a step's own, a fork's copy of its source's,
        # or nested under a node that delegates.
        from clio_agent.gact.node_folders import child_node_metadata  # noqa: PLC0415

        inherited.update(
            child_node_metadata(
                current.metadata, inherited, source.metadata if source else None, title
            )
        )
        if read_only:
            from clio_agent.gact.side_sessions import READ_ONLY_SIDE_PROFILE  # noqa: PLC0415

            approval_profile = READ_ONLY_SIDE_PROFILE
        created = store.create(
            workspace_id=parent.workspace_id,
            title=str(inherited.get("subagent_name") or title),
            parent_session_id=parent_id,
            model={**choice.ran, "variant": ""} if choice.ran else dict(parent.model),
            agent={"id": child.agent["id"]},
            mode=parent.mode,
            edit_mode=parent.edit_mode,
            approval_mode="ask" if approval_profile is not None else parent.approval_mode,
            approval_profile=parent.approval_profile
            if approval_profile is None
            else approval_profile,
            metadata=inherited,
            creation_id=creation_id,
            creation_digest=creation_digest,
            defer_initialization=True,
        )
        if source is not None:
            # The fork starts where its source is: its conversation through the last
            # finished tool result, before the assignment (the fork's note) arrives.
            from clio_agent.gact.conversation_copy import (
                copy_conversation,  # noqa: PLC0415
                fork_side_result,  # noqa: PLC0415
            )

            call = dict(inherited.get("fork_call") or {})
            copy_conversation(
                app,
                source.id,
                created.id,
                open_turn=True,
                fork_call={
                    "call_id": call.get("call_id", ""),
                    "result": fork_side_result(source.id, call.get("note")),
                }
                if call.get("call_id")
                else None,
            )
        return created

    # The spending guard reads the choice before the child is created.
    create.model_choice = choice.to_dict()  # type: ignore[attr-defined]
    return create
