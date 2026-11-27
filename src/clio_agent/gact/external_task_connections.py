"""Live external transport resources; Core remains the only task/history authority."""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import Awaitable, Callable, Collection, Iterable
from contextlib import suppress
from functools import partial
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport, SSETransport, StdioTransport
from fastmcp_tasks.client_models import ClientGetTaskResult
from mcp.shared.exceptions import MCPError

from clio_agent.arc.task_types import BackendReference
from clio_agent.gact import external_task_records, view_call_cancel, view_call_ledger
from clio_agent.gact.native_work import now
from clio_agent.runtime import net_refusals
from clio_agent.tools import fleet_close_listeners, namespace_instances, shared_direct_connections
from clio_agent.tools.mcp_runtime import enter_connection
from clio_agent.tools.task_client import AcceptedNativeCall, TaskAwareClient
from clio_agent.tools.task_transport import TaskObservationTransport

if TYPE_CHECKING:
    from clio_agent.arc.task_types import SubmissionContext
    from clio_agent.gact.external_tasks import ExternalConnection, ExternalTasks

logger = logging.getLogger(__name__)

#: A retained connection to an in-process server (a workspace fleet's MCP proxy) was
#: replaced because the call now names a different server object: the fleet that
#: owned the old proxy was reaped or rebuilt, and its process is gone with it.
EXTERNAL_MCP_CONNECTION_SUPERSEDED = "external_mcp_connection_superseded"
#: A retained connection to a proxy its fleet stopped routing to (the fleet was reaped,
#: restarted or rebuilt, or the namespace was rebound): nothing can reach it any more,
#: and it kept the proxy's server process running (CV-N2).
EXTERNAL_MCP_CONNECTION_FLEET_CLOSED = "external_mcp_connection_fleet_closed"
#: Fleet closes that only free an idle fleet (the reaper's TTL and LRU cap); nothing
#: replaces the closed fleet's servers, unlike a restart or a configuration switch.
_IDLE_REAPS = frozenset({"idle_ttl", "lru_cap"})


def holds_tasks(host: ExternalConnection) -> bool:
    """Whether ``host`` still drives an accepted task (its observation and input path)."""

    return any(not driver.done() for driver in host.drivers.values())


def _in_process_owner(configured: Client) -> str:
    """Identify the in-process server a client targets ("" for any other transport).

    A gateway proxy belongs to one workspace fleet; when that fleet is reaped, a new
    proxy (with its own server process) replaces it. Keying the retained connection
    on the proxy object keeps calls on the current fleet instead of the reaped one.
    The retained connection references its proxy, so the id cannot be reused while
    the entry exists.
    """

    transport = configured.transport
    if isinstance(transport, FastMCPTransport):
        return f"in-process:{id(transport.server):x}"
    return ""


