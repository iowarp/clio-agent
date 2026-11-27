"""A direct server connection kept alive across calls, against actual SDK, stdio and Core.

Before the pool, every call to a direct (stdio) server started the server again: a 7 s
cold start per page action for a ``uvx`` server, and a stateful server (a scene, a
notebook kernel) lost its state between calls. Calls through one fleet proxy, parallel
ones included, now share one connection, whether or not an executor holds it: one
process per fleet, until the fleet closes.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psutil
import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from mcp.shared.exceptions import MCPError

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import QuestionReply, TaskSnapshot
from clio_agent.gact import context
from clio_agent.gact import external_task_connections as connections
from clio_agent.gact.external_tasks import SERVER_STOPPED_UNDER_CALL, ExternalTasks
from clio_agent.gact.questions import QuestionCommands
from clio_agent.gact.task_operations import TaskOperations
from clio_agent.gact.workspaces import WorkspaceStore
from clio_agent.tools import shared_direct_connections
from clio_agent.tools.task_call_context import TASK_CALL, TaskCall
from tests.test_arc.test_conversation_owner_contracts import create_environment

SERVER = """
import asyncio
import json
import os
import sys
from pathlib import Path
import psutil
from fastmcp import Context, FastMCP

with Path(sys.argv[1]).open("a") as starts:
    starts.write(json.dumps({"pid": os.getpid(), "created_at": psutil.Process().create_time()}) + "\\n")
server = FastMCP("pooled-direct")
state: dict[str, str] = {}

@server.tool
def pid() -> dict[str, int]:
    return {"pid": os.getpid()}

@server.tool
def remember(value: str) -> dict[str, str]:
    state["value"] = value
    return state

@server.tool
def recall() -> dict[str, str]:
    return state or {"value": "nothing"}

@server.tool
async def nap(seconds: float) -> dict[str, int]:
    await asyncio.sleep(seconds)
    return {"pid": os.getpid()}

@server.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
async def peek(seconds: float) -> dict[str, int]:
    await asyncio.sleep(seconds)
    return {"pid": os.getpid()}

@server.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
def closed() -> dict[str, int]:
    raise RuntimeError("Connection closed")

@server.tool
async def choose(tag: str, ctx: Context) -> dict[str, str]:
    response = await ctx.elicit("Choose a value for " + tag, response_type=str)
    return {"tag": tag, "value": response.data if response.action == "accept" else response.action}

