"""Namespace preparation behavior shared by async and synchronous MCP executors."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class AsyncNamespacePreparationMixin:
    """Add live namespace merging and persistent connection preparation."""

    _call_lock: Any
    _client: Any
    _closed: bool
    _mcp_tools: dict[str, Any]
    _namespace_clients: dict[str, Any]
    _namespace_servers: Mapping[str, Any]

    async def _connect_namespace(self, namespace: str, proxy: Any) -> None:
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
            if namespace not in self._namespace_clients:
                await self._connect_namespace(namespace, proxy)

    def is_namespace_prepared(self, namespace: str) -> bool:
        """Return whether this executor owns a persistent namespace client."""

        return namespace in self._namespace_clients


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

    def prepare_namespace(self, namespace: str) -> None:
        """Establish and cache a declared namespace's persistent connection."""

        if self._closed:
            raise RuntimeError("SyncMCPToolExecutor is closed")
        self._run_coroutine(
            self._async_executor.prepare_namespace(namespace),
            timeout=self._setup_timeout,
            action=f"MCP namespace {namespace!r} setup",
        )

    def is_namespace_prepared(self, namespace: str) -> bool:
        """Return whether this workspace owns a live namespace client."""

        return self._async_executor.is_namespace_prepared(namespace)
