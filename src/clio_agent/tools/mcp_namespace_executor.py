"""Namespace preparation behavior shared by async and synchronous MCP executors."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from typing import Any

from clio_agent.tools.mcp_task_routing import record_route_healed, resolve_namespace_route


class AsyncNamespacePreparationMixin:
    """Add live namespace merging and persistent connection preparation."""

    _client: Any
    _closed: bool
    _mcp_tools: dict[str, Any]
    _namespace_clients: dict[str, Any]
    _namespace_ctxs: dict[str, Any]
    _namespace_direct_routes: dict[str, bool]
    _namespace_heal_attempted: set[str]
    _namespace_locks: dict[str, Any]
    _namespace_servers: Mapping[str, Any]
    _connected_namespaces: set[str]

    def _namespace_succeeded(self, namespace: str | None) -> None:
        """Learn a launcher only after its first actual routed call succeeds."""
        if namespace is not None and namespace not in self._connected_namespaces:
            from clio_agent.tools import spawn_diet

            self._connected_namespaces.add(namespace)
            spawn_diet.namespace_connected(namespace)

    def _namespace_call_failed(self, namespace: str | None, error: Exception) -> None:
        """A lazy proxy handshake does not prove its subprocess ever connected."""
        from mcp.shared.exceptions import MCPError
        from mcp_types import CONNECTION_CLOSED

        if (
            namespace is not None
            and namespace not in self._connected_namespaces
            and isinstance(error, MCPError)
            and error.code == CONNECTION_CLOSED
        ):
            from clio_agent.tools import spawn_diet

            spawn_diet.spawn_failed(namespace)

    async def _connect_namespace(self, namespace: str, proxy: Any) -> Any:
        """Connect + cache namespace's client; overridden by AsyncMCPToolExecutor.

        Declared to return the connected client (not ``None`` -- the prior
        stub's lie went unnoticed while every caller discarded the result;
        F12's heal path is the first to assign it).
        """
        raise NotImplementedError

    def merge_namespace_tools(self, namespace: str, tools: Mapping[str, Any]) -> None:
        """Merge freshly mounted definitions into the live flat tool table.

        New names bump :attr:`tools_revision`, so a connected server's later tool-list
        changes reach its agent (``live_tool_refresh``). Initial definitions are joined
        before the first model request.
        """

        del namespace
        added = any(name not in self._mcp_tools for name in tools)
        self._mcp_tools = {**self._mcp_tools, **tools}
        if added:
            self.__dict__["_clio_tools_revision"] = self.tools_revision + 1

    @property
    def tools_revision(self) -> int:
        """Bumped each time a refresh changes this executor's tool table."""

        return self.__dict__.get("_clio_tools_revision", 0)

    def namespaces_pending_refresh(self) -> dict[str, int]:
        """Connected namespaces whose server announced a tool-list change not yet re-listed.

        Only a namespace this executor has talked to qualifies: the notification came
        from a process one of its connections reached. Cheap and lock-free for callers
        (a dictionary read), so an agent loop may ask before every step.
        """

        from clio_agent.tools.mcp_tools_changed import changed_since  # noqa: PLC0415

        seen: dict[str, int] = self.__dict__.setdefault("_clio_tools_seen", {})
        return changed_since(seen, self._namespace_servers)

    async def refresh_changed_namespaces(self) -> dict[str, tuple[list[str], list[str]]]:
        """Re-list each changed namespace over this executor's persistent connection.

        The listing goes over :meth:`_namespace_client` (the proxy front, whose backend
        is the kept-alive process that sent the notification), never a fresh listing
        transport, which would start a new process without the change. The namespace's
        definitions are then replaced, so removed tools disappear too. Returns
        ``{namespace: (added, removed)}`` for each namespace re-listed; every skip and
        failure is a typed trace reason, and a failed re-list keeps the old definitions.
        """

        from anyio import BrokenResourceError, ClosedResourceError  # noqa: PLC0415
        from mcp.shared.exceptions import MCPError  # noqa: PLC0415

        from clio_agent.runtime import trace  # noqa: PLC0415
        from clio_agent.tools import mcp_tools_changed as changed  # noqa: PLC0415

        # A server that cannot answer the re-list: protocol error, closed or broken
        # connection, OS-level failure, timeout, or a closed executor/transport.
        unreachable = (
            MCPError,
            OSError,
            TimeoutError,
            RuntimeError,
            BrokenResourceError,
            ClosedResourceError,
        )

        seen: dict[str, int] = self.__dict__.setdefault("_clio_tools_seen", {})
        results: dict[str, tuple[list[str], list[str]]] = {}
        for namespace, generation in self.namespaces_pending_refresh().items():
            seen[namespace] = generation
            if namespace not in self._connected_namespaces:
                trace.event(
                    "TOOLS",
                    "%s namespace=%s generation=%d",
                    changed.MCP_TOOLS_REFRESH_NOT_CONNECTED,
                    namespace,
                    generation,
                )
                continue
            try:
                client = await self._namespace_client(namespace, self._namespace_servers[namespace])
                listed = await client.list_tools()
            except unreachable as exc:  # typed below; the old definitions stay
                trace.event(
                    "TOOLS",
                    "%s namespace=%s generation=%d error=%r",
                    changed.MCP_TOOLS_REFRESH_FAILED,
                    namespace,
                    generation,
                    exc,
                )
                continue
            tools = changed.prefixed_namespace_tools(namespace, listed)
            table, added, removed = changed.replace_namespace_entries(
                self._mcp_tools, namespace, tools
            )
            # One assignment: a reader holding the old table keeps a consistent view.
            self._mcp_tools = table
            if added or removed:
                self.__dict__["_clio_tools_revision"] = self.tools_revision + 1
            results[namespace] = (added, removed)
            trace.event(
                "TOOLS",
                "%s namespace=%s generation=%d added=%s removed=%s",
                changed.MCP_TOOLS_REFRESHED,
                namespace,
                generation,
                ",".join(added) or "-",
                ",".join(removed) or "-",
            )
        return results

    async def prepare_namespace(self, namespace: str) -> None:
        """Establish one declared namespace's persistent client connection."""

        if self._closed:
            raise RuntimeError("AsyncMCPToolExecutor is closed")
        if self._client is None:
            raise RuntimeError("AsyncMCPToolExecutor is not started")
        proxy = self._namespace_servers.get(namespace)
        if proxy is None:
            raise ValueError(f"unknown MCP namespace {namespace!r}")
        await self._namespace_client(namespace, proxy)

    def is_namespace_prepared(self, namespace: str) -> bool:
        """Return whether this executor owns a persistent namespace client."""

        return namespace in self._namespace_clients

    async def list_namespace_tools(self, namespace: str) -> list[Any]:
        """List ``namespace``'s bare tools over its persistent connection (connecting it).

        The one start of a cold server serves both the connection and the listing: a
        first start connects directly (``mcp_task_routing.first_start_direct``) and
        records the server's capability from that connection, so no heal restarts it.
        """

        from clio_agent.tools.mcp_task_routing import (  # noqa: PLC0415
            first_start_direct,
            record_definitive_capability,
        )

        if self._closed or self._client is None:
            raise RuntimeError("AsyncMCPToolExecutor is not running")
        proxy = self._namespace_servers.get(namespace)
        if proxy is None:
            raise ValueError(f"unknown MCP namespace {namespace!r}")
        lock = self._namespace_locks.setdefault(namespace, asyncio.Lock())
        async with lock:
            client = self._namespace_clients.get(namespace)
            if client is None:
                with first_start_direct(namespace):
                    client = await self._connect_namespace(namespace, proxy)
            listed = list(await client.list_tools())
            if self._namespace_direct_routes.get(namespace, False):
                # A direct connection sees the server itself: its capability is definitive.
                record_definitive_capability(namespace, client, listed)
        return listed

    async def reset_namespace(self, namespace: str) -> None:
        """Close ``namespace``'s persistent connection so the next use starts it again."""

        lock = self._namespace_locks.setdefault(namespace, asyncio.Lock())
        async with lock:
            await self._evict_namespace_client(namespace)
            self._connected_namespaces.discard(namespace)
            self._namespace_heal_attempted.discard(namespace)

    async def _namespace_client(self, namespace: str, proxy: Any) -> Any:
        """Return this namespace's client, connecting / healing it under its own lock.

        Concurrent calls to one namespace share one connect; calls to different
        namespaces never wait on each other, and no lock is held across a call.
        """
        lock = self._namespace_locks.setdefault(namespace, asyncio.Lock())
        async with lock:
            return await self._namespace_client_locked(namespace, proxy)

    async def _namespace_client_locked(self, namespace: str, proxy: Any) -> Any:
        """Return this namespace's persistent client, healing a stale proxy route.

        #1281 F2 (adversarial review): a namespace connected while capability
        was unknown/False caches its client FOREVER without this -- a LATER
        True verdict (from a discovery pass that lands after the connect)
        would change nothing, permanently stranding a live task-capable
        server on the suppressing proxy path. Every reuse re-resolves the
        route; if it flipped unknown/False -> direct, the stale client is
        evicted and reconnected direct, typed ``MCP_TASK_ROUTE_HEALED``.
        Bound STRICTLY to that one direction (checked via
        ``_namespace_direct_routes``, stamped by ``_connect_namespace``) --
        a namespace that connected DIRECT is never evicted/thrashed back to
        proxy by a later degrade, which cannot happen under the F7 demotion
        guard anyway but is enforced here independently.

        #1281 F12 (adversarial review): ``resolve_namespace_route`` sees only
        the capability-derived INTENT, not whether a direct factory actually
        exists/constructs for THIS executor -- when it does not (a missing
        factory, or one that raises, F4/F9), every reuse re-resolved intent
        as "should heal", so EVERY call evicted, reconnected (still landing
        proxy), and reported a heal that never actually happened (measured:
        3 calls -> 3 connects -> 2 false heal events). ``_namespace_heal_
        attempted`` bounds a namespace to AT MOST ONE heal attempt: marked
        BEFORE evicting (so a second reuse never retries), cleared ONLY on a
        confirmed successful direct landing (so a genuine future capability
        fix -- a factory later threaded on, say -- can still heal again).
        ``record_route_healed`` fires ONLY on that confirmed success.
        """

        client = self._namespace_clients.get(namespace)
        if client is None:
            return await self._connect_namespace(namespace, proxy)
        if self._namespace_direct_routes.get(namespace, False):
            return client
        if namespace in self._namespace_heal_attempted:
            return client
        if not resolve_namespace_route(namespace).use_direct:
            return client
        self._namespace_heal_attempted.add(namespace)
        await self._evict_namespace_client(namespace)
        reconnected = await self._connect_namespace(namespace, proxy)
        if self._namespace_direct_routes.get(namespace, False):
            self._namespace_heal_attempted.discard(namespace)
            record_route_healed(namespace)
        return reconnected

    async def _evict_namespace_client(self, namespace: str) -> None:
        """Close + drop a namespace's stale cached client ahead of a heal reconnect."""

        ctx = self._namespace_ctxs.pop(namespace, None)
        self._namespace_clients.pop(namespace, None)
        self._namespace_direct_routes.pop(namespace, None)
        if ctx is not None:
            with suppress(Exception):
                await ctx.__aexit__(None, None, None)


class SyncNamespacePreparationMixin:
    """Expose namespace preparation through the synchronous executor boundary."""

    _async_executor: Any
    _closed: bool
    _loop: asyncio.AbstractEventLoop

    def _run_while_server_works(self, coro: Any, *, action: str) -> Any:
        """Run ``coro`` on the executor loop while the MCP server(s) it starts keep working.

        No fixed deadline: a server still starting on a slow machine is waited for while
        its own process tree works, up to ``tools.mcp.max_wait_s``; a whole
        ``tools.mcp.no_progress_s`` window with no answer and no server work raises the
        typed :class:`~clio_agent.tools.mcp_server_progress.NoProgressTimeout`.
        """
        from clio_agent.tools.mcp_server_progress import wait_while_server_works  # noqa: PLC0415

        waiting = wait_while_server_works(coro, op_name=action)
        return asyncio.run_coroutine_threadsafe(waiting, self._loop).result()

    def merge_namespace_tools(self, namespace: str, tools: Mapping[str, Any]) -> None:
        """Merge freshly mounted definitions into the live async executor."""

        self._async_executor.merge_namespace_tools(namespace, tools)

    @property
    def tools_revision(self) -> int:
        """Bumped each time a refresh changes the tool table (see the async executor)."""

        return self._async_executor.tools_revision

    def refresh_changed_namespaces(self) -> dict[str, tuple[list[str], list[str]]]:
        """Re-list namespaces whose server announced a tool-list change (no-op when none).

        The pending check is a plain read, so the executor loop is only used when a
        connected namespace actually changed.
        """

        if self._closed or not self._async_executor.namespaces_pending_refresh():
            return {}
        return self._run_while_server_works(
            self._async_executor.refresh_changed_namespaces(),
            action="MCP tool list refresh",
        )

    def prepare_namespace(self, namespace: str) -> None:
        """Establish and cache a declared namespace's persistent connection.

        Progress-based (:meth:`_run_while_server_works`): a connect whose server is
        still visibly starting is never cut off and restarted.
        """

        if self._closed:
            raise RuntimeError("SyncMCPToolExecutor is closed")
        self._run_while_server_works(
            self._async_executor.prepare_namespace(namespace),
            action=f"namespace {namespace!r} connect",
        )

    def is_namespace_prepared(self, namespace: str) -> bool:
        """Return whether this workspace owns a live namespace client."""

        return self._async_executor.is_namespace_prepared(namespace)

    def list_namespace_tools(self, namespace: str) -> list[Any]:
        """Connect ``namespace`` and list its bare tools over that one connection."""

        if self._closed:
            raise RuntimeError("SyncMCPToolExecutor is closed")
        return self._run_while_server_works(
            self._async_executor.list_namespace_tools(namespace),
            action=f"namespace {namespace!r} connect and list",
        )

    def reset_namespace(self, namespace: str) -> None:
        """Close ``namespace``'s persistent connection (its server process stops)."""

        if self._closed:
            raise RuntimeError("SyncMCPToolExecutor is closed")
        asyncio.run_coroutine_threadsafe(
            self._async_executor.reset_namespace(namespace), self._loop
        ).result()
