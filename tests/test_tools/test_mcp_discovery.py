"""Concurrent, non-readiness-blocking MCP namespace discovery (#1232 pt 2).

Pins the bounded-concurrent discovery pass and the background healer:

* a dead namespace degrades TYPED and immediately, never blocking a sibling;
* the pass's wall time is bounded by the SLOWEST namespace, never the SUM
  (the exact serial-cost-multiplication bug #1232 pt 2 fixes);
* a degraded namespace heals on the background re-probe and calls back with
  its tools + the typed heal event.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from clio_agent.errors import MCP_NAMESPACE_DISCOVERY_TIMEOUT, MCP_NAMESPACE_DISCOVERY_UNREACHABLE
from clio_agent.tools import mcp_discovery
from clio_agent.tools.mcp_config import MCPServerSpec


def _spec(name: str) -> MCPServerSpec:
    return MCPServerSpec(name=name, transport="stdio", command="fake-launcher", args=())


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name

    def model_copy(self, *, update: dict[str, Any]) -> "_FakeTool":
        return _FakeTool(update.get("name", self.name))


def _patch_namespace_timeout(monkeypatch: pytest.MonkeyPatch, timeout_s: float) -> None:
    monkeypatch.setattr(mcp_discovery, "_namespace_attempt_timeout_s", lambda _ns: timeout_s)


def test_fast_namespace_lists_successfully(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_list(
        namespace: str, spec: MCPServerSpec, attempt_key: object = None
    ) -> dict[str, Any]:
        return {f"{namespace}_tool": _FakeTool(f"{namespace}_tool")}

    monkeypatch.setattr(mcp_discovery, "_list_one_namespace", _fake_list)
    _patch_namespace_timeout(monkeypatch, 5.0)

    result = mcp_discovery.discover_declared_tools_bounded({"good": _spec("good")})
    assert "good_tool" in result.tools
    assert result.degraded == {}


def test_dead_namespace_degrades_typed_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_list(
        namespace: str, spec: MCPServerSpec, attempt_key: object = None
    ) -> dict[str, Any]:
        raise ConnectionRefusedError("dead namespace")

    monkeypatch.setattr(mcp_discovery, "_list_one_namespace", _fake_list)
    _patch_namespace_timeout(monkeypatch, 5.0)

    result = mcp_discovery.discover_declared_tools_bounded({"dead": _spec("dead")})
    assert result.tools == {}
    assert result.degraded == {"dead": MCP_NAMESPACE_DISCOVERY_UNREACHABLE}


def test_one_slow_namespace_never_blocks_a_fast_sibling(monkeypatch: pytest.MonkeyPatch) -> None:
    """SABOTAGE: revert to serial-with-no-timeout and this test times out the suite."""

    def _fake_list(
        namespace: str, spec: MCPServerSpec, attempt_key: object = None
    ) -> dict[str, Any]:
        if namespace == "slow":
            time.sleep(10)  # never completes within the test's bound
        return {f"{namespace}_tool": _FakeTool(f"{namespace}_tool")}

    monkeypatch.setattr(mcp_discovery, "_list_one_namespace", _fake_list)
    _patch_namespace_timeout(monkeypatch, 0.3)

    started = time.monotonic()
    result = mcp_discovery.discover_declared_tools_bounded(
        {"slow": _spec("slow"), "fast": _spec("fast")}, concurrency=8
    )
    elapsed = time.monotonic() - started

    assert "fast_tool" in result.tools
    assert result.degraded.get("slow") == MCP_NAMESPACE_DISCOVERY_TIMEOUT
    # Bounded by the DEADLINE, not the sleep duration -- proves the pass moved
    # on rather than waiting for the slow namespace's thread to finish.
    assert elapsed < 2.0, f"pass took {elapsed:.2f}s -- one slow namespace blocked the rest"


def test_three_dead_namespaces_cost_the_max_not_the_sum(monkeypatch: pytest.MonkeyPatch) -> None:
    """The concurrency fix in numbers: 3 namespaces each needing ~0.3s degrade in
    ~0.3s total (concurrent), not ~0.9s (serial) -- the exact "three dead
    namespaces -> minutes of boot" shape, scaled down for a fast unit test."""

    def _fake_list(
        namespace: str, spec: MCPServerSpec, attempt_key: object = None
    ) -> dict[str, Any]:
        time.sleep(0.3)
        raise ConnectionRefusedError("dead")

    monkeypatch.setattr(mcp_discovery, "_list_one_namespace", _fake_list)
    _patch_namespace_timeout(
        monkeypatch, 5.0
    )  # long enough that the sleep, not the deadline, decides

    specs = {f"dead{i}": _spec(f"dead{i}") for i in range(3)}
    started = time.monotonic()
    result = mcp_discovery.discover_declared_tools_bounded(specs, concurrency=8)
    elapsed = time.monotonic() - started

    assert set(result.degraded) == set(specs)
    assert elapsed < 0.9, f"pass took {elapsed:.2f}s -- namespaces ran serially, not concurrently"