async def connection(
    self: ExternalTasks,
    server: str,
    configured: Client,
    auth_scope: str,
    *,
    original: BackendReference | None = None,
    pool_owner: Any = None,
) -> ExternalConnection:
    """Retain a fully configured direct transport on the application lifespan.

    ``pool_owner`` keeps a direct (stdio or SSE) connection open after its call, for
    every later or parallel call through the same fleet proxy (:func:`lease_or_open`).
    Its caller leases or holds it once it is open (:func:`_pooled`), so a caller that
    stops waiting leaves no phantom lease or hold behind.
    """
    from clio_agent.gact.external_tasks import ExternalConnection

    if self._closing:
        raise ValueError("External task observation is closing")
    isolated = isinstance(configured.transport, (StdioTransport, SSETransport))
    pool = (
        pool_key(server, auth_scope, pool_owner)
        if isolated and pool_owner is not None and original is None
        else None
    )
    if isolated and (
        configured.is_connected()
        or any(
            host.client is not None
            and getattr(host.client.transport, "original", None) is configured.transport
            for host in self.connections.values()
        )
    ):
        raise ValueError("Legacy invocation input requires a fresh exclusive transport")
    key = (
        server,
        auth_scope,
        original.session
        if original is not None
        else uuid4().hex
        if isolated
        else _in_process_owner(configured),
    )
    if key in self.connections:
        return self.connections[key]
    opening = self._opening.get(key)
    if opening is None:

        async def open_client() -> ExternalConnection:
            host = ExternalConnection(
                original.model_copy(update={"task_id": ""})
                if original is not None
                else BackendReference(
                    owner="external_mcp",
                    server=server,
                    auth_scope=auth_scope,
                    session=uuid4().hex,
                    transport=type(configured.transport).__name__,
                ),
                isolated=isolated,
                cache_key=key,
                pool=pool,
                owner_ref=pool_owner if pool is not None else None,
            )

            async def observe(session: Any, task_id: str, reply: ClientGetTaskResult) -> None:
                await self._observe(host, task_id, reply)

            async def guard(
                session: Any, task_id: str, answers: dict[str, Any]
            ) -> dict[str, Any] | None:
                def validate() -> dict[str, Any] | None:
                    with self.history.serialized():
                        current = self._task(host, task_id)
                        details = self._details(current)
                        withdrawing = details.get("withdrawing") or {}
                        kept = {k: v for k, v in answers.items() if k not in withdrawing}
                        if any(
                            details.get("answers", {}).get(key) != value
                            for key, value in kept.items()
                        ):
                            raise ValueError(
                                "Task update differs from confirmed canonical input responses"
                            )
                        if not withdrawing:
                            return None
                        # A withdrawn input is never delivered: the task stopped asking.
                        for key in answers:
                            withdrawing.pop(key, None)
                        details["withdrawing"] = withdrawing
                        reference = self.history.write_content(current.owner.session_id, details)
                        self.history.record_snapshot(
                            current.model_copy(update={"details": reference, "updated_at": now()}),
                            observation_id=uuid4().hex,
                        )
                        return kept

                return await self.history.io.run(validate)

            async def accepted(call: AcceptedNativeCall) -> Any:
                previous = host.calls.get(call.task.task_id)
                if previous is not None and previous.submission.handle != call.submission.handle:
                    raise ValueError("Server reused an active task identity for another submission")
                host.calls[call.task.task_id] = call  # retain custody before persistence
                # Its result names its refusals; its record keeps them across a restart.
                net_refusals.outlast(
                    call.submission.handle,
                    keep=external_task_records.refusal_keeper(
                        self.history, asyncio.get_running_loop()
                    ),
                )

                async def adopt() -> Any:
                    return await self.history.io.run(self._adopt, host, call)

                try:
                    receipt = await adopt()
                except Exception:

                    async def retry() -> None:
                        assert host.client is not None
                        if any(
                            error.accepted is call for error in host.client.unconfirmed_adoptions
                        ):
                            await host.client.retry_adoption(call.submission.handle)
                        else:
                            await adopt()
                        self._drive(host, call)
                        await self._retire_resources(host, call)

                    self.recovery.defer("external-adopt:" + call.submission.handle, retry)
                    raise
                self._drive(host, call)
                return receipt

            async def elicit(message: str, response_type: Any, params: Any, request: Any) -> Any:
                try:
                    return await self._input(host, params, request)
                except Exception:
                    # The SDK reduces this to an error message; keep where it failed.
                    logger.warning(
                        "Task input reason=external_input_failed server=%s request=%s",
                        host.backend.server,
                        getattr(request, "request_id", ""),
                        exc_info=True,
                    )
                    raise

            transport = TaskObservationTransport(configured.transport, observe, guard)
            client = TaskAwareClient.from_configured(
                configured, transport, on_task_accepted=accepted, elicitation_handler=elicit
            )
            try:
                # The connection outlives the turn whose call opened it (enter_connection).
                host.client = await enter_connection(client)
                host.tools = None
                host.stack.push_async_exit(client)
            except BaseException:
                await host.stack.aclose()
                await client.close()
                raise
            self.connections[key] = host
            await _retire_superseded(self, host)
            return host

        opening = asyncio.create_task(open_client())
        self._opening[key] = opening
        if pool is not None:  # a later holder or lease of the pool joins this start
            from clio_agent.tools.mcp_server_progress import current_tree  # noqa: PLC0415

            self._pool_opening[pool] = (opening, current_tree())

        def opened(
            task: asyncio.Task[ExternalConnection], key: Any = key, pool: Any = pool
        ) -> None:
            # Even when every waiter was cancelled: the started server stays registered.
            if self._opening.get(key) is task:
                del self._opening[key]
            if pool is not None and self._pool_opening.get(pool, (None,))[0] is task:
                del self._pool_opening[pool]
            if not task.cancelled() and task.exception() is not None:
                logger.info("external_mcp_open_failed server=%s error=%r", server, task.exception())

        opening.add_done_callback(opened)
    return await asyncio.shield(opening)