server.run(transport="stdio", show_banner=False)
"""


class _Env(SimpleNamespace):
    app: Any
    root: TaskSnapshot
    external: ExternalTasks
    starts: Path
    script: Path
    tmp: Path

    def client(self, timeout: float | None = 20) -> Client:
        transport = StdioTransport(
            sys.executable, ["-B", str(self.script), str(self.starts)], cwd=str(self.tmp)
        )
        return Client(transport, mode="legacy", timeout=timeout)

    async def call(
        self, tool: str, args: dict[str, Any], owner: Any, tag: str, timeout: float | None = 20
    ) -> Any:
        app_token = context.set_app(self.app)
        session_token = context.set_session_id(self.root.owner.session_id)
        invocation_token = TASK_CALL.set(TaskCall(invocation_id="pool-" + tag))
        try:
            return await self.external.call(
                "pooled", self.client(timeout), tool, args, auth_scope="scope", pool_owner=owner
            )
        finally:
            TASK_CALL.reset(invocation_token)
            context.reset(session_token)
            context.reset(app_token)

    def started(self) -> list[dict[str, Any]]:
        if not self.starts.exists():
            return []
        return [json.loads(line) for line in self.starts.read_text().splitlines()]

    async def settle(self) -> None:
        while self.external._retiring:
            await asyncio.gather(*tuple(self.external._retiring))


def _gone(process: dict[str, Any]) -> bool:
    try:
        return psutil.Process(process["pid"]).create_time() != process["created_at"]
    except psutil.NoSuchProcess:
        return True


@pytest.fixture
async def env(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[_Env]:
    app, conversations, root = await create_environment(arc, tmp_path)
    app.state.task_recovery = conversations.recovery
    app.state.questions = QuestionCommands(conversations.history)

    async def cancel(task: TaskSnapshot, caller: Any) -> dict[str, Any]:
        if task.kind == "Question":
            closed = await asyncio.to_thread(app.state.questions.cancel, task.handle)
            return {"handle": closed.handle, "status": closed.status}
        return await conversations.cancel(caller, task.handle)

    app.state.task_operations = TaskOperations(conversations.history, cancel)
    monkeypatch.setattr("clio_agent.gact.agent_elicitation._enabled", lambda: False)

    def setup_workspace() -> None:
        app.state.workspaces = WorkspaceStore(path=tmp_path / "workspaces.json")
        workspace = app.state.workspaces.create(name="Pool", root_path=str(tmp_path))
        session = app.state.sessions.get(root.owner.session_id)
        assert session is not None
        session.workspace_id = workspace.id
        app.state.sessions._persist(session)

    await asyncio.to_thread(setup_workspace)
    script = tmp_path / "pooled_server.py"
    script.write_text(SERVER, encoding="utf-8")
    starts = tmp_path / "starts.jsonl"
    (tmp_path / "owned-processes.json").write_text(
        json.dumps({"purpose": "Direct pool contract", "script": str(script), "ports": []}),
        encoding="utf-8",
    )
    environment = _Env(
        app=app,
        root=root,
        external=ExternalTasks(app),
        starts=starts,
        script=script,
        tmp=tmp_path,
    )
    try:
        yield environment
    finally:
        await environment.external.close()
        await conversations.shutdown()
        await conversations.recovery.drain()
        assert all(_gone(process) for process in environment.started()), (
            "An owned stdio server survived shutdown"
        )


@pytest.mark.asyncio
@pytest.mark.timeout(120)  # Real stdio servers start and must retire.
async def test_calls_share_the_held_connection_instead_of_starting_the_server(env: _Env) -> None:
    """SABOTAGE: make ``connections._lease`` always open a connection; each call starts one."""
    proxy = object()
    client, release = await env.external.hold("pooled", "scope", proxy, env.client())
    assert "pid" in {tool.name for tool in await client.list_tools()}
    [opened] = env.started()
    held = opened["pid"]

    first = await env.call("pid", {}, proxy, "first")
    await env.call("remember", {"value": "scene"}, proxy, "remember")
    recalled = await env.call("recall", {}, proxy, "recall")
    await env.settle()

    assert first.structured_content == {"pid": held}
    assert recalled.structured_content == {"value": "scene"}, "Server state survives calls"
    assert [start["pid"] for start in env.started()] == [held]
    assert len(env.external.connections) == 1

    await release()
    await env.settle()
    assert len(env.external.connections) == 1, "A working connection outlives its holder"
    after = await env.call("recall", {}, proxy, "after-release")
    assert after.structured_content == {"value": "scene"}
    assert [start["pid"] for start in env.started()] == [held]

    owners = {f"in-process:{id(proxy):x}"}
    assert await connections.retire_owned(env.external, owners, "/w", "idle_ttl") == 1
    await env.settle()
    assert not env.external.connections, "The fleet's close retires it"
    assert all(_gone(process) for process in env.started())


@pytest.mark.asyncio
@pytest.mark.timeout(120)  # Real stdio servers start and must retire.
async def test_a_start_whose_holder_stopped_waiting_is_joined_not_repeated(env: _Env) -> None:
    """Dry 14: a connect's wait ended while its server was still starting; the shielded
    start went on, and the next connect started a second server beside it, so one call
    opened a scene in one process and the next found none in the other.

    SABOTAGE: drop the in-flight join in ``_pooled`` -> a second start; or give the
    abandoned start a holder at creation -> ``holders == 2``.
    """
    slow = env.tmp / "slow_server.py"
    slow.write_text("import time\ntime.sleep(2)\n" + SERVER, encoding="utf-8")

    def client() -> Client:
        transport = StdioTransport(
            sys.executable, ["-B", str(slow), str(env.starts)], cwd=str(env.tmp)
        )
        return Client(transport, mode="legacy", timeout=20)

    proxy = object()
    abandoned = asyncio.create_task(env.external.hold("pooled", "scope", proxy, client()))
    while not env.external._pool_opening:
        await asyncio.sleep(0.01)
    abandoned.cancel()
    with pytest.raises(asyncio.CancelledError):
        await abandoned

    _client, release = await env.external.hold("pooled", "scope", proxy, client())
    [start] = env.started()
    [host] = env.external.connections.values()
    assert host.holders == 1, "The abandoned start holds nothing"
    reply = await env.call("pid", {}, proxy, "after-join")
    await env.settle()
    assert reply.structured_content == {"pid": start["pid"]}
    assert [s["pid"] for s in env.started()] == [start["pid"]]
    await release()


@pytest.mark.asyncio
@pytest.mark.timeout(120)  # Real stdio servers start and must retire.
async def test_a_reader_joins_the_starting_server_instead_of_starting_another(env: _Env) -> None:
    """Dry 14: a connector check started its own server of a namespace while the
    conversation's was still starting, so two servers of it ran at once.

    SABOTAGE: drop the in-flight join in ``live_client`` -> ``None`` (the caller then
    starts a second server); drop the owner filter -> another fleet's server answers.
    """
    slow = env.tmp / "slow_server.py"
    slow.write_text("import time\ntime.sleep(2)\n" + SERVER, encoding="utf-8")
    transport = StdioTransport(sys.executable, ["-B", str(slow), str(env.starts)], cwd=str(env.tmp))
    proxy = object()
    holding = asyncio.create_task(
        env.external.hold("pooled", "scope", proxy, Client(transport, mode="legacy", timeout=20))
    )
    while not env.external._pool_opening:
        await asyncio.sleep(0.01)

    assert await env.external.live_client("pooled", [object()]) is None, "Another fleet's"
    reader = await env.external.live_client("pooled", [proxy])
    assert reader is not None
    assert {tool.name for tool in await reader.list_tools()} >= {"pid"}
    _client, release = await holding
    assert len(env.started()) == 1
    await release()


@pytest.mark.asyncio
@pytest.mark.timeout(120)  # Real stdio servers start and must retire.
async def test_parallel_calls_reach_the_same_process(env: _Env) -> None:
    proxy = object()
    _client, release = await env.external.hold("pooled", "scope", proxy, env.client())

    busy = asyncio.create_task(env.call("nap", {"seconds": 1.5}, proxy, "busy"))
    while not any(host.rpcs for host in env.external.connections.values()):
        await asyncio.sleep(0.01)
    beside = await env.call("pid", {}, proxy, "beside")
    assert not busy.done(), "The parallel call did not wait for the busy one"
    napped = await busy
    await env.settle()

    [start] = env.started()
    assert napped.structured_content == beside.structured_content == {"pid": start["pid"]}
    assert len(env.external.connections) == 1 and not _gone(start)
    await release()


@pytest.mark.asyncio
@pytest.mark.timeout(120)  # Real stdio servers start and must retire.
async def test_unheld_parallel_calls_open_one_connection_that_stays(env: _Env) -> None:
    """Without an executor holding it, simultaneous first calls still start one server,
    and it stays up for the fleet's life."""
    proxy = object()
    replies = await asyncio.gather(
        *(env.call("nap", {"seconds": 0.5}, proxy, f"cold-{index}") for index in range(3))
    )
    await env.settle()

    [start] = env.started()
    assert {reply.structured_content["pid"] for reply in replies} == {start["pid"]}
    assert len(env.external.connections) == 1 and not _gone(start), "Unheld, it stays"


