"""Desktop-owned Clio Coder terminals; GACT owns preparation and checked hand-back."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from clio_agent.gact.clio_coder_route import resolve_route
from clio_agent.gact.clio_coder_tool import _shell_argv, autonomy_ceiling, result_row
from clio_agent.paths import workspace_state_dir
from clio_agent.runtime.clio_coder import ClioCoderInstall, DelegationRequest, RunOptions
from clio_agent.runtime.clio_coder.interactive import (
    InteractiveSession,
    finish_interactive_session,
    prepare_interactive_session,
)
from clio_agent.runtime.clio_coder.launch import close_egress
from clio_agent.runtime.clio_coder.runs import new_run_id
from clio_agent.runtime.clio_coder.workspace import remove_worktree


class TerminalRequest(BaseModel):
    """A person's coding assignment, with an optional acceptance command."""

    task: str = Field(min_length=1, max_length=20_000)
    verify_command: str = Field(default="", max_length=4000)


class TerminalExit(BaseModel):
    """The native terminal's actual exit status, never an inferred success."""

    exit_code: int | None


class TerminalOwner:
    """Own preparation and hand-back beyond any one HTTP request's lifetime."""

    def __init__(self) -> None:
        self.pending: dict[tuple[str, str], InteractiveSession] = {}
        self.finishing: dict[tuple[str, str], asyncio.Task[dict[str, Any]]] = {}
        self.preparing: set[asyncio.Task[InteractiveSession]] = set()
        self.preparing_sessions: dict[asyncio.Task[InteractiveSession], tuple[str, Path]] = {}
        self.closed = False

    async def prepare(
        self, sid: str, request: DelegationRequest, autonomy: str
    ) -> InteractiveSession:
        if self.closed:
            raise HTTPException(503, "Clio Coder terminal owner is stopping")
        if len(self.pending) + len(self.preparing) >= 32:
            raise HTTPException(409, "Return an open Clio Coder terminal before opening another")
        while len(self.finishing) >= 128:
            finished = next((key for key, task in self.finishing.items() if task.done()), None)
            if finished is None:
                break
            del self.finishing[finished]
        task = asyncio.create_task(
            asyncio.to_thread(prepare_interactive_session, request, autonomy=autonomy)
        )
        self.preparing.add(task)
        self.preparing_sessions[task] = sid, request.repo
        try:
            interactive = await asyncio.shield(task)
            if self.closed:
                close_egress(interactive.confinement)
                await asyncio.to_thread(remove_worktree, interactive.workspace)
                raise HTTPException(503, "Clio Coder terminal owner is stopping")
            self.pending[sid, interactive.run_id] = interactive
            return interactive
        except asyncio.CancelledError:
            interactive = await task
            close_egress(interactive.confinement)
            await asyncio.to_thread(remove_worktree, interactive.workspace)
            raise
        finally:
            self.preparing.discard(task)
            self.preparing_sessions.pop(task, None)

    def require_idle(self, sid: str) -> None:
        if any(value[0] == sid for value in self.preparing_sessions.values()) or any(
            key[0] == sid for key in self.pending
        ):
            raise HTTPException(409, "Close and return Clio Coder terminals before deleting")

    def require_workspace_idle(self, root: Path) -> None:
        if any(value[1] == root for value in self.preparing_sessions.values()) or any(
            interactive.workspace.repo == root for interactive in self.pending.values()
        ):
            raise HTTPException(409, "Close and return Clio Coder terminals before deleting")

    async def finish(self, sid: str, run_id: str, exit_code: int | None) -> dict[str, Any]:
        key = sid, run_id
        task = self.finishing.get(key)
        if task is None:
            interactive = self.pending.get(key)
            if interactive is None:
                raise HTTPException(404, "Clio Coder terminal not found in this conversation")
            task = asyncio.create_task(self._finish(key, interactive, exit_code))
            self.finishing[key] = task
        return await asyncio.shield(task)

    async def _finish(
        self, key: tuple[str, str], interactive: InteractiveSession, exit_code: int | None
    ) -> dict[str, Any]:
        result = await finish_interactive_session(interactive, exit_code=exit_code)
        self.pending.pop(key, None)
        return result_row(result, interactive.task, [])

    async def close(self) -> None:
        self.closed = True
        results = await asyncio.gather(
            *self.preparing, *self.finishing.values(), return_exceptions=True
        )
        for interactive in self.pending.values():
            close_egress(interactive.confinement)
        self.pending.clear()
        self.finishing.clear()
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            raise BaseExceptionGroup("Clio Coder terminal shutdown failures", failures)


def _session_root(app: FastAPI, sid: str) -> tuple[Any, Path]:
    session = app.state.sessions.get(sid)
    if session is None:
        raise HTTPException(404, "Conversation not found")
    workspace = app.state.workspaces.get(session.workspace_id)
    if workspace is None or not workspace.root_path:
        raise HTTPException(409, "Conversation workspace unavailable")
    root = Path(workspace.root_path)
    if not (root / ".git").exists():
        raise HTTPException(409, "Clio Coder needs a git workspace")
    return session, root


def guard_terminal_session_delete(app: FastAPI, deps: Any, sid: str, workspace_id: str) -> None:
    """Keep the ordinary delete permission gate and the terminal owner together."""
    terminals = getattr(app.state, "clio_coder_terminals", None)
    if terminals is not None:
        terminals.require_idle(sid)
    deps.guard_direct_destructive_action(
        app,
        session_id=sid,
        workspace_id=workspace_id,
        tool_name="gact.session.delete",
        args={"session_id": sid},
        summary=f"delete session {sid}",
        reason="user_requested_session_delete",
    )


def register_clio_coder_terminal_routes(app: FastAPI) -> None:
    """Keep launch credentials in memory only, scoped to the conversation that prepared them."""
    owner = TerminalOwner()
    app.state.clio_coder_terminals = owner

    @app.post("/v1/sessions/{sid}/clio-coder/terminal")
    async def prepare(sid: str, body: TerminalRequest) -> dict[str, Any]:
        if not body.task.strip():
            raise HTTPException(422, "Provide a coding assignment")
        install = ClioCoderInstall.resolve()
        await asyncio.to_thread(install.require_supported)
        flags = await asyncio.to_thread(install.help_flags)
        if "--session-file" not in flags:
            raise HTTPException(409, "Clio Coder must support --session-file for Desktop hand-back")
        session, root = _session_root(app, sid)
        ceiling = autonomy_ceiling(session)
        if ceiling == "read-only":
            raise HTTPException(409, "Interactive delegation is unavailable in plan mode")
        route = resolve_route(app, session)
        run_id = new_run_id()
        request = DelegationRequest(
            task=body.task,
            repo=root,
            state_root=workspace_state_dir(root) / "clio-coder" / "terminals" / run_id,
            run_id=run_id,
            target=route.target,
            home_template=route.home_template,
            child_env=route.env,
            install=install,
            options=RunOptions(autonomy=ceiling),
            verify_command=_shell_argv(body.verify_command)
            if body.verify_command.strip()
            else None,
        )
        interactive = await owner.prepare(sid, request, ceiling)
        return {
            "run_id": run_id,
            "launch": interactive.launch.terminal_open_args(),
            "brief": interactive.brief(),
            "branch": interactive.workspace.branch,
        }

    @app.post("/v1/sessions/{sid}/clio-coder/terminal/{run_id}/exit")
    async def finish(sid: str, run_id: str, body: TerminalExit) -> dict[str, Any]:
        return await owner.finish(sid, run_id, body.exit_code)