def accepted_call(host: ExternalConnection, handle: str) -> bool:
    """Whether the server accepted submission ``handle`` as a task on ``host``."""
    return any(call.submission.handle == handle for call in host.calls.values())


def pool_key(server: str, auth_scope: str, owner: Any) -> tuple[str, str, str]:
    """The pool of kept-alive direct connections to ``server`` through fleet proxy ``owner``."""
    return (server, auth_scope, f"in-process:{id(owner):x}")


def _usable(host: ExternalConnection) -> bool:
    return (
        host.client is not None
        and host.client.is_connected()
        and not (host.closing or host.superseded or host.broken)
    )


async def lease_or_open(
    self: ExternalTasks,
    server: str,
    configured: Client,
    auth_scope: str,
    owner: Any,
    token: object,
) -> ExternalConnection:
    """The connection one call uses: the fleet's kept-alive one, shared, when ``owner``.

    Parallel calls through one fleet proxy reach the same server process, as they did
    through the proxy: a stateful server (a scene, a notebook kernel, a database session)
    keeps its state across them. Without ``owner``, or for an in-process server, this is
    :func:`connection` as before. Each call's progress names its own token; an
    elicitation without a task belongs to the only call still waiting on a plain reply
    (external_task_inputs). The connection's egress channel joins the calling tool call,
    so what the chokepoint refused on it reaches that call's result (net_refusals).
    """
    if owner is None or not isinstance(configured.transport, (StdioTransport, SSETransport)):
        host = await connection(self, server, configured, auth_scope)
    else:
        host = await _lease(self, server, configured, auth_scope, owner, token)
    from clio_agent.tools.fleet_serving import net_child_of  # noqa: PLC0415

    channel = net_child_of(getattr(host.client, "transport", None))
    net_refusals.join(channel, label=f"the {server} server")  # shared by its calls
    return host


async def _pooled(
    self: ExternalTasks,
    server: str,
    configured: Client,
    auth_scope: str,
    owner: Any,
    by: str,
) -> ExternalConnection:
    """The pool's open connection, joining its in-flight start, or a new one.

    Under the pool's lock. A start whose waiter was cancelled (a stopped turn, a wait
    that ended) keeps starting the same server; the next caller joins it instead of
    spawning a second process, so the pool never has two servers alive at once.
    """
    pool = pool_key(server, auth_scope, owner)
    starting = self._pool_opening.get(pool)
    if starting is not None and not starting[0].done():
        from clio_agent.tools.mcp_server_progress import follow_tree  # noqa: PLC0415

        follow_tree(starting[1])  # this wait measures the server that start spawned
        logger.info("external_mcp_kept_alive_joined server=%s by=%s pool=%s", server, by, pool[2])
        with suppress(Exception):  # a failed start is replaced below; its error is logged
            await asyncio.shield(starting[0])
    ready = [host for host in self.connections.values() if host.pool == pool and _usable(host)]
    if ready:
        return max(ready, key=lambda candidate: candidate.holders)
    logger.info("external_mcp_kept_alive_opened server=%s by=%s pool=%s", server, by, pool[2])
    return await connection(self, server, configured, auth_scope, pool_owner=owner)


async def _lease(
    self: ExternalTasks,
    server: str,
    configured: Client,
    auth_scope: str,
    owner: Any,
    token: object,
) -> ExternalConnection:
    pool = pool_key(server, auth_scope, owner)
    async with self._pool_locks.setdefault(pool, asyncio.Lock()):
        host = await _pooled(self, server, configured, auth_scope, owner, "call")
        host.leases[token] = None
        return host


