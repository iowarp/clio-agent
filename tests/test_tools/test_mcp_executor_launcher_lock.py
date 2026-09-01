"""Dispatch-time cold-spawn contention protection (#1237).

The discovery/listing pass (``tools/mcp_discovery.py::_list_one_namespace``)
has always guarded its cold spawn onto the shared uv launcher cache with
``acquire_launcher_cache_lock`` (#1232 pt 3). The ACTUAL dispatch-time spawn
for a real tool CALL -- ``AsyncMCPToolExecutor._connect_namespace``, a
SEPARATE connection from the discovery pass's own throwaway one -- never did,
leaving the exact #1186/#1232-pt-3 shared-uv-cache race unprotected on the
call path. This covers the fix: the lock is acquired only when the
namespace's declared spec actually uses the shared cache, and never
otherwise.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from clio_agent.tools.mcp_config import MCPServerSpec
from clio_agent.tools.mcp_executor import AsyncMCPToolExecutor


class _FakeClient:
    def __init__(self) -> None:
        self.protocol_version = "2026-07-28"

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


def _spec(*, shared: bool = True) -> MCPServerSpec:
    env = {} if shared else {"UV_CACHE_DIR": "/custom/cache"}
    return MCPServerSpec(name="geo", transport="stdio", command="uvx", args=(), env=env)


def _executor(declared_specs: dict[str, MCPServerSpec] | None) -> AsyncMCPToolExecutor:
    namespace_target = object()
    composite_target = object()
    executor = AsyncMCPToolExecutor(
        composite_target,
        timeout=5.0,
        client_factory=lambda _target: _FakeClient(),
        preloaded_tools={},
        namespace_servers={"geo": namespace_target},
        server_id="composite",
    )
    if declared_specs is not None:
        executor._clio_namespace_specs = declared_specs  # noqa: SLF001
    return executor


@pytest.mark.asyncio
async def test_connect_namespace_acquires_the_lock_for_a_shared_cache_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _fake_lock(server_id: str, **_kw: Any):
        calls.append(server_id)
        yield

    monkeypatch.setattr("clio_agent.tools.mcp_executor.aacquire_launcher_cache_lock", _fake_lock)

    executor = _executor({"geo": _spec(shared=True)})
    await executor.start()
    try:
        await executor._connect_namespace("geo", object())  # noqa: SLF001
    finally:
        await executor.aclose()

    assert calls == ["geo"]


@pytest.mark.asyncio
async def test_connect_namespace_skips_the_lock_with_no_declared_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SABOTAGE: a namespace with no _clio_namespace_specs entry (the default
    executor, or a namespace not backed by a cold-cacheable stdio spec) must
    never even ATTEMPT the lock."""

    calls: list[str] = []

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _fake_lock(server_id: str, **_kw: Any):
        calls.append(server_id)
        yield

    monkeypatch.setattr("clio_agent.tools.mcp_executor.aacquire_launcher_cache_lock", _fake_lock)

    executor = _executor(None)
    await executor.start()
    try:
        await executor._connect_namespace("geo", object())  # noqa: SLF001
    finally:
        await executor.aclose()

    assert calls == []


@pytest.mark.asyncio
async def test_connect_namespace_skips_the_lock_for_an_opted_out_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spec with its OWN UV_CACHE_DIR opted out of the shared dir (mirrors
    uses_shared_launcher_cache's own contract) -- never locked."""

    calls: list[str] = []

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _fake_lock(server_id: str, **_kw: Any):
        calls.append(server_id)
        yield

    monkeypatch.setattr("clio_agent.tools.mcp_executor.aacquire_launcher_cache_lock", _fake_lock)

    executor = _executor({"geo": _spec(shared=False)})
    await executor.start()
    try:
        await executor._connect_namespace("geo", object())  # noqa: SLF001
    finally:
        await executor.aclose()

    assert calls == []


@pytest.mark.asyncio
async def test_prepare_namespace_establishes_one_persistent_client() -> None:
    """Readiness connects the workspace client once; later sessions reuse it."""

    composite_target = object()
    namespace_target = object()
    connected_targets: list[object] = []

    def _client_factory(target: object) -> _FakeClient:
        connected_targets.append(target)
        return _FakeClient()

    executor = AsyncMCPToolExecutor(
        composite_target,
        timeout=5.0,
        client_factory=_client_factory,
        preloaded_tools={},
        namespace_servers={"geo": namespace_target},
        server_id="composite",
    )
    await executor.start()
    try:
        await executor.prepare_namespace("geo")
        await executor.prepare_namespace("geo")
    finally:
        await executor.aclose()

    assert connected_targets == [composite_target, namespace_target]
    assert executor.namespace_connection_era("geo") is not None


@pytest.mark.asyncio
async def test_the_real_async_lock_serializes_two_concurrent_cold_spawns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """SABOTAGE (the actual #1237 bug shape): two concurrent on-demand cold
    spawns onto the SAME shared launcher cache (e.g. two sessions/workspaces
    each connecting the same namespace for the first time) must be
    serialized by the REAL async lock -- not race each other. Exercises
    ``aacquire_launcher_cache_lock`` directly (the exact primitive
    ``_connect_namespace`` calls, proven wired-in by the tests above),
    isolating the concurrency guarantee from the surrounding executor/proxy
    machinery.
    """

    from clio_agent.tools import mcp_config as _mcp_config
    from clio_agent.tools.launcher_cache_lock import aacquire_launcher_cache_lock

    monkeypatch.setattr(_mcp_config, "_mcp_uv_cache_dir", lambda: tmp_path)

    order: list[str] = []

    async def _holder() -> None:
        async with aacquire_launcher_cache_lock("geo", timeout_s=10.0):
            order.append("a-start")
            await asyncio.sleep(0.3)
            order.append("a-end")

    async def _waiter() -> None:
        await asyncio.sleep(0.05)  # let the holder acquire first, deterministically
        async with aacquire_launcher_cache_lock("geo", timeout_s=10.0):
            order.append("b-start")

    await asyncio.wait_for(asyncio.gather(_holder(), _waiter()), timeout=15.0)

    assert order == ["a-start", "a-end", "b-start"], order