def test_empty_specs_returns_immediately() -> None:
    result = mcp_discovery.discover_declared_tools_bounded({})
    assert result.tools == {} and result.degraded == {}


# --------------------------------------------------------------------------- #
# #1240: the connect/list call itself must be bounded (not just how long a   #
# caller waits on it) and an abandoned attempt must be force-closed on the   #
# spot -- the fix for the CI-observed leaked stdio child.                     #
# --------------------------------------------------------------------------- #


def test_list_one_namespace_forwards_the_attempt_timeout_and_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAILING-FIRST for #1240: before this fix, ``_list_declared_tools`` was
    always called bare (no ``timeout_s``, no ``attempt_key``) — the SDK's
    per-request timeout defaults to ``None`` end to end, and
    ``mcp_probe_hardening`` only bounds the era-negotiation probe, not
    ``list_tools``/legacy ``initialize`` — so a namespace whose server
    accepted the connection but never answered ``list_tools`` hung its
    discovery-pool worker (and its spawned stdio child) forever. Pins the
    wiring: the SAME generous runaway deadline that already bounds how long a
    caller WAITS on this attempt now ALSO bounds the attempt's own
    connect/list call, and the attempt is registered under its ``attempt_key``
    so an abandoning caller can force-close it."""

    captured: dict[str, Any] = {}

    def _fake_list_declared_tools(
        spec: MCPServerSpec, *, timeout_s: float | None = None, attempt_key: object | None = None
    ) -> list[Any]:
        captured["timeout_s"] = timeout_s
        captured["attempt_key"] = attempt_key
        return []

    monkeypatch.setattr("clio_agent.tools.gateway._list_declared_tools", _fake_list_declared_tools)
    monkeypatch.setattr(
        "clio_agent.tools.launcher_cache_lock.uses_shared_launcher_cache", lambda spec: False
    )
    _patch_namespace_timeout(monkeypatch, 42.0)

    token = object()
    mcp_discovery._list_one_namespace("geo", _spec("geo"), token)

    assert captured["timeout_s"] == 42.0, "the attempt's own connect/list call must be bounded"
    assert captured["attempt_key"] is token, "the attempt must register under its OWN key"


def test_list_one_namespace_binds_declared_probe_retry_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.tools.mcp_probe_hardening import resolve_timeout_retries

    observed: dict[str, int] = {}

    def _fake_list_declared_tools(
        spec: MCPServerSpec,
        *,
        timeout_s: float | None = None,
        attempt_key: object | None = None,
    ) -> list[Any]:
        del spec, timeout_s, attempt_key
        observed["retries"] = resolve_timeout_retries()
        return []

    monkeypatch.setattr("clio_agent.tools.gateway._list_declared_tools", _fake_list_declared_tools)
    monkeypatch.setattr(
        "clio_agent.tools.launcher_cache_lock.uses_shared_launcher_cache", lambda spec: False
    )
    spec = MCPServerSpec(
        name="geo",
        transport="stdio",
        command="clio-kit",
        probe_timeout_retries=11,
    )

    mcp_discovery._list_one_namespace("geo", spec)

    assert observed == {"retries": 11}


def test_abandoning_a_namespace_force_closes_its_listing_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1240: the MOMENT the pass gives up on a namespace, it force-closes
    THAT specific attempt's transport (freeing its spawned child and any held
    launcher-cache lock immediately) rather than leaving it to whatever is
    left of its own bound. A sibling that is still legitimately in flight (or
    already finished) is never touched."""

    def _fake_list(
        namespace: str, spec: MCPServerSpec, attempt_key: object = None
    ) -> dict[str, Any]:
        if namespace == "slow":
            time.sleep(10)  # never completes within the test's bound
        return {f"{namespace}_tool": _FakeTool(f"{namespace}_tool")}

    monkeypatch.setattr(mcp_discovery, "_list_one_namespace", _fake_list)
    _patch_namespace_timeout(monkeypatch, 0.3)

    closed: list[object] = []
    monkeypatch.setattr(
        "clio_agent.tools.listing_attempts.force_close_listing_attempt",
        lambda key, **_kw: closed.append(key) or True,
    )

    result = mcp_discovery.discover_declared_tools_bounded(
        {"slow": _spec("slow"), "fast": _spec("fast")}, concurrency=8
    )

    assert result.degraded.get("slow") == MCP_NAMESPACE_DISCOVERY_TIMEOUT
    assert "fast_tool" in result.tools
    assert len(closed) == 1, (
        f"expected exactly one force-close (the abandoned namespace), got {closed}"
    )