async def live_client(self: ExternalTasks, server: str, owners: Iterable[Any]) -> Any | None:
    """A kept-alive client of ``server`` through one of ``owners`` (fleet proxies), joining
    a start in flight; ``None`` when none runs.

    For a reader that only needs the server to answer (a connector's tool count): it
    uses the process the conversations already run, so the namespace never has a
    second server alive beside it. The join has no limit of its own: the start it joins
    is watched by its own connect (stall reporting, no ceiling).
    """
    tags = {pool_key(server, "", owner)[2] for owner in owners}
    for pool, (opening, _tree) in tuple(self._pool_opening.items()):
        if pool[0] == server and pool[2] in tags and not opening.done():
            logger.info("external_mcp_live_client_joined server=%s", server)
            with suppress(Exception):  # a failed start leaves no process to read
                await asyncio.shield(opening)
    ready = [
        host
        for host in self.connections.values()
        if host.pool is not None
        and host.pool[0] == server
        and host.pool[2] in tags
        and _usable(host)
    ]
    if not ready:
        return None
    return max(ready, key=lambda candidate: candidate.holders).client


def release(host: ExternalConnection, token: object) -> None:
    """End ``token``'s lease on ``host``; its record may still be landing (``settling``)."""
    if host.pool is None or token not in host.leases:
        return
    submission = host.leases.pop(token)
    if submission is not None:
        host.settling.append(submission)


def waiting_inline(host: ExternalConnection) -> list[SubmissionContext]:
    """Calls on a kept-alive connection still waiting on a plain (task-less) reply."""
    tasks = {call.submission.handle for call in host.calls.values()}
    return [
        submission
        for submission in host.leases.values()
        if submission is not None and submission.handle not in tasks
    ]


def note_failure(host: ExternalConnection, error: BaseException) -> None:
    """A kept-alive connection whose transport failed under a call is not leased again."""
    if host.pool is not None and not isinstance(error, MCPError):
        host.broken = True


async def refresh_instructions(
    self: ExternalTasks, *, timeout: float, servers: Collection[str] | None = None
) -> int:
    """Ask each kept-alive direct connection's server for its instructions now.

    One ``server/discover`` per server, in parallel, each bounded by ``timeout``
    (:func:`~clio_agent.tools.mcp_server_instructions.rediscover_instructions`); a
    turn reads the result in its prompt and ``search_tools``. ``servers`` limits the
    asking to those namespaces. Returns how many answered.
    """
    from clio_agent.tools.mcp_server_instructions import (  # noqa: PLC0415
        rediscover_instructions,
    )

    clients: dict[str, Any] = {}
    for host in tuple(self.connections.values()):
        if servers is not None and host.cache_key[0] not in servers:
            continue
        if host.pool is not None and _usable(host):
            clients.setdefault(host.cache_key[0], host.client)
    if not clients:
        return 0
    answered = await asyncio.gather(
        *(
            rediscover_instructions(server, client, timeout=timeout)
            for server, client in clients.items()
        )
    )
    return sum(answered)


async def hold(
    self: ExternalTasks, server: str, auth_scope: str, owner: Any, configured: Client
) -> tuple[Any, Callable[[], Awaitable[None]]]:
    """Hold the fleet's kept-alive direct connection to ``server`` for an executor.

    The executor lists, reads resources and listens over it, and calls share it
    (:func:`lease_or_open`), so the server runs once per fleet. ``configured`` (a fresh,
    unentered client) opens it when none is open. Returns the client and the coroutine
    function releasing the hold; the connection itself stays until its fleet closes
    (:func:`retire_connection`).
    """
    pool = pool_key(server, auth_scope, owner)
    async with self._pool_locks.setdefault(pool, asyncio.Lock()):
        host = await _pooled(self, server, configured, auth_scope, owner, "holder")
        host.holders += 1
    released = False

    async def release_hold() -> None:
        nonlocal released
        if released:
            return
        released = True
        host.holders = max(0, host.holders - 1)
        if not host.holders:
            await retire_connection(self, host)

    assert host.client is not None
    return host.client, release_hold


