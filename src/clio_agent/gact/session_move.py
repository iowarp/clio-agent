"""Move a user conversation to another workspace, keeping its session id.

``POST /v1/sessions/{sid}/move`` (:mod:`clio_agent.gact.routes.session_move`) owns
the HTTP contract; this module owns what moves with the conversation.

* **Moves:** the session row (``workspace_id``), and with it everything keyed by the
  session id: transcript, ARC context, context-file ledger, session-scoped
  permission policies, session memory, scratch, usage and metadata. The sessions
  the conversation owns move too: its delegated agent sessions and side sessions
  (kinds ``agent`` and ``lookup``), transitively, because they cannot be moved on
  their own and would otherwise be stranded in the old workspace. The session's
  agent-managed tool-output folder (spills and viewed-media snapshots) is relocated
  from the old workspace's state to the new one, so the conversation can still read
  what it viewed and deleting the session still removes it.
* **Stays:** user branches of the conversation (kind ``branch``): each is its own
  row in the conversation list and moves on its own. Workspace files, artifacts,
  resources, connected storage, workspace-scoped grants and policies, and
  workspace/project memory belong to the workspace and stay. References to them in
  the transcript are not rewritten; the next turn runs with the target workspace's
  root, grants, policies and project memory (a ``run_code`` kernel restarts there).
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from clio_agent.gact.session_kind import session_kind
from clio_agent.platform_paths import win_extended_path
from clio_agent.runtime import trace

logger = logging.getLogger(__name__)

__all__ = [
    "MOVABLE_KINDS",
    "OWNED_KINDS",
    "MoveResult",
    "active_work",
    "move_session",
    "owned_sessions",
]

#: Session kinds a person can move (the conversation list's rows).
MOVABLE_KINDS = frozenset({"conversation", "branch"})
#: Session kinds owned by their parent conversation, which move with it.
OWNED_KINDS = frozenset({"agent", "lookup"})
#: Typed reason when a moved session's tool-output folder could not be relocated.
TOOL_OUTPUT_RELOCATE_FAILED_REASON = "session_move_tool_output_relocate_failed"


@dataclass
class MoveResult:
    """What one move changed."""

    session: Any
    moved: list[Any] = field(default_factory=list)
    from_workspace_id: str = ""
    relocated_tool_output: list[str] = field(default_factory=list)


def owned_sessions(app: Any, sid: str) -> list[Any]:
    """Sessions owned by ``sid`` (agent and side sessions), transitively, oldest first.

    The walk stops at user branches and conversations: those are independent rows.
    """
    children = app.state.sessions.child_ids_by_parent()
    owned: list[Any] = []
    seen = {sid}
    frontier = [sid]
    while frontier:
        parent = frontier.pop(0)
        for child_id in reversed(children.get(parent, [])):
            if child_id in seen:
                continue
            seen.add(child_id)
            child = app.state.sessions.get(child_id)
            if child is None or session_kind(child) not in OWNED_KINDS:
                continue
            owned.append(child)
            frontier.append(child_id)
    return owned


def active_work(app: Any, sessions: list[Any]) -> dict[str, list[str]]:
    """``{session id: [active task handles]}`` for each session with work in flight.

    Mirrors the session-delete guard: a running turn (the turn runner's slot) or an
    active task tree (delegations, background scripts).
    """
    busy: dict[str, list[str]] = {}
    runner = getattr(app.state, "turn_runner", None)
    history = getattr(app.state, "task_history", None)
    for session in sessions:
        handles = (
            sorted(history.tree_handles(session.id, active_only=True))
            if history is not None
            else []
        )
        if handles or (runner is not None and runner.busy(session.id)):
            busy[session.id] = handles
    return busy


def _workspace_root(app: Any, workspace_id: str) -> str:
    workspace = app.state.workspaces.get(workspace_id) if workspace_id else None
    return str(getattr(workspace, "root_path", "") or "")


def _relocate_tool_output(sid: str, old_root: str, new_root: str) -> bool:
    """Move ``sid``'s tool-output folder between workspace states; ``True`` if moved."""
    from clio_agent.tools.servers.shell_spill_store import spill_directory  # noqa: PLC0415

    if not old_root or not new_root:
        return False
    source = spill_directory(old_root, session_id=sid)
    target = spill_directory(new_root, session_id=sid)
    if source.resolve() == target.resolve() or not source.is_dir():
        return False
    target.mkdir(parents=True, exist_ok=True)
    for entry in sorted(source.rglob("*")):
        if not entry.is_file():
            continue
        destination = target / entry.relative_to(source)
        if destination.exists():
            continue  # content-addressed snapshots; an existing file is the same bytes
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(win_extended_path(entry), win_extended_path(destination))
    shutil.rmtree(win_extended_path(source), ignore_errors=True)
    return True


def move_session(
    app: Any, session: Any, target_workspace_id: str, owned: list[Any] | None = None
) -> MoveResult:
    """Re-home ``session`` and the sessions it owns in ``target_workspace_id``.

    The caller has checked the session kind, the target workspace and active work,
    and holds each moved session's turn slot (``owned`` names the sessions it holds;
    ``None`` reads them). Runs off the event loop.
    """
    from_workspace_id = session.workspace_id
    moved_at = datetime.now(timezone.utc).isoformat()
    result = MoveResult(session=session, from_workspace_id=from_workspace_id)
    old_root = _workspace_root(app, from_workspace_id)
    new_root = _workspace_root(app, target_workspace_id)
    owned = owned_sessions(app, session.id) if owned is None else owned
    for row in [session, *owned]:
        prior = row.workspace_id
        if prior == target_workspace_id:
            continue
        updated = app.state.sessions.update(
            row.id,
            workspace_id=target_workspace_id,
            metadata_patch={
                "last_workspace_move": {
                    "from_workspace_id": prior,
                    "to_workspace_id": target_workspace_id,
                    "moved_at": moved_at,
                    **({} if row.id == session.id else {"with_session_id": session.id}),
                }
            },
        )
        if updated is None:
            continue
        result.moved.append(updated)
        if updated.id == session.id:
            result.session = updated
        prior_root = old_root if prior == from_workspace_id else _workspace_root(app, prior)
        try:
            if _relocate_tool_output(row.id, prior_root, new_root):
                result.relocated_tool_output.append(row.id)
        except OSError as exc:
            # The row already moved; its earlier spills stay readable at their old path.
            logger.warning(
                "session move tool output not relocated reason=%s session=%s error=%r",
                TOOL_OUTPUT_RELOCATE_FAILED_REASON,
                row.id,
                exc,
            )
    trace.event(
        "SESSION",
        "session moved session=%s from=%s to=%s sessions=%d tool_output=%d",
        session.id,
        from_workspace_id,
        target_workspace_id,
        len(result.moved),
        len(result.relocated_tool_output),
    )
    return result