@pytest.mark.asyncio
@pytest.mark.timeout(120)  # Real stdio servers start and must retire.
async def test_unheld_sequential_calls_keep_the_server_and_its_state(env: _Env) -> None:
    """A server that joined mid-turn (no executor connected or held it) keeps one process
    across calls: a scene opened by one call is there for the next.

    SABOTAGE: in ``retire_connection`` keep a pooled host only while ``host.holders`` is
    set (the 001b45e71 rule); each call then starts the server and ``recall`` finds
    nothing.
    """
    proxy = object()
    await env.call("remember", {"value": "scene"}, proxy, "open")
    await env.settle()
    recalled = await env.call("recall", {}, proxy, "use")
    await env.settle()

    assert recalled.structured_content == {"value": "scene"}
    assert len(env.started()) == 1, "One start per fleet, not one per call"
    owners = {f"in-process:{id(proxy):x}"}
    assert await connections.retire_owned(env.external, owners, "/w", "idle_ttl") == 1
    await env.settle()
    assert not env.external.connections and all(_gone(p) for p in env.started())


async def _in_flight(env: _Env, tool: str, proxy: Any, tag: str) -> asyncio.Task[Any]:
    """Start ``tool`` (sleeping 60 s, no request timeout) once its request is on the wire."""
    call = asyncio.create_task(env.call(tool, {"seconds": 60}, proxy, tag, timeout=None))
    for _ in range(400):
        if any(host.rpcs for host in env.external.connections.values()):
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.5)  # the server is now inside the sleep
    assert not call.done()
    return call


