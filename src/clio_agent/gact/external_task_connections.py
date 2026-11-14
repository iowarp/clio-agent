"""Live external transport resources; Core remains the only task/history authority."""

from __future__ import annotations

import asyncio
import logging
import weakref
from functools import partial
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport, SSETransport, StdioTransport
from fastmcp_tasks.client_models import ClientGetTaskResult

from clio_agent.arc.task_types import BackendReference
from clio_agent.gact import view_call_cancel
from clio_agent.tools import fleet_close_listeners
from clio_agent.tools.mcp_runtime import enter_connection
from clio_agent.tools.task_client import AcceptedNativeCall, TaskAwareClient
from clio_agent.tools.task_transport import TaskObservationTransport

if TYPE_CHECKING:
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
) -> ExternalConnection:
    """Retain a fully configured direct transport on the application lifespan."""
    from clio_agent.gact.external_tasks import ExternalConnection

    if self._closing:
        raise ValueError("External task observation is closing")
    isolated = isinstance(configured.transport, (StdioTransport, SSETransport))
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
            )

            async def observe(session: Any, task_id: str, reply: ClientGetTaskResult) -> None:
                await self._observe(host, task_id, reply)

            async def guard(session: Any, task_id: str, answers: dict[str, Any]) -> None:
                def validate() -> None:
                    current = self._task(host, task_id)
                    details = self._details(current)
                    if any(
                        details.get("answers", {}).get(key) != value
                        for key, value in answers.items()
                    ):
                        raise ValueError(
                            "Task update differs from confirmed canonical input responses"
                        )

                await self.history.io.run(validate)

            async def accepted(call: AcceptedNativeCall) -> Any:
                previous = host.calls.get(call.task.task_id)
                if previous is not None and previous.submission.handle != call.submission.handle:
                    raise ValueError("Server reused an active task identity for another submission")
                host.calls[call.task.task_id] = call  # retain custody before persistence

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
                return await self._input(host, params, request)

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
    try:
        return await asyncio.shield(opening)
    finally:
        if opening.done():
            self._opening.pop(key, None)


def accepted_call(host: ExternalConnection, handle: str) -> bool:
    """Whether the server accepted submission ``handle`` as a task on ``host``."""
    return any(call.submission.handle == handle for call in host.calls.values())


async def abandon_view_call(
    self: ExternalTasks, host: ExternalConnection, handle: str, rpc: Any, lazy: Any
) -> None:
    """Cancel the RPC of a view's call outside a turn whose request went away.

    Only for a plain call (no task was accepted for it): nothing else waits for its
    reply, so the RPC is cancelled on the wire and its record says so with the typed
    reason. A turn's call and an accepted task keep their custody rules unchanged.
    """
    if not view_call_cancel.requested() or accepted_call(host, handle):
        return
    rpc.cancel()
    if lazy is None:  # written ahead: its record says it was cancelled, outcome unknown
        await self._record_or_defer(
            "external-call:" + handle,
            partial(self._attention, handle, view_call_cancel.VIEW_CALL_CANCELLED_TEXT, lost=False),
        )
    self._lazy.pop(handle, None)


async def _retire_superseded(self: ExternalTasks, current: ExternalConnection) -> None:
    """Retire idle retained connections to a replaced in-process server (typed)."""

    server, auth_scope, owner = current.cache_key
    if not owner.startswith("in-process:"):
        return
    for host in tuple(self.connections.values()):
        stale_server, stale_scope, stale_owner = host.cache_key
        if (stale_server, stale_scope) != (server, auth_scope) or stale_owner == owner:
            continue
        if not stale_owner.startswith("in-process:"):
            continue
        await _retire_stale(self, host, EXTERNAL_MCP_CONNECTION_SUPERSEDED)


async def _retire_stale(
    self: ExternalTasks, host: ExternalConnection, reason: str, **why: str
) -> None:
    """Retire ``host`` now when idle; a busy one is marked so its calls end typed."""

    idle = not (host.rpcs or host.calls or any(not d.done() for d in host.drivers.values()))
    detail = "".join(f" {key}={value}" for key, value in why.items())
    logger.warning("%s server=%s retired=%s%s", reason, host.cache_key[0], idle, detail)
    if idle:
        await retire_connection(self, host, shutdown=True)
    else:  # its calls end with their server, and their records name why
        host.superseded = True


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


async def retire_owned(self: ExternalTasks, owners: set[str], root: str, reason: str) -> int:
    """Retire every retained connection to one of ``owners``; return how many matched."""

    matched = [host for host in tuple(self.connections.values()) if host.cache_key[2] in owners]
    for host in matched:
        await _retire_stale(
            self, host, EXTERNAL_MCP_CONNECTION_FLEET_CLOSED, fleet=root or "-", because=reason
        )
    return len(matched)


def failure_reason(host: ExternalConnection, error: BaseException) -> str:
    """The recorded reason of a failed submission; a superseded connection is typed."""
    text = str(error) or type(error).__name__
    return f"{EXTERNAL_MCP_CONNECTION_SUPERSEDED}: {text}" if host.superseded else text


async def retire_connection(
    self: ExternalTasks,
    host: ExternalConnection,
    *,
    shutdown: bool = False,
) -> None:
    """Close one exclusive transport only after submission and observation custody ends.

    A shared connection closes only at shutdown, or once it was superseded (its proxy is
    no longer routed to): then it goes when its last call's custody ends.
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
            if host.invocation is not None:
                current = await self.history.io.run(
                    self.history.task_snapshot, host.invocation.handle
                )
                if current is None or (
                    current.terminal_at is None
                    and not (
                        current.raw_status in {"inline", "completed", "failed", "cancelled"}
                        and current.backend_observation is not None
                    )
                ):
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
        host.calls.clear()
        host.drivers.clear()


async def close_connections(self: ExternalTasks) -> None:
    """Finish acceptance, then close observation without cancelling external server jobs."""
    self._closing = True
    if unwatch := getattr(self, "_unwatch_fleets", None):
        unwatch()
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
