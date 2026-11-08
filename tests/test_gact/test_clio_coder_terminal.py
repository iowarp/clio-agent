"""Desktop preparation and hand-back ownership independent of request lifetime."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from clio_agent.gact.routes import clio_coder_terminal as terminal


@pytest.mark.asyncio
async def test_finish_is_single_flight_and_survives_session_deletion(monkeypatch):
    owner = terminal.TerminalOwner()
    interactive = SimpleNamespace(run_id="r1", task="fix", confinement={}, workspace=None)
    owner.pending["deleted-session", "r1"] = interactive
    gate = asyncio.Event()

    async def finish(*args, **kwargs):
        await gate.wait()
        return {"outcome": "succeeded"}

    called = AsyncMock(side_effect=finish)
    monkeypatch.setattr(terminal, "finish_interactive_session", called)
    monkeypatch.setattr(terminal, "result_row", lambda result, *_: result)
    request = asyncio.create_task(owner.finish("deleted-session", "r1", 0))
    await asyncio.sleep(0)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    retry = asyncio.create_task(owner.finish("deleted-session", "r1", 0))
    gate.set()
    assert await retry == {"outcome": "succeeded"}
    assert await owner.finish("deleted-session", "r1", 0) == {"outcome": "succeeded"}
    assert called.await_count == 1
    assert not owner.pending


@pytest.mark.asyncio
async def test_cancelled_prepare_releases_channel_and_worktree(monkeypatch):
    owner = terminal.TerminalOwner()
    interactive = SimpleNamespace(
        run_id="r1", confinement={"net_child_id": "owned"}, workspace="ws"
    )
    ready = asyncio.Event()
    release = asyncio.Event()

    removed = Mock()

    async def prepare(function, *args, **kwargs):
        if function is terminal.remove_worktree:
            removed(*args)
            return None
        ready.set()
        await release.wait()
        return interactive

    monkeypatch.setattr(terminal.asyncio, "to_thread", prepare)
    closed = Mock()
    monkeypatch.setattr(terminal, "close_egress", closed)
    request = asyncio.create_task(owner.prepare("s1", Mock(), "default"))
    await ready.wait()
    with pytest.raises(HTTPException, match="409"):
        owner.require_idle("s1")
    request.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await request
    closed.assert_called_once_with(interactive.confinement)
    removed.assert_called_once_with("ws")
    assert not owner.pending
    owner.require_idle("s1")


@pytest.mark.asyncio
async def test_owner_shutdown_releases_pending_channels(monkeypatch):
    owner = terminal.TerminalOwner()
    owner.pending["s1", "r1"] = SimpleNamespace(confinement={"net_child_id": "owned"})
    closed = Mock()
    monkeypatch.setattr(terminal, "close_egress", closed)
    await owner.close()
    closed.assert_called_once_with({"net_child_id": "owned"})
    assert not owner.pending
    with pytest.raises(HTTPException, match="503"):
        await owner.prepare("s1", Mock(), "default")


@pytest.mark.asyncio
async def test_exit_route_is_conversation_scoped_but_does_not_require_live_session(monkeypatch):
    app = FastAPI()
    terminal.register_clio_coder_terminal_routes(app)
    interactive = SimpleNamespace(run_id="r1", task="fix", confinement={}, workspace=None)
    app.state.clio_coder_terminals.pending["s1", "r1"] = interactive
    finished = AsyncMock(return_value={"outcome": "failed"})
    monkeypatch.setattr(terminal, "finish_interactive_session", finished)
    monkeypatch.setattr(terminal, "result_row", lambda result, *_: result)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        wrong_owner = await client.post(
            "/v1/sessions/s2/clio-coder/terminal/r1/exit", json={"exit_code": 0}
        )
        assert wrong_owner.status_code == 404
        for _ in range(2):
            response = await client.post(
                "/v1/sessions/s1/clio-coder/terminal/r1/exit", json={"exit_code": None}
            )
            assert response.status_code == 200
            assert response.json() == {"outcome": "failed"}
    assert finished.await_count == 1


def test_delete_guard_preserves_permission_gate_and_refuses_active_terminal(tmp_path):
    app = FastAPI()
    deps = SimpleNamespace(guard_direct_destructive_action=Mock())
    terminal.guard_terminal_session_delete(app, deps, "s1", "w1")
    deps.guard_direct_destructive_action.assert_called_once()
    owner = terminal.TerminalOwner()
    app.state.clio_coder_terminals = owner
    owner.pending["s1", "r1"] = SimpleNamespace(workspace=SimpleNamespace(repo=tmp_path))
    with pytest.raises(HTTPException, match="409"):
        terminal.guard_terminal_session_delete(app, deps, "s1", "w1")
    with pytest.raises(HTTPException, match="409"):
        owner.require_workspace_idle(tmp_path)