#: What a plain call's task record says when its turn was stopped mid-call.
TURN_CALL_CANCELLED = "mcp_call_cancelled_by_stop"
TURN_CALL_CANCELLED_TEXT = (
    f"{TURN_CALL_CANCELLED}: the turn was stopped before the service answered; "
    "the call was cancelled and whether the service acted on it is not known."
)
#: The same for a ``tools.*`` call whose ``run_code`` script was stopped mid-call (a
#: background script outlives its turn, so the turn's stop does not cover it).
SCRIPT_CALL_CANCELLED_TEXT = (
    f"{TURN_CALL_CANCELLED}: the script was stopped before the service answered; "
    "the call was cancelled and whether the service acted on it is not known."
)


def _cut_off_text(self: ExternalTasks, sid: str) -> str:
    """Why a plain call is cut off now: its view's request went, its turn stopped, or
    the script that made it was stopped."""
    if view_call_cancel.requested():
        return view_call_cancel.VIEW_CALL_CANCELLED_TEXT
    from clio_agent.gact.cancellation_check import turn_stop_requested  # noqa: PLC0415
    from clio_agent.runtime.cancellation import scope_stopped  # noqa: PLC0415

    if scope_stopped():
        return SCRIPT_CALL_CANCELLED_TEXT
    if turn_stop_requested(getattr(self, "app", None), sid):
        return TURN_CALL_CANCELLED_TEXT
    return ""


async def abandon_call(
    self: ExternalTasks, host: ExternalConnection, handle: str, rpc: Any, lazy: Any, sid: str = ""
) -> None:
    """Cancel the RPC of a plain call nobody waits for any more.

    That is a view's call outside a turn whose request went away, or a turn's call
    when the turn was stopped (Stop, or its assignment cancelled; not the service's
    shutdown, whose restart recovers it). No task was accepted for it, so nothing else
    waits for its reply: the RPC is cancelled on the wire (``notifications/cancelled``)
    and its record ends cancelled with the typed reason and ``settlement: unknown``,
    opening no attention episode, so no model is woken for it (:mod:`.view_call_ledger`).
    An accepted task keeps its custody: it is the conversation's started work, which
    Stop keeps or cancels as a task (``children``).
    """
    if accepted_call(host, handle):
        return
    text = _cut_off_text(self, sid)
    if not text:
        return
    logger.info("external_mcp_call_cut_off handle=%s reason=%s", handle, text.split(":", 1)[0])
    rpc.cancel()
    if lazy is None:  # written ahead: it ends cancelled, outcome unknown, no wake (MC-F2)
        await self._record_or_defer(
            "external-call:" + handle,
            partial(view_call_ledger.settle_cut_off, self.history, handle, text),
        )
    self._lazy.pop(handle, None)


async def _retire_superseded(self: ExternalTasks, current: ExternalConnection) -> None:
    """Retire idle retained connections to a replaced in-process server (typed).

    A replaced server kept for its open App views (``namespace_instances``
    retention) keeps its connection, and a view's call opening one to it retires
    nothing: the views draw on the old server while new calls go to the new one.
    """

    server, auth_scope, owner = current.cache_key
    if not owner.startswith("in-process:"):
        return
    retained = namespace_instances.retained_owners()
    if owner in retained:
        return
    for host in tuple(self.connections.values()):
        stale_server, stale_scope, stale_owner = host.cache_key
        if (stale_server, stale_scope) != (server, auth_scope) or stale_owner == owner:
            continue
        if not stale_owner.startswith("in-process:") or stale_owner in retained:
            continue
        await _retire_stale(self, host, EXTERNAL_MCP_CONNECTION_SUPERSEDED)