@pytest.mark.asyncio
@pytest.mark.timeout(120)  # Real stdio servers start and must retire.
async def test_a_read_in_flight_when_its_server_stops_ends_typed_and_retry_safe(
    env: _Env,
) -> None:
    """Take 1p: two exec_read calls on a superseded relay waited 23+ minutes. A busy
    connection was only marked; the server it waited on kept running.

    SABOTAGE: in ``_retire_stale`` only mark a busy host (drop the ``client.close()``);
    the read then waits out its 60 s nap and this test times out.
    """
    proxy = object()
    call = await _in_flight(env, "peek", proxy, "read")
    [start] = env.started()
    owners = {f"in-process:{id(proxy):x}"}

    assert await connections.retire_owned(env.external, owners, "/w", "superseded") == 1
    assert _gone(start), "The stopped server's process is gone before its replacement starts"
    # Promptly: the server's 60 s sleep is not waited out.
    reply = await asyncio.wait_for(call, 5)
    await env.settle()

    assert reply.is_error is True
    body = json.loads(reply.content[0].text)
    assert body["acceptance"] == "server_stopped" and body["retry_safe"] is True
    assert body["error"].startswith(connections.EXTERNAL_MCP_CONNECTION_FLEET_CLOSED + ":")
    assert not env.external.connections


@pytest.mark.asyncio
@pytest.mark.timeout(120)  # Real stdio servers start and must retire.
async def test_a_write_in_flight_when_its_server_stops_ends_uncertain_with_its_reason(
    env: _Env,
) -> None:
    """A call that may have changed something is not retry-safe, but it ends now, typed."""
    proxy = object()
    call = await _in_flight(env, "nap", proxy, "write")
    owners = {f"in-process:{id(proxy):x}"}

    assert await connections.retire_owned(env.external, owners, "/w", "superseded") == 1
    reply = await asyncio.wait_for(call, 5)  # not the server's 60 s sleep
    await env.settle()

    body = reply.structured_content
    assert body["acceptance"] == "uncertain" and body["retry_safe"] is False
    assert body["error"].startswith(connections.EXTERNAL_MCP_CONNECTION_FLEET_CLOSED + ":")
    assert all(_gone(p) for p in env.started())


@pytest.mark.asyncio
@pytest.mark.timeout(120)  # Real stdio servers start and must retire.
async def test_an_error_reply_from_a_server_stopped_under_the_call_is_typed(
    env: _Env,
) -> None:
    """Take 1p, proxy route: the fleet proxy answers a call whose backend it lost with an
    error reply ("Connection closed"), not a transport failure. When the server was being
    stopped under the call, that reply is the typed, retry-safe ``server_stopped``.

    SABOTAGE: drop the ``stopped_under()`` check in ``ExternalTasks._call``; the first
    reply is then the plain error text.
    """

    async def call(stopped: bool) -> Any:
        app_token = context.set_app(env.app)
        session_token = context.set_session_id(env.root.owner.session_id)
        invocation_token = TASK_CALL.set(TaskCall(invocation_id=f"pool-cut-{stopped}"))
        try:
            return await env.external.call(
                "pooled",
                env.client(),
                "closed",
                {},
                auth_scope="scope",
                pool_owner=proxy,
                stopped_under=lambda: stopped,
            )
        finally:
            TASK_CALL.reset(invocation_token)
            context.reset(session_token)
            context.reset(app_token)

    proxy = object()
    cut = await call(True)
    plain = await call(False)
    await env.settle()

    assert cut.is_error is True
    body = json.loads(cut.content[0].text)
    assert body["acceptance"] == "server_stopped" and body["retry_safe"] is True
    assert body["error"].startswith(SERVER_STOPPED_UNDER_CALL + ":")
    assert "Connection closed" in body["error"]
    # No stop under it: the server's own error, unchanged.
    assert plain.is_error is True and "Connection closed" in plain.content[0].text
    assert "server_stopped" not in plain.content[0].text


