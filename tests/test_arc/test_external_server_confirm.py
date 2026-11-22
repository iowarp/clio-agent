"""One ask for a server-confirmed MCP call: the server's card, flagged by CLIO.

A tool that declares ``io.iowarp/confirm`` gets the request flag when CLIO skipped its
own ask; the server then asks once. A flagged call that settles without asking (and
without saying it needed none) is recorded and stops CLIO deferring to that server; a
call whose server no longer declares the confirm is not sent at all.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import QuestionReply, TaskSnapshot
from clio_agent.gact import context
from clio_agent.gact import mcp_server_confirm as confirm
from clio_agent.gact.external_tasks import ExternalTasks
from clio_agent.gact.questions import QuestionCommands
from clio_agent.gact.task_operations import TaskOperations
from clio_agent.gact.workspaces import WorkspaceStore
from clio_agent.tools import server_confirm
from clio_agent.tools.task_call_context import TASK_CALL, TaskCall
from tests.test_arc.test_conversation_owner_contracts import create_environment

SERVER = """
from fastmcp import Context, FastMCP
from fastmcp.tools import ToolResult

server = FastMCP("confirming")
DECLARED = {"io.iowarp/confirm": {"version": 1}}

def flag(ctx: Context):
    return (ctx.request_context.meta or {}).get("io.iowarp/confirm")

@server.tool(meta=DECLARED)
async def submit(job: str, ctx: Context) -> dict:
    if flag(ctx) != "server":
        return {"flag": None}
    answer = await ctx.elicit("Submit " + job + "?", response_type=["approve", "decline"])
    return {"flag": "server", "answer": answer.data if answer.action == "accept" else answer.action}

@server.tool(meta=DECLARED)
def submit_silently(job: str, ctx: Context) -> dict:
    return {"flag": flag(ctx)}

@server.tool(meta=DECLARED)
def observe(job: str, ctx: Context) -> ToolResult:
    return ToolResult(
        structured_content={"flag": flag(ctx)}, meta={"io.iowarp/confirm": "not_needed"}
    )

@server.tool
def legacy(job: str, ctx: Context) -> dict:
    return {"flag": flag(ctx)}