async def _retire_stale(
    self: ExternalTasks, host: ExternalConnection, reason: str, **why: str
) -> None:
    """Retire ``host`` now when idle; a busy one stops now so its calls end typed.

    A busy stdio server is stopped here, before its replacement starts (MI1): a call
    waiting on it (one whose reply never comes, such as an ssh that never exits) then
    fails at once with ``reason``, instead of waiting on a server nothing routes to. A
    remote (http) server is not ours to stop: its calls finish, and it retires after.
    """

    idle = not (host.rpcs or host.calls or any(not d.done() for d in host.drivers.values()))
    detail = "".join(f" {key}={value}" for key, value in why.items())
    logger.warning("%s server=%s retired=%s%s", reason, host.cache_key[0], idle, detail)
    if idle:
        await retire_connection(self, host, shutdown=True)
        return
    host.superseded = True  # never leased again; its records name why
    host.stop_reason = reason
    client = host.client
    if client is None or host.closing or "stdio" not in host.backend.transport.casefold():
        return
    if why.get("because") in _IDLE_REAPS and not host.rpcs and holds_tasks(host):
        # An idle fleet reap replaces nothing: stopping the server would cut the input
        # path of the tasks it still drives, losing the answers a person gives them. It
        # retires once their custody ends, as a remote server does.
        logger.warning(
            "external_mcp_connection_kept_for_tasks server=%s reason=%s tasks=%d",
            host.cache_key[0],
            reason,
            len(host.calls),
        )
        return
    logger.warning(
        "external_mcp_connection_stopped_busy server=%s reason=%s rpcs=%d tasks=%d",
        host.cache_key[0],
        reason,
        len(host.rpcs),
        len(host.calls),
    )
    try:
        await client.close()  # pending replies fail now; retire_connection finishes it
    except Exception:
        logger.exception("external_mcp_connection_stop_failed server=%s", host.cache_key[0])


def watch_fleets(self: ExternalTasks) -> None:
    """Retire the connections retained on a fleet's proxies when it drops them (CV-N2).

    The reaper, a restart or a rebuild closes a fleet, and a connector change rebinds
    a namespace to a new proxy, on whichever thread does it; the retirement runs on the
    application's loop. Registered once per application, removed by
    :func:`close_connections`.
    """

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # built outside the application's lifespan (tests): not watched
        logger.info("external_mcp_fleet_watch_skipped reason=no_running_loop")
        return
    owner = weakref.ref(self)

    def dropped(root: str, proxies: tuple[Any, ...], reason: str) -> None:
        tasks = owner()
        if tasks is None or tasks._closing or loop.is_closed():
            return
        owners = {f"in-process:{id(proxy):x}" for proxy in proxies}
        asyncio.run_coroutine_threadsafe(retire_owned(tasks, owners, root, reason), loop)

    self._unwatch_fleets = fleet_close_listeners.listen(dropped)

    async def stop(proxies: tuple[Any, ...], reason: str) -> int:
        tasks = owner()
        if tasks is None or tasks._closing:
            return 0
        owners = {f"in-process:{id(proxy):x}" for proxy in proxies}
        return await retire_owned(tasks, owners, "-", reason)

    # MI1: a connector's server about to start in a folder first retires the kept-alive
    # connection to its other server there.
    unstop = namespace_instances.add_stopper(loop, stop)
    unwatch = self._unwatch_fleets

    def unwatch_all() -> None:
        unwatch()
        unstop()

    self._unwatch_fleets = unwatch_all


def share_direct_connections(self: ExternalTasks) -> None:
    """Let executors on the application's loop hold its kept-alive direct connections.

    Removed by :func:`close_connections`; built outside a running loop (tests), nothing
    is registered and executors keep their own connections.
    """

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    owner = weakref.ref(self)

    def available() -> bool:
        tasks = owner()
        return tasks is not None and not tasks._closing

    async def held(
        server: str, auth_scope: str, proxy: Any, configured: Any
    ) -> tuple[Any, Callable[[], Awaitable[None]]]:
        tasks = owner()
        if tasks is None or tasks._closing:
            raise RuntimeError("External task observation is closing")
        return await hold(tasks, server, auth_scope, proxy, configured)

    self._unshare_direct = shared_direct_connections.register(loop, held, available)