class TestNamespaceDiscoveryHealer:
    def test_mark_degraded_is_visible_in_pending(self) -> None:
        healer = mcp_discovery.NamespaceDiscoveryHealer(
            spec_provider=dict, on_healed=lambda *_a: None, tick_s=1000.0
        )
        healer.mark_degraded("dead", MCP_NAMESPACE_DISCOVERY_UNREACHABLE)
        assert healer.pending() == {"dead": MCP_NAMESPACE_DISCOVERY_UNREACHABLE}

    def test_probe_once_heals_and_calls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        spec = _spec("dead")
        healed_calls: list[tuple[str, dict[str, Any]]] = []

        def _fake_list(
            namespace: str, s: MCPServerSpec, attempt_key: object = None
        ) -> dict[str, Any]:
            return {f"{namespace}_tool": _FakeTool(f"{namespace}_tool")}

        monkeypatch.setattr(mcp_discovery, "_list_one_namespace", _fake_list)

        healer = mcp_discovery.NamespaceDiscoveryHealer(
            spec_provider=lambda: {"dead": spec},
            on_healed=lambda ns, tools: healed_calls.append((ns, tools)),
            tick_s=1000.0,
        )
        healer.mark_degraded("dead", MCP_NAMESPACE_DISCOVERY_UNREACHABLE)
        healed = healer.probe_once()

        assert healed == ["dead"]
        assert healer.pending() == {}
        assert healed_calls == [("dead", {"dead_tool": healed_calls[0][1]["dead_tool"]})]

    def test_probe_once_keeps_a_still_dead_namespace_pending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _still_dead(
            namespace: str, s: MCPServerSpec, attempt_key: object = None
        ) -> dict[str, Any]:
            raise ConnectionRefusedError("still dead")

        monkeypatch.setattr(mcp_discovery, "_list_one_namespace", _still_dead)

        healer = mcp_discovery.NamespaceDiscoveryHealer(
            spec_provider=lambda: {"dead": _spec("dead")},
            on_healed=lambda *_a: pytest.fail("must not heal a still-dead namespace"),
            tick_s=1000.0,
        )
        healer.mark_degraded("dead", MCP_NAMESPACE_DISCOVERY_UNREACHABLE)
        healed = healer.probe_once()
        assert healed == []
        assert "dead" in healer.pending()

    def test_probe_once_drops_a_namespace_no_longer_declared(self) -> None:
        healer = mcp_discovery.NamespaceDiscoveryHealer(
            spec_provider=dict,  # empty -> "dead" is no longer declared
            on_healed=lambda *_a: pytest.fail("must not call back for an undeclared namespace"),
            tick_s=1000.0,
        )
        healer.mark_degraded("dead", MCP_NAMESPACE_DISCOVERY_UNREACHABLE)
        healed = healer.probe_once()
        assert healed == []
        assert healer.pending() == {}

    def test_start_stop_runs_and_shuts_down_cleanly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ticks = threading.Event()
        monkeypatch.setattr(
            mcp_discovery.NamespaceDiscoveryHealer,
            "probe_once",
            lambda self: (ticks.set(), [])[1],
        )
        healer = mcp_discovery.NamespaceDiscoveryHealer(
            spec_provider=dict, on_healed=lambda *_a: None, tick_s=0.05
        )
        healer.start()
        assert ticks.wait(timeout=2.0), "healer thread never ticked"
        healer.stop()
        assert not healer._thread.is_alive()

    def test_request_stop_does_not_block_the_caller(self) -> None:
        """SABOTAGE: a caller on the event loop calling stop() (the blocking join,
        up to tick_s + 5s) instead of request_stop() would freeze the server on
        every periodic relay-catalog refresh (#1232 pt 2 thread-leak fix)."""

        healer = mcp_discovery.NamespaceDiscoveryHealer(
            spec_provider=dict, on_healed=lambda *_a: None, tick_s=1000.0
        )
        healer.start()
        started = time.monotonic()
        healer.request_stop()
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, f"request_stop blocked for {elapsed:.2f}s -- must be non-blocking"
        # The thread still exits promptly even though the caller did not wait.
        healer._thread.join(timeout=5.0)
        assert not healer._thread.is_alive()


