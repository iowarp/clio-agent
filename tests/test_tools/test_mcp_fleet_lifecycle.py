"""MCP fleet lifecycle invariants (#930 S1/#931) — the substrate the memory
campaign optimizes, pinned with a real stdio subprocess.

Truths pinned here (verified 2026-07-14, the #929 diagnosis):

1. ``build_gateway`` spawns NOTHING (lazy proxies).
2. ``build_tool_catalog`` spawns the fleet transiently and REAPS it — the
   #702 boot double-spawn costs latency, not resident memory.
3. ``create_sync_tool_executor`` spawns the fleet EAGERLY at start, before
   any tool call — the resident cost #932 makes lazy (that slice flips the
   third assertion to spawn-on-first-call).
4. ``close()`` reaps the fleet.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import psutil
import pytest

from clio_agent.tools.execution import create_sync_tool_executor
from clio_agent.tools.gateway import build_gateway, build_tool_catalog
from clio_agent.tools.mcp_config import MCPServerSpec

STUB = '''
import os, sys
from pathlib import Path
from fastmcp import FastMCP

marker = Path(sys.argv[1])
with open(marker, "a", encoding="utf-8") as f:
    f.write(f"start {os.getpid()}\\n")

mcp = FastMCP("stub")

@mcp.tool(tags={"stub-tag"})
def echo(text: str) -> str:
    return text

mcp.run()
'''


@pytest.fixture
def stub_spec(tmp_path: Path) -> tuple[MCPServerSpec, Path]:
    script = tmp_path / "stub_mcp.py"
    script.write_text(STUB, encoding="utf-8")
    marker = tmp_path / "stub-marker.txt"
    spec = MCPServerSpec(
        name="stub",
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


def _alive(marker: Path) -> list[int]:
    """Liveness by pid AND identity (cmdline names the stub) — bare
    pid_exists lies under Windows pid reuse (the process_tree.py guard
    pattern)."""

    living: list[int] = []
    for pid in _starts(marker):
        try:
            cmdline = " ".join(psutil.Process(pid).cmdline())
        except psutil.Error:
            continue
        if "stub_mcp.py" in cmdline:
            living.append(pid)
    return living


def _wait_reaped(marker: Path, timeout: float = 15.0) -> list[int]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        living = _alive(marker)
        if not living:
            return []
        time.sleep(0.5)
    return _alive(marker)


def test_fleet_lifecycle_gateway_catalog_executor(stub_spec: tuple[MCPServerSpec, Path]) -> None:
    spec, marker = stub_spec
    try:
        _run_lifecycle(spec, marker)
    finally:
        # Hygiene on failure: never leak a stub into the rest of the suite.
        for pid in _alive(marker):
            try:
                psutil.Process(pid).kill()
            except psutil.Error:
                pass


def _run_lifecycle(spec: MCPServerSpec, marker: Path) -> None:
    # 1. Lazy proxies: mounting spawns nothing.
    gateway = build_gateway({"stub": spec})
    assert _starts(marker) == []

    # 2. Catalog derivation spawns transiently and REAPS (the #702 double-spawn
    #    is latency, not resident memory).
    catalog = build_tool_catalog(gateway)
    assert "stub_echo" in catalog
    assert len(_starts(marker)) == 1
    assert _wait_reaped(marker) == [], "catalog-derivation fleet leaked"

    # 3. Executor start spawns the fleet EAGERLY, before any call — the
    #    resident cost. #932 flips this: after that slice, the spawn count
    #    stays at 1 here and rises only on the first stub tool CALL —
    #    and close() must STILL reap the lazily-spawned server (step 4).
    executor = create_sync_tool_executor(gateway)
    try:
        deadline = time.time() + 15
        while len(_starts(marker)) < 2 and time.time() < deadline:
            time.sleep(0.5)
        assert len(_starts(marker)) == 2, "executor start no longer eager? update for #932"
        assert len(_alive(marker)) == 1

        # The resident server actually serves calls (same process, no respawn).
        result = executor.call_tool("stub_echo", {"text": "ping"})
        assert "ping" in str(result)
        assert len(_starts(marker)) == 2
    finally:
        executor.close()

    # 4. close() reaps the resident fleet.
    assert _wait_reaped(marker) == [], "executor close leaked the fleet"


def test_preloaded_executor_is_lazy_per_namespace(stub_spec: tuple[MCPServerSpec, Path]) -> None:
    """#932: an executor seeded with preloaded tool definitions spawns NOTHING
    at start — the server spawns on the FIRST CALL routed to its namespace,
    stays resident for subsequent calls, and close() still reaps it."""

    spec, marker = stub_spec
    try:
        _run_lazy_lifecycle(spec, marker)
    finally:
        for pid in _alive(marker):
            try:
                psutil.Process(pid).kill()
            except psutil.Error:
                pass


@pytest.fixture()
def _isolated_listing_cache(tmp_path, monkeypatch):
    """#942 tests must never touch the real user listing cache."""

    from clio_agent.tools import listing_cache

    monkeypatch.setattr(listing_cache, "_cache_path", lambda: tmp_path / "listing-cache.json")
    yield