@pytest.mark.asyncio
@pytest.mark.timeout(120)  # Real stdio servers start and must retire.
async def test_a_silent_call_is_reported_on_its_row_and_keeps_waiting(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No reply for a window: a visible notice, growing windows, no deadline.

    SABOTAGE: await ``rpc`` directly in ``ExternalTasks._call``; no notice arrives.
    """
    from clio_agent.gact import external_call_stall

    monkeypatch.setattr(external_call_stall, "mcp_no_progress_s", lambda: 0.4)
    notices: list[tuple[float, str | None]] = []

    async def progress(value: float, total: float | None, message: str | None) -> None:
        notices.append((value, message))

    proxy = object()
    app_token = context.set_app(env.app)
    session_token = context.set_session_id(env.root.owner.session_id)
    invocation_token = TASK_CALL.set(TaskCall(invocation_id="pool-stall"))
    try:
        reply = await env.external.call(
            "pooled",
            env.client(),
            "peek",
            {"seconds": 2.0},
            auth_scope="scope",
            pool_owner=proxy,
            progress_handler=progress,
        )
    finally:
        TASK_CALL.reset(invocation_token)
        context.reset(session_token)
        context.reset(app_token)
    await env.settle()

    assert reply.structured_content == {"pid": env.started()[0]["pid"]}, "It still answered"
    messages = [message for _, message in notices]
    assert messages and all(
        m is not None and m.startswith("No reply from pooled peek for ") for m in messages
    )
    assert "Stop the turn to cancel." in messages[0]
    # 0.4 s, then 0.8 s, 1.6 s ...: fewer notices than fixed 0.4 s windows would give.
    assert len(messages) <= 3


@pytest.mark.asyncio
@pytest.mark.timeout(120)  # Real stdio servers start and must retire.
async def test_an_inline_input_belongs_to_the_call_waiting_on_the_connection(env: _Env) -> None:
    """Each call's elicitation is attributed to that call, not an earlier one.

    SABOTAGE: keep finished calls in ``host.leases`` (skip ``connections.release``) and
    the second input is refused as ambiguous.
    """
    proxy = object()
    _client, release = await env.external.hold("pooled", "scope", proxy, env.client())
    questions: asyncio.Queue[TaskSnapshot] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    env.app.state.questions.on_change = lambda task: (
        loop.call_soon_threadsafe(questions.put_nowait, task)
        if task.raw_status == "pending"
        else None
    )
    history = env.app.state.task_history
    for tag in ("first", "second"):
        call = asyncio.create_task(env.call("choose", {"tag": tag}, proxy, tag))
        question = await asyncio.wait_for(questions.get(), 30)
        assert question.owner.parent_handle is not None
        invocation = await asyncio.to_thread(history.task_snapshot, question.owner.parent_handle)
        assert invocation is not None and invocation.invocation_id == "pool-" + tag
        await asyncio.to_thread(
            env.app.state.questions.answer,
            question.handle,
            QuestionReply(metadata={"value": "answer-" + tag}, responder_human="human"),
        )
        result = await asyncio.wait_for(call, 30)
        assert result.structured_content == {"tag": tag, "value": "answer-" + tag}
        while not questions.empty():  # the routing annotation's revision of the same question
            assert questions.get_nowait().handle == question.handle
    await env.settle()
    assert len(env.started()) == 1
    await release()


@pytest.mark.asyncio
@pytest.mark.timeout(120)  # Real stdio servers start and must retire.
async def test_an_inline_input_with_two_plain_calls_waiting_is_refused(env: _Env) -> None:
    """Stdio carries no link from a server request to its call: with two calls waiting on
    plain replies, an elicitation could be either one's, so it is refused, not guessed."""
    proxy = object()
    _client, release = await env.external.hold("pooled", "scope", proxy, env.client())
    asked: list[TaskSnapshot] = []
    env.app.state.questions.on_change = asked.append

    busy = asyncio.create_task(env.call("nap", {"seconds": 1.5}, proxy, "busy"))
    while not any(host.rpcs for host in env.external.connections.values()):
        await asyncio.sleep(0.01)
    chosen = await asyncio.wait_for(env.call("choose", {"tag": "both"}, proxy, "both"), 30)
    await busy
    await env.settle()

    assert chosen.is_error and not asked
    assert len(env.started()) == 1
    await release()


@pytest.mark.asyncio
@pytest.mark.timeout(120)  # Real stdio servers start and must retire.
async def test_a_fleet_drop_closes_the_held_connection(env: _Env) -> None:
    proxy, other = object(), object()
    _client, release = await env.external.hold("pooled", "scope", proxy, env.client())
    await env.call("pid", {}, proxy, "before")
    owners = {f"in-process:{id(proxy):x}"}

    assert (
        await connections.retire_owned(env.external, {f"in-process:{id(other):x}"}, "/w", "x") == 0
    )
    assert await connections.retire_owned(env.external, owners, "/w", "idle_ttl") == 1
    await env.settle()

    assert not env.external.connections
    assert all(_gone(process) for process in env.started())
    await release()  # the executor's own close afterwards is harmless


def _host(**fields: Any) -> Any:
    client = SimpleNamespace(is_connected=lambda: True, unconfirmed_adoptions=())
    base = {
        "client": client,
        "pool": ("s", "scope", "in-process:1"),
        "closing": False,
        "superseded": False,
        "broken": False,
        "leases": {},
        "settling": [],
        "calls": {},
        "rpcs": set(),
        "holders": 0,
    }
    return SimpleNamespace(**(base | fields))


@pytest.mark.asyncio
async def test_a_call_shares_a_working_connection_and_prefers_the_held_one() -> None:
    owner = object()
    pool = connections.pool_key("s", "scope", owner)
    busy = _host(pool=pool, leases={object(): None}, rpcs={object()})
    held = _host(pool=pool, holders=1)
    unusable = [
        _host(pool=pool, holders=2, broken=True),
        _host(pool=pool, holders=2, superseded=True),
        _host(pool=pool, holders=2, closing=True),
        _host(pool=("s", "other", pool[2]), holders=2),
    ]
    tasks = SimpleNamespace(
        connections=dict(enumerate([busy, held, *unusable])), _pool_locks={}, _pool_opening={}
    )
    token = object()

    assert await connections._lease(tasks, "s", None, "scope", owner, token) is held
    held.broken = True
    assert await connections._lease(tasks, "s", None, "scope", owner, object()) is busy, (
        "A busy connection is shared, not skipped"
    )
    held.broken = False
    assert token in held.leases

    submission = SimpleNamespace(handle="plain")
    held.leases[token] = submission
    connections.release(held, object())  # someone else's token: not released
    assert held.leases[token] is submission
    assert connections.waiting_inline(held) == [submission]
    held.calls = {"t1": SimpleNamespace(submission=SimpleNamespace(handle="plain"))}
    assert connections.waiting_inline(held) == [], "A call that became a task names itself"
    connections.release(held, token)
    assert token not in held.leases and held.settling == [submission]


def test_only_a_transport_failure_stops_a_connection_being_shared() -> None:
    host = _host()
    connections.note_failure(host, MCPError(-32602, "bad"))
    assert host.broken is False
    connections.note_failure(host, RuntimeError("Connection closed"))
    assert host.broken is True


@pytest.mark.asyncio
async def test_the_holder_is_only_offered_while_its_owner_is_open() -> None:
    open_ = True

    async def hold(*_: Any) -> Any:
        raise AssertionError("not called")

    remove = shared_direct_connections.register(asyncio.get_running_loop(), hold, lambda: open_)
    try:
        assert shared_direct_connections.holder() is hold
        open_ = False
        assert shared_direct_connections.holder() is None
    finally:
        remove()
    open_ = True
    assert shared_direct_connections.holder() is None


@pytest.mark.asyncio
async def test_an_idle_reap_keeps_a_stdio_server_that_still_drives_a_task() -> None:
    """Trial QA a18: the reaper closed an idle fleet while relay's install task waited on
    its person. Stopping the stdio server failed the pending ``tasks/update`` (Connection
    closed), and the answers given to the task's questions never reached relay.

    SABOTAGE: drop the idle-reap branch in ``_retire_stale``; the client closes at once.
    """
    closed: list[bool] = []

    class _Client:
        async def close(self) -> None:
            closed.append(True)

    driver: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    host = SimpleNamespace(
        rpcs=set(),
        calls={"task-1": object()},
        drivers={"task-1": driver},
        cache_key=("relay", "", ""),
        backend=SimpleNamespace(transport="stdio"),
        client=_Client(),
        closing=False,
        superseded=False,
        stop_reason="",
    )
    reason = connections.EXTERNAL_MCP_CONNECTION_FLEET_CLOSED
    for because in ("idle_ttl", "lru_cap"):
        await connections._retire_stale(None, host, reason, fleet="/w", because=because)  # type: ignore[arg-type]
        assert host.superseded and not closed, "Kept for its task, never leased again"

    await connections._retire_stale(None, host, reason, fleet="/w", because="restart")  # type: ignore[arg-type]
    assert closed, "A restart replaces the server: it still stops now"
    driver.cancel()
