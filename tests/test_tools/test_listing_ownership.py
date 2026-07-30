"""Exclusive-ownership listing invariants (P0.3 finding #1).

The boot/catalog listing pass MUST construct its OWN transports from the specs
rather than reusing the shared proxy transports the long-lived executor holds.
Two properties follow and are pinned here with real stdio subprocesses:

* A listing (or catalog refresh) can NEVER disconnect an in-flight executor call
  — the two run on disjoint transports and disjoint subprocesses.
* No cross-loop keep_alive poisoning: fastmcp-4 pins a kept-alive session to the
  loop that opened it, but a listing-owned transport is fully torn down before
  its loop closes.

Also covers: a cached second listing spawns nothing; concurrent listings each
own their transport; a failed listing still reaps the subprocess it spawned.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

from clio_agent.tools.execution import create_sync_tool_executor
from clio_agent.tools.gateway import (
    _list_declared_tools,
    build_gateway,
    list_tool_definitions,
    namespace_proxies,
)
from clio_agent.tools.mcp_config import MCPServerSpec

STUB = '''
import os, sys, time
from pathlib import Path
from fastmcp import FastMCP

marker = Path(sys.argv[1])
with open(marker, "a", encoding="utf-8") as f:
    f.write(f"start {os.getpid()}\\n")

mcp = FastMCP("stub")

@mcp.tool
def echo(text: str) -> str:
    return text

@mcp.tool
def slow(seconds: float) -> str:
    time.sleep(seconds)
    return "slow-done"

mcp.run()
'''

# Writes a start marker, then exits non-zero BEFORE serving — a listing against
# it connects a subprocess and then fails, exercising the failure/cleanup path.
CRASH_STUB = '''
import os, sys
from pathlib import Path
with open(Path(sys.argv[1]), "a", encoding="utf-8") as f:
    f.write(f"start {os.getpid()}\\n")
sys.exit(1)
'''


def _write_spec(tmp_path: Path, body: str, name: str = "stub") -> tuple[MCPServerSpec, Path]:
    script = tmp_path / f"{name}_mcp.py"
    script.write_text(body, encoding="utf-8")
    marker = tmp_path / f"{name}-marker.txt"
    spec = MCPServerSpec(
        name=name,
        transport="stdio",
        command=sys.executable,
        args=(str(script), str(marker)),
    )
    return spec, marker


def _starts(marker: Path) -> list[int]:
    if not marker.exists():
        return []
    return [
        int(line.split()[1])
        for line in marker.read_text(encoding="utf-8").splitlines()
        if line.startswith("start")
    ]


def _alive(marker: Path, needle: str = "_mcp.py") -> list[int]:
    living: list[int] = []
    for pid in _starts(marker):
        try:
            cmdline = " ".join(psutil.Process(pid).cmdline())
        except psutil.Error:
            continue
        if needle in cmdline:
            living.append(pid)
    return living


def _wait_starts(marker: Path, count: int, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline and len(_starts(marker)) < count:
        time.sleep(0.05)


def _wait_reaped(marker: Path, timeout: float = 15.0) -> list[int]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _alive(marker):
            return []
        time.sleep(0.25)
    return _alive(marker)


def _kill(marker: Path) -> None:
    for pid in _alive(marker):
        try:
            psutil.Process(pid).kill()
        except psutil.Error:
            pass


@pytest.fixture()
def _isolated_listing_cache(tmp_path, monkeypatch):
    from clio_agent.tools import listing_cache

    monkeypatch.setattr(listing_cache, "_cache_path", lambda: tmp_path / "listing-cache.json")
    yield


def test_listing_owns_its_transport_never_interrupts_active_call(tmp_path: Path) -> None:
    """A listing on the same namespace as an in-flight executor call runs on its
    OWN transport/subprocess and never disconnects the executor's call."""

    spec, marker = _write_spec(tmp_path, STUB)
    gw = build_gateway({"stub": spec})
    definitions = list_tool_definitions(gw)  # own-transport listing (spawn+reap)
    assert "stub_echo" in definitions
    assert _wait_reaped(marker) == []
    assert len(_starts(marker)) == 1

    executor = create_sync_tool_executor(
        gw,
        preloaded_tools=definitions,
        namespace_servers=namespace_proxies(gw),
    )
    result_box: dict[str, str] = {}

    def _run_slow() -> None:
        result_box["out"] = executor.call_tool("stub_slow", {"seconds": 3.0})

    worker = threading.Thread(target=_run_slow)
    try:
        worker.start()
        # The executor spawns its RESIDENT server on the first routed call.
        _wait_starts(marker, 2)
        resident = set(_alive(marker))
        assert resident, "executor resident server never came up"

        # List the SAME namespace via the ownership primitive WHILE the slow call
        # is in flight. It must spawn its own subprocess and reap it...
        listed = _list_declared_tools(spec)
        assert "echo" in {t.name for t in listed}
        assert len(_starts(marker)) == 3, "listing did not own a distinct subprocess"

        # ...without touching the executor's resident server.
        assert resident <= set(_alive(marker)), "listing killed the executor's resident server"

        worker.join(timeout=15)
        assert not worker.is_alive()
        # The in-flight call completed successfully — proof it was not disconnected.
        assert "slow-done" in str(result_box.get("out"))
    finally:
        executor.close()
        _kill(marker)