def test_boot_listing_is_sequential_one_chain_at_a_time(
    tmp_path: Path, _isolated_listing_cache
) -> None:
    """#942: the boot listing pass must hold at most ONE declared chain alive
    at a time — the composite pass held the whole fleet at once and the boot
    memory peak was the sum of every chain (broke the v0.8.0 release gate).
    Sampled concurrently while the listing runs; also pins name-prefixing
    equivalence with the composite (keys AND tool.name are namespaced), and
    that a CACHED second boot spawns nothing at all."""

    import threading

    from clio_agent.tools.gateway import list_tool_definitions

    specs: dict[str, MCPServerSpec] = {}
    for ns in ("alpha", "beta", "gamma"):
        script = tmp_path / f"stub_seq_{ns}.py"
        script.write_text(STUB, encoding="utf-8")
        specs[ns] = MCPServerSpec(
            name=ns,
            transport="stdio",
            command=sys.executable,
            args=(str(script), str(tmp_path / f"{ns}-marker.txt")),
        )
    gateway = build_gateway(specs)

    def _stubs_alive() -> int:
        """DISTINCT namespaces with a live chain (a venv python shim + its
        base interpreter are two processes of ONE chain)."""

        namespaces: set[str] = set()
        for proc in psutil.process_iter(["cmdline"]):
            try:
                cmdline = " ".join(proc.info["cmdline"] or [])
            except psutil.Error:
                continue
            for ns in ("alpha", "beta", "gamma"):
                if f"stub_seq_{ns}.py" in cmdline:
                    namespaces.add(ns)
        return len(namespaces)

    peak_concurrent = 0
    stop = threading.Event()

    def _sample() -> None:
        nonlocal peak_concurrent
        while not stop.is_set():
            peak_concurrent = max(peak_concurrent, _stubs_alive())
            time.sleep(0.025)

    sampler = threading.Thread(target=_sample, daemon=True)
    sampler.start()
    try:
        definitions = list_tool_definitions(gateway)
    finally:
        stop.set()
        sampler.join(timeout=5)
        for ns in ("alpha", "beta", "gamma"):
            for pid in _starts(tmp_path / f"{ns}-marker.txt"):
                try:
                    if "stub_seq_" in " ".join(psutil.Process(pid).cmdline()):
                        psutil.Process(pid).kill()
                except psutil.Error:
                    pass

    assert peak_concurrent <= 1, (
        f"boot listing held {peak_concurrent} stub chains alive simultaneously"
    )
    assert {"alpha_echo", "beta_echo", "gamma_echo"} <= set(definitions)
    assert any(name.startswith("fs_") for name in definitions), "builtins missing"
    assert any(name.startswith("shell_") for name in definitions), "builtins missing"
    for name, tool in definitions.items():
        assert tool.name == name, f"tool object not renamed: {tool.name} under key {name}"
    # And the pass reaped everything it spawned.
    for ns in ("alpha", "beta", "gamma"):
        marker = tmp_path / f"{ns}-marker.txt"
        assert len(_starts(marker)) == 1, f"{ns} spawned more than once during listing"

    # A second boot rides the listing cache: identical definitions, ZERO spawns.
    definitions_cached = list_tool_definitions(gateway)
    assert set(definitions_cached) == set(definitions)
    for name, tool in definitions.items():
        # FULL object equality — a lossy round-trip (e.g. the aliased `meta`
        # field silently dropping, with every MCP tag on it) forks cached-boot
        # catalog behavior from first-boot. Key-set equality missed exactly that.
        assert definitions_cached[name] == tool, f"cached round-trip lost data on {name}"
    for ns in ("alpha", "beta", "gamma"):
        marker = tmp_path / f"{ns}-marker.txt"
        assert len(_starts(marker)) == 1, f"cached boot spawned {ns} again"