server.run(transport="stdio", show_banner=False)
"""


class _Env(SimpleNamespace):
    app: Any
    root: TaskSnapshot
    external: ExternalTasks
    script: Path
    tmp: Path

    def client(self) -> Client:
        transport = StdioTransport(sys.executable, ["-B", str(self.script)], cwd=str(self.tmp))
        return Client(transport, mode="legacy", timeout=20)

    async def call(self, tool: str, owner: Any, *, flagged: bool) -> Any:
        app_token = context.set_app(self.app)
        session_token = context.set_session_id(self.root.owner.session_id)
        call_token = TASK_CALL.set(TaskCall(invocation_id="call-" + tool, server_confirm=flagged))
        try:
            return await self.external.call(
                "confirming", self.client(), tool, {"job": "sim"}, auth_scope="s", pool_owner=owner
            )
        finally:
            TASK_CALL.reset(call_token)
            context.reset(session_token)
            context.reset(app_token)

    def reasons(self) -> list[str]:
        return [row.get("reason", "") for row in self.app.state.permissions.values()]

    def untrusted(self) -> set[tuple[str, str]]:
        return getattr(self.app.state, "mcp_server_confirm_untrusted", set())


@pytest.fixture
async def env(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[_Env]:
    app, conversations, root = await create_environment(arc, tmp_path)
    app.state.task_recovery = conversations.recovery
    app.state.questions = QuestionCommands(conversations.history)
    app.state.permissions = {}

    async def cancel(task: TaskSnapshot, caller: Any) -> dict[str, Any]:
        return await conversations.cancel(caller, task.handle)

    app.state.task_operations = TaskOperations(conversations.history, cancel)
    monkeypatch.setattr("clio_agent.gact.agent_elicitation._enabled", lambda: False)

    def setup_workspace() -> None:
        app.state.workspaces = WorkspaceStore(path=tmp_path / "workspaces.json")
        workspace = app.state.workspaces.create(name="Confirm", root_path=str(tmp_path))
        session = app.state.sessions.get(root.owner.session_id)
        assert session is not None
        session.workspace_id = workspace.id
        app.state.sessions._persist(session)

    await asyncio.to_thread(setup_workspace)
    script = tmp_path / "confirming_server.py"
    script.write_text(SERVER, encoding="utf-8")
    (tmp_path / "owned-processes.json").write_text(
        json.dumps({"purpose": "Server confirm contract", "script": str(script), "ports": []}),
        encoding="utf-8",
    )
    environment = _Env(app=app, root=root, external=ExternalTasks(app), script=script, tmp=tmp_path)
    try:
        yield environment
    finally:
        await environment.external.close()
        await conversations.shutdown()
        await conversations.recovery.drain()


@pytest.mark.asyncio
@pytest.mark.timeout(120)  # A real stdio server starts and must retire.
async def test_a_flagged_call_is_confirmed_once_by_its_servers_own_card(env: _Env) -> None:
    """SABOTAGE: drop the request flag in ``ExternalTasks._call`` and no card is asked."""
    proxy = object()
    _client, release = await env.external.hold("confirming", "s", proxy, env.client())
    questions: asyncio.Queue[TaskSnapshot] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    env.app.state.questions.on_change = lambda task: (
        loop.call_soon_threadsafe(questions.put_nowait, task)
        if task.raw_status == "pending"
        else None
    )
    call = asyncio.create_task(env.call("submit", proxy, flagged=True))
    question = await asyncio.wait_for(questions.get(), 30)
    await asyncio.to_thread(
        env.app.state.questions.answer,
        question.handle,
        QuestionReply(metadata={"value": "approve"}, responder_human="human"),
    )
    result = await asyncio.wait_for(call, 30)
    assert result.structured_content == {"flag": "server", "answer": "approve"}
    assert confirm.REASON_MISSING not in env.reasons()
    assert not env.untrusted()

    unflagged = await env.call("submit", proxy, flagged=False)
    assert unflagged.structured_content == {"flag": None}  # today's behaviour, no card
    await release()


@pytest.mark.asyncio
@pytest.mark.timeout(120)  # A real stdio server starts and must retire.
async def test_a_flagged_call_settled_without_its_card_stops_the_deferral(env: _Env) -> None:
    proxy = object()
    _client, release = await env.external.hold("confirming", "s", proxy, env.client())
    observed = await env.call("observe", proxy, flagged=True)
    assert observed.structured_content == {"flag": "server"}
    assert not env.untrusted()  # it said it needed no ask

    silent = await env.call("submit_silently", proxy, flagged=True)
    assert silent.structured_content == {"flag": "server"}
    assert confirm.REASON_MISSING in env.reasons()
    assert (env.root.owner.session_id, "confirming") in env.untrusted()
    await release()


@pytest.mark.asyncio
@pytest.mark.timeout(120)  # A real stdio server starts and must retire.
async def test_a_deferred_call_its_server_no_longer_declares_is_not_sent(env: _Env) -> None:
    proxy = object()
    _client, release = await env.external.hold("confirming", "s", proxy, env.client())
    with pytest.raises(PermissionError, match=confirm.REASON_UNDECLARED):
        await env.call("legacy", proxy, flagged=True)
    assert confirm.REASON_UNDECLARED in env.reasons()
    assert (env.root.owner.session_id, "confirming") in env.untrusted()
    plain = await env.call("legacy", proxy, flagged=False)
    assert plain.structured_content == {"flag": None}
    await release()


def _app() -> Any:
    return SimpleNamespace(state=SimpleNamespace())


CONTEXT = {"kind": "external_mcp", "server": "relay", "server_confirm": True}


@pytest.mark.parametrize(
    ("context_", "mode", "policy", "deferred"),
    [
        (CONTEXT, "ask", "", True),
        (CONTEXT, "auto-edits", "", True),
        (CONTEXT, "ask", "ask", True),  # a person-made ask rule: the card is a person's ask
        (CONTEXT, "ai-review", "", False),  # the reviewer decides, not a person
        (CONTEXT, "ai-review", "ask", True),
        ({**CONTEXT, "always_ask": True}, "ask", "", False),
        ({**CONTEXT, "server_confirm": False}, "ask", "", False),
        ({"kind": "external_mcp", "server": "relay"}, "ask", "", False),
        (None, "ask", "", False),
    ],
)
def test_only_a_person_facing_ask_is_deferred(
    context_: Any, mode: str, policy: str, deferred: bool
) -> None:
    assert confirm.may_defer(_app(), "sess", context_, mode, policy) is deferred


def test_a_server_that_missed_its_confirm_is_asked_about_by_clio_again() -> None:
    app = _app()
    app.state.mcp_server_confirm_untrusted = {("sess", "relay")}
    assert not confirm.may_defer(app, "sess", CONTEXT, "ask", "")
    assert confirm.may_defer(app, "other", CONTEXT, "ask", "")


@pytest.mark.parametrize(
    ("tool", "declared"),
    [
        ({"_meta": {"io.iowarp/confirm": {"version": 1}}}, True),
        (SimpleNamespace(meta={"io.iowarp/confirm": {"version": 1}}), True),
        ({"_meta": {"io.iowarp/confirm": {"version": 2}}}, False),
        ({"_meta": {"io.iowarp/confirm": True}}, False),
        ({"_meta": {}}, False),
        (SimpleNamespace(meta=None), False),
    ],
)
def test_only_the_known_declaration_version_is_trusted(tool: Any, declared: bool) -> None:
    assert server_confirm.declares(tool) is declared


def test_the_deferred_decision_is_an_allow_that_carries_the_flag() -> None:
    decision = confirm.ServerConfirm()
    assert decision == "allow" and decision.server_confirm is True
    assert confirm.flag({"a/b": 1}) == {"a/b": 1, "io.iowarp/confirm": "server"}
    assert confirm.flag(None) == {"io.iowarp/confirm": "server"}