def test_cached_second_listing_spawns_nothing(
    tmp_path: Path, _isolated_listing_cache
) -> None:
    """A namespace already listed (cached) spawns nothing on a second listing."""

    spec, marker = _write_spec(tmp_path, STUB)
    gw = build_gateway({"stub": spec})
    try:
        first = list_tool_definitions(gw)
        assert "stub_echo" in first
        assert len(_starts(marker)) == 1
        assert _wait_reaped(marker) == []

        second = list_tool_definitions(gw)  # rides the listing cache
        assert set(second) == set(first)
        assert len(_starts(marker)) == 1, "cached listing spawned again"
    finally:
        _kill(marker)


def test_concurrent_listings_each_own_their_transport(tmp_path: Path) -> None:
    """Concurrent listings of one spec each build their own transport and succeed."""

    spec, marker = _write_spec(tmp_path, STUB)
    results: list[set[str]] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def _list_once() -> None:
        try:
            tools = _list_declared_tools(spec)
            with lock:
                results.append({t.name for t in tools})
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=_list_once) for _ in range(3)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not errors, f"a concurrent listing failed: {errors}"
        assert len(results) == 3
        assert all("echo" in r for r in results)
        assert _wait_reaped(marker) == [], "a concurrent listing leaked its subprocess"
    finally:
        _kill(marker)


def test_list_gateway_tools_then_executor_reuse_succeeds(
    tmp_path: Path, _isolated_listing_cache
) -> None:
    """Finding #4: introspecting a declared gateway via ``list_gateway_tools`` on
    a SHORT-LIVED loop routes through the ownership primitive, so the long-lived
    executor can still reuse the shared proxy transports and dispatch a call."""

    import asyncio

    from clio_agent.tools.gateway import list_gateway_tools

    spec, marker = _write_spec(tmp_path, STUB)
    gw = build_gateway({"stub": spec})
    try:
        # A fresh throwaway loop — the pre-fix composite listing would have
        # stranded a kept-alive session on the shared transport here.
        listed = asyncio.run(list_gateway_tools(gw))
        assert any(t["name"] == "stub_echo" for t in listed)
        assert _wait_reaped(marker) == []

        executor = create_sync_tool_executor(
            gw,
            preloaded_tools=list_tool_definitions(gw),
            namespace_servers=namespace_proxies(gw),
        )
        try:
            out = executor.call_tool("stub_echo", {"text": "reuse-ok"})
            assert "reuse-ok" in str(out)
        finally:
            executor.close()
    finally:
        _kill(marker)


def test_failed_listing_still_cleans_up_its_subprocess(tmp_path: Path) -> None:
    """A listing whose backend crashes still reaps the subprocess it spawned."""

    spec, marker = _write_spec(tmp_path, CRASH_STUB, name="crash")
    try:
        with pytest.raises(Exception):  # noqa: B017 - any connect/list failure is acceptable
            _list_declared_tools(spec)
        # It DID spawn (wrote the marker) and was cleaned up by the owning finally.
        assert len(_starts(marker)) >= 1
        assert _wait_reaped(marker, timeout=10) == [], "failed listing leaked its subprocess"
    finally:
        _kill(marker)
