"""Namespace preparation behavior shared by async and synchronous MCP executors."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from typing import Any

from clio_agent.tools.mcp_task_routing import record_route_healed, resolve_namespace_route


class AsyncNamespacePreparationMixin:
    """Add live namespace merging and persistent connection preparation."""

    _call_lock: Any
    _client: Any
    _closed: bool
    _mcp_tools: dict[str, Any]
    _namespace_clients: dict[str, Any]
    _namespace_ctxs: dict[str, Any]
    _namespace_direct_routes: dict[str, bool]
    _namespace_heal_attempted: set[str]
    _namespace_servers: Mapping[str, Any]

    async def _connect_namespace(self, namespace: str, proxy: Any) -> Any:
        """Connect + cache namespace's client; overridden by AsyncMCPToolExecutor.

        Declared to return the connected client (not ``None`` -- the prior
        stub's lie went unnoticed while every caller discarded the result;
        F12's heal path is the first to assign it).
        """
        raise NotImplementedError

    def merge_namespace_tools(self, namespace: str, tools: Mapping[str, Any]) -> None:
        """Merge freshly mounted definitions into the live flat tool table."""

        del namespace
        self._mcp_tools.update(tools)

    async def prepare_namespace(self, namespace: str) -> None:
        """Establish one declared namespace's persistent client connection."""

        if self._closed:
            raise RuntimeError("AsyncMCPToolExecutor is closed")
        if self._client is None or self._call_lock is None:
            raise RuntimeError("AsyncMCPToolExecutor is not started")
        proxy = self._namespace_servers.get(namespace)
        if proxy is None:
            raise ValueError(f"unknown MCP namespace {namespace!r}")
        async with self._call_lock:
            await self._namespace_client(namespace, proxy)

    def is_namespace_prepared(self, namespace: str) -> bool:
        """Return whether this executor owns a persistent namespace client."""

        return namespace in self._namespace_clients

    async def _namespace_client(self, namespace: str, proxy: Any) -> Any:
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
    _setup_timeout: float

    def _run_coroutine(self, coro: Any, *, timeout: float, action: str) -> Any:
        raise NotImplementedError

    def merge_namespace_tools(self, namespace: str, tools: Mapping[str, Any]) -> None:
        """Merge freshly mounted definitions into the live async executor."""

        self._async_executor.merge_namespace_tools(namespace, tools)

    def prepare_namespace(self, namespace: str, *, timeout: float | None = None) -> None:
        """Establish and cache a declared namespace's persistent connection.

        ``timeout`` lets the session-readiness boundary widen successive cold
        start attempts without mutating this executor's configured baseline.
        Ordinary callers retain the configured setup timeout.
        """

        if self._closed:
            raise RuntimeError("SyncMCPToolExecutor is closed")
        effective_timeout = self._setup_timeout if timeout is None else timeout
        if effective_timeout <= 0:
            raise ValueError("namespace setup timeout must be positive")
        self._run_coroutine(
            self._async_executor.prepare_namespace(namespace),
            timeout=effective_timeout,
            action=f"MCP namespace {namespace!r} setup",
        )

    def is_namespace_prepared(self, namespace: str) -> bool:
        """Return whether this workspace owns a live namespace client."""

        return self._async_executor.is_namespace_prepared(namespace)