async def retire_owned(self: ExternalTasks, owners: set[str], root: str, reason: str) -> int:
    """Retire every retained connection to one of ``owners``; return how many matched."""

    matched = [
        host
        for host in tuple(self.connections.values())
        if host.cache_key[2] in owners or (getattr(host, "pool", None) or ("", "", ""))[2] in owners
    ]
    for host in matched:
        await _retire_stale(
            self, host, EXTERNAL_MCP_CONNECTION_FLEET_CLOSED, fleet=root or "-", because=reason
        )
    return len(matched)


def failure_reason(host: ExternalConnection, error: BaseException) -> str:
    """The recorded reason of a failed submission; a stopped connection's is typed."""
    text = str(error) or type(error).__name__
    if not host.superseded:
        return text
    return f"{host.stop_reason or EXTERNAL_MCP_CONNECTION_SUPERSEDED}: {text}"


async def retire_connection(
    self: ExternalTasks,
    host: ExternalConnection,
    *,
    shutdown: bool = False,
) -> None:
    """Close one exclusive transport only after submission and observation custody ends.

    A shared connection closes only at shutdown, or once it was superseded (its proxy is
    no longer routed to): then it goes when its last call's custody ends. A kept-alive
    direct connection stays open while it still works, until its fleet closes (the
    reaper, a restart or a rebuild) or it is superseded; a broken one closes like an
    exclusive one once no call leases it.
    """
    async with host.reconnect_lock:
        if (
            host.client is None
            or host.closing
            or not (shutdown or host.isolated or host.superseded)
        ):
            return
        if host.rpcs or any(not driver.done() for driver in host.drivers.values()):
            return
        if host.client.unconfirmed_adoptions:
            if shutdown:
                # The SDK refuses to abandon accepted but unrecorded identities.
                await host.client.close()
            return
        if not shutdown:
            if self.history.requires_recovery or host.calls:
                return
            for invocation in (host.invocation, *host.settling):
                if invocation is None:
                    continue
                current = await self.history.io.run(self.history.task_snapshot, invocation.handle)
                if current is None or (
                    current.terminal_at is None
                    and not (
                        current.raw_status in {"inline", "completed", "failed", "cancelled"}
                        and current.backend_observation is not None
                    )
                ):
                    return
                if invocation in host.settling:
                    host.settling.remove(invocation)
        if (
            host.pool is not None
            and not (shutdown or host.superseded)
            and (host.leases or _usable(host))
        ):
            # A kept-alive server lives as long as its fleet, as it did behind the proxy:
            # a later call finds its state (a scene, a kernel, a session) whether or not
            # an executor holds it. The fleet's close retires it (watch_fleets).
            return
        client = host.client
        host.closing = True
        try:
            await host.stack.aclose()
            # SDK stdio context exit alone preserves keep_alive subprocesses.
            await client.close()
        except BaseException:
            host.closing = False
            raise
        if self.connections.get(host.cache_key) is host:
            self.connections.pop(host.cache_key)
        host.client, host.invocation = None, None
        host.settling.clear()
        host.calls.clear()
        host.drivers.clear()


async def close_connections(self: ExternalTasks) -> None:
    """Finish acceptance, then close observation without cancelling external server jobs."""
    self._closing = True
    if unwatch := getattr(self, "_unwatch_fleets", None):
        unwatch()
    if unshare := getattr(self, "_unshare_direct", None):
        unshare()
    if self._opening:
        await asyncio.shield(asyncio.gather(*tuple(self._opening.values()), return_exceptions=True))
    hosts = tuple(self.connections.values())
    rpcs = tuple(rpc for host in hosts for rpc in host.rpcs)
    if rpcs:
        await asyncio.shield(asyncio.gather(*rpcs, return_exceptions=True))
    drivers = [driver for host in hosts for driver in host.drivers.values()]
    for driver in drivers:
        driver.cancel()
    await asyncio.gather(*drivers, return_exceptions=True)
    if self._retiring:
        await asyncio.shield(asyncio.gather(*tuple(self._retiring)))
    for host in tuple(self.connections.values()):
        await retire_connection(self, host, shutdown=True)