class TestEnsureNamespace:
    """#1237: the call-time on-demand-mount rendezvous point."""

    def teardown_method(self) -> None:
        # Never let one test's in-flight registry entries leak into the next.
        mcp_discovery._ensure_inflight.clear()

    def test_success_returns_tools(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            mcp_discovery,
            "_list_one_namespace",
            lambda ns, s: {f"{ns}_tool": _FakeTool(f"{ns}_tool")},
        )
        result = mcp_discovery.ensure_namespace("geo", _spec("geo"))
        assert "geo_tool" in result
        assert mcp_discovery._ensure_inflight == {}

    def test_concurrent_callers_share_one_mount_attempt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SABOTAGE: two concurrent on-demand mounts for the SAME namespace must
        share ONE attempt, never race a second cold spawn."""

        call_count = 0
        release = threading.Event()

        def _slow_list(namespace: str, spec: MCPServerSpec) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            release.wait(timeout=5.0)
            return {f"{namespace}_tool": _FakeTool(f"{namespace}_tool")}

        monkeypatch.setattr(mcp_discovery, "_list_one_namespace", _slow_list)

        results: list[dict[str, Any]] = []

        def _caller() -> None:
            results.append(mcp_discovery.ensure_namespace("geo", _spec("geo")))

        callers = [threading.Thread(target=_caller, daemon=True) for _ in range(5)]
        for c in callers:
            c.start()
        deadline = time.time() + 2.0
        while "geo" not in mcp_discovery._ensure_inflight and time.time() < deadline:
            time.sleep(0.01)
        assert "geo" in mcp_discovery._ensure_inflight, "no in-flight entry registered"
        release.set()
        for c in callers:
            c.join(timeout=5.0)

        assert call_count == 1, f"expected exactly ONE mount attempt, got {call_count}"
        assert len(results) == 5
        assert all("geo_tool" in r for r in results)

    def test_failed_attempt_is_never_a_cached_terminal_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SABOTAGE: a failed on-demand mount must NOT poison future calls -- the
        next ensure_namespace call for the same namespace must re-attempt, never
        raise a remembered/stale failure."""

        attempts: list[int] = []

        def _fails_once_then_succeeds(namespace: str, spec: MCPServerSpec) -> dict[str, Any]:
            attempts.append(1)
            if len(attempts) == 1:
                raise ConnectionRefusedError("first attempt: transient")
            return {f"{namespace}_tool": _FakeTool(f"{namespace}_tool")}

        monkeypatch.setattr(mcp_discovery, "_list_one_namespace", _fails_once_then_succeeds)

        with pytest.raises(ConnectionRefusedError):
            mcp_discovery.ensure_namespace("geo", _spec("geo"))
        assert mcp_discovery._ensure_inflight == {}, "a failed attempt must not stay registered"

        result = mcp_discovery.ensure_namespace("geo", _spec("geo"))
        assert "geo_tool" in result
        assert len(attempts) == 2, "the second call must re-attempt, not reuse a cached failure"

    def test_ensure_namespace_async_shares_the_sync_registry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        monkeypatch.setattr(
            mcp_discovery,
            "_list_one_namespace",
            lambda ns, s: {f"{ns}_tool": _FakeTool(f"{ns}_tool")},
        )
        result = asyncio.run(mcp_discovery.ensure_namespace_async("pandas", _spec("pandas")))
        assert "pandas_tool" in result
        assert mcp_discovery._ensure_inflight == {}
