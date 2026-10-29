"""Live external transport resources; Core remains the only task/history authority."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport, SSETransport, StdioTransport
from fastmcp_tasks.client_models import ClientGetTaskResult

from clio_agent.arc.task_types import BackendReference
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
        idle = not (host.rpcs or host.calls or any(not d.done() for d in host.drivers.values()))
        logger.warning(
            "%s server=%s retired=%s",
            EXTERNAL_MCP_CONNECTION_SUPERSEDED,
            server,
            idle,
        )
        if idle:
            await retire_connection(self, host, shutdown=True)


async def retire_connection(
    self: ExternalTasks,
    host: ExternalConnection,
    *,
    shutdown: bool = False,
) -> None:
    """Close one exclusive transport only after submission and observation custody ends."""
    async with host.reconnect_lock:
        if host.client is None or host.closing or not (shutdown or host.isolated):
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