def test_boot_listing_isolates_a_broken_namespace(tmp_path: Path, _isolated_listing_cache) -> None:
    """#942: one unlistable namespace degrades to no-tools (typed warning);
    every other namespace and the builtins still list."""

    from clio_agent.tools.gateway import list_tool_definitions

    script = tmp_path / "stub_ok.py"
    script.write_text(STUB, encoding="utf-8")
    specs = {
        "ok": MCPServerSpec(
            name="ok",
            transport="stdio",
            command=sys.executable,
            args=(str(script), str(tmp_path / "ok-marker.txt")),
        ),
        "broken": MCPServerSpec(
            name="broken",
            transport="stdio",
            command=sys.executable,
            args=("-c", "import sys; sys.exit(3)"),
        ),
    }
    gateway = build_gateway(specs)
    definitions = list_tool_definitions(gateway)
    assert "ok_echo" in definitions
    assert not any(name.startswith("broken_") for name in definitions)
    assert any(name.startswith("fs_") for name in definitions)


def test_uncalled_namespace_never_spawns(tmp_path: Path) -> None:
    """THE #932 promise: with namespace-direct routing, calling one namespace
    spawns ONLY that namespace's backend — a mounted-but-uncalled server costs
    zero processes (the composite's own name resolution would list-and-spawn
    every mount)."""

    from clio_agent.tools.gateway import list_tool_definitions, namespace_proxies

    specs: dict[str, MCPServerSpec] = {}
    markers: dict[str, Path] = {}
    for ns in ("alpha", "beta"):
        script = tmp_path / f"stub_{ns}.py"
        script.write_text(STUB, encoding="utf-8")
        markers[ns] = tmp_path / f"{ns}-marker.txt"
        specs[ns] = MCPServerSpec(
            name=ns,
            transport="stdio",
            command=sys.executable,
            args=(str(script), str(markers[ns])),
        )

    gateway = build_gateway(specs)
    definitions = list_tool_definitions(gateway)  # one transient pass, both spawn+reap
    assert {"alpha_echo", "beta_echo"} <= set(definitions)
    assert _wait_reaped(markers["alpha"]) == [] and _wait_reaped(markers["beta"]) == []

    executor = create_sync_tool_executor(
        gateway,
        preloaded_tools=definitions,
        namespace_servers=namespace_proxies(gateway),
    )
    try:
        # Composite-fallback pin (fastmcp Namespace gate, load-bearing): an
        # unknown-namespace name errors WITHOUT connecting any declared mount.
        try:
            executor.call_tool("zeta_echo", {"text": "nope"})
        except Exception:
            pass  # the typed not-found error is expected; the assertion is below
        assert len(_starts(markers["alpha"])) == 1, "composite fallback spawned alpha!"
        assert len(_starts(markers["beta"])) == 1, "composite fallback spawned beta!"

        result = executor.call_tool("alpha_echo", {"text": "only-a"})
        assert "only-a" in str(result)
        assert len(_starts(markers["alpha"])) == 2  # listing pass + the routed call
        assert len(_starts(markers["beta"])) == 1, "uncalled namespace spawned!"
    finally:
        executor.close()
        for marker in markers.values():
            for pid in _alive(marker):
                try:
                    psutil.Process(pid).kill()
                except psutil.Error:
                    pass
    assert _wait_reaped(markers["alpha"]) == [], "close leaked the routed namespace"


def _run_lazy_lifecycle(spec: MCPServerSpec, marker: Path) -> None:
    from clio_agent.tools.gateway import list_tool_definitions, namespace_proxies

    gateway = build_gateway({"stub": spec})
    definitions = list_tool_definitions(gateway)  # the ONE boot listing pass
    assert "stub_echo" in definitions
    assert _wait_reaped(marker) == [], "listing-pass fleet leaked"
    assert len(_starts(marker)) == 1

    executor = create_sync_tool_executor(
        gateway,
        preloaded_tools=definitions,
        namespace_servers=namespace_proxies(gateway),
    )
    try:
        # Start spawns NOTHING: definitions are preloaded, no list_tools fan-out.
        time.sleep(2)
        assert len(_starts(marker)) == 1, "preloaded executor start still spawned the fleet"
        assert executor.get_tool_names() and "stub_echo" in executor.get_tool_names()
        assert len(_starts(marker)) == 1, "metadata access spawned the fleet"

        # First CALL spawns exactly one server for the namespace...
        result = executor.call_tool("stub_echo", {"text": "ping"})
        assert "ping" in str(result)
        assert len(_starts(marker)) == 2
        assert len(_alive(marker)) == 1

        # ...and subsequent calls reuse the resident server (no respawn).
        result = executor.call_tool("stub_echo", {"text": "pong"})
        assert "pong" in str(result)
        assert len(_starts(marker)) == 2
    finally:
        executor.close()

    assert _wait_reaped(marker) == [], "executor close leaked the lazily-spawned fleet"
