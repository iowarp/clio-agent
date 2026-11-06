"""Plugin MCP definitions are known before building the initial model request."""

from __future__ import annotations

import threading
from typing import Any

import pytest

from clio_agent.gact.agents.builders import _gateway_and_namespace_tools
from clio_agent.gact.runtime.globals import _UnsupportedSessionAgent
from clio_agent.tools import mcp_prepare
from clio_agent.tools.mcp_config import MCPServerSpec

#: Hang detector for the overlap wait, never a timing budget.
_OVERLAP_BACKSTOP_S = 30.0


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name

    def model_copy(self, *, update: dict[str, Any]) -> "_FakeTool":
        return _FakeTool(update["name"])


class _FakeExecutor:
    """Minimal stand-in for SyncMCPToolExecutor: declared namespace specs, a mergeable
    live tool table, and the one-start listing a preparation uses."""

    def __init__(
        self,
        declared_specs: dict[str, MCPServerSpec],
        preloaded: dict[str, Any],
        listing: Any = None,
    ) -> None:
        self._clio_namespace_specs = declared_specs
        self._mcp_tools: dict[str, Any] = dict(preloaded)
        self.listing = listing or (lambda namespace: [])
        self.started: list[str] = []
        self.prepared_namespaces: set[str] = set()

    def to_dspy_tools(self) -> list[Any]:
        return [_FakeTool(name) for name in self._mcp_tools]

    def get_tool_names(self) -> list[str]:
        return list(self._mcp_tools)

    def merge_namespace_tools(self, namespace: str, tools: dict[str, Any]) -> None:
        del namespace
        self._mcp_tools.update(tools)

    def list_namespace_tools(self, namespace: str) -> list[Any]:
        self.started.append(namespace)
        return list(self.listing(namespace))

    def prepare_namespace(self, namespace: str) -> None:
        self.prepared_namespaces.add(namespace)

    def is_namespace_prepared(self, namespace: str) -> bool:
        return namespace in self.prepared_namespaces


def _spec(name: str) -> MCPServerSpec:
    return MCPServerSpec(name=name, transport="stdio", command="fake-launcher", args=())


@pytest.fixture(autouse=True)
def _no_provisioning(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(
        mcp_prepare, "provision", lambda namespace, spec: mcp_prepare.PROVISION_NOT_APPLICABLE
    )
    # The live listing cache is the real one; these fake launchers never persist.
    monkeypatch.setattr("clio_agent.gact.mcp_readiness._store_live_listing", lambda *a: None)
    monkeypatch.setattr("clio_agent.gact.mcp_readiness.time.sleep", lambda _delay: None)
    yield
    for namespace in ("geo", "ndp", "fs", "ghost"):
        mcp_prepare.forget(namespace)


def _resolve(executor: _FakeExecutor, namespaces: list[str]) -> tuple[dict[str, Any], dict]:
    """Resolve every tool of the plugin namespaces (no gateway built-ins)."""
    return _gateway_and_namespace_tools(executor, [], namespaces)


class TestResolveObtainsInitialDefinitions:
    def test_a_listed_namespace_needs_no_start_before_the_first_model_call(self) -> None:
        executor = _FakeExecutor(
            declared_specs={"geo": _spec("geo")}, preloaded={"geo_geocode": _FakeTool("g")}
        )

        available, mount_failures = _resolve(executor, ["geo"])

        assert "geo_geocode" in available
        assert mount_failures == {}
        assert executor.started == []
        assert executor.prepared_namespaces == set()

    def test_cached_definitions_are_used_while_execution_starts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from clio_agent.gact.mcp_background_prepare import prepare_in_background

        entered = threading.Event()
        release = threading.Event()
        executor = _FakeExecutor(
            declared_specs={"geo": _spec("geo")}, preloaded={"geo_geocode": _FakeTool("g")}
        )

        def connect(namespace: str) -> None:
            entered.set()
            assert release.wait(_OVERLAP_BACKSTOP_S)
            executor.prepared_namespaces.add(namespace)

        monkeypatch.setattr(executor, "prepare_namespace", connect)
        warmup = prepare_in_background(executor, "geo", executor._clio_namespace_specs["geo"])
        try:
            assert entered.wait(_OVERLAP_BACKSTOP_S)
            available, failures = _resolve(executor, ["geo"])
            assert set(available) == {"geo_geocode"}
            assert failures == {}
            assert not warmup.done(), "known definitions must not wait for execution startup"
        finally:
            release.set()
            warmup.result(_OVERLAP_BACKSTOP_S)
        assert executor.started == [], "a cache hit never starts a separate listing process"
        assert executor.prepared_namespaces == {"geo"}

    @pytest.mark.parametrize(
        "spec",
        [
            MCPServerSpec(name="geo", transport="stdio", command="node", args=("server.js",)),
            MCPServerSpec(name="geo", transport="stdio", command="npx", args=("-y", "server")),
            MCPServerSpec(name="geo", transport="stdio", command="native-mcp"),
            MCPServerSpec(name="geo", transport="http", url="https://example.test/mcp"),
            MCPServerSpec(
                name="geo", transport="http", url="https://example.test/sse", legacy_sse=True
            ),
        ],
        ids=["node", "npx", "native", "http", "sse"],
    )
    def test_initial_listing_uses_the_declared_transport(self, spec: MCPServerSpec) -> None:
        executor = _FakeExecutor(
            declared_specs={"geo": spec},
            preloaded={},
            listing=lambda ns: [_FakeTool("geocode")],
        )
        tools, failures = _resolve(executor, ["geo"])
        assert set(tools) == {"geo_geocode"}
        assert failures == {}
        assert executor.started == ["geo"]

    def test_a_cold_namespace_is_listed_before_resolve_returns(self) -> None:
        executor = _FakeExecutor(
            declared_specs={"geo": _spec("geo")},
            preloaded={},
            listing=lambda ns: [_FakeTool("geocode")],
        )

        available, mount_failures = _resolve(executor, ["geo"])

        assert "geo_geocode" in available
        assert mount_failures == {}
        assert "geo_geocode" in executor._mcp_tools, "the merge must reach the LIVE table"
        assert executor.started == ["geo"], "one start serves the connection and the listing"

    def test_a_cold_namespace_joins_warmup_before_the_first_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from clio_agent.gact.mcp_background_prepare import prepare_in_background

        monkeypatch.setenv("CLIO_MCP_TURN_PREPARE_GRACE_S", "0")
        release = threading.Event()
        entered = threading.Event()
        returned = threading.Event()
        results: list[tuple[dict[str, Any], dict]] = []

        def slow(namespace: str) -> list[Any]:
            entered.set()
            release.wait(timeout=_OVERLAP_BACKSTOP_S)
            return [_FakeTool("geocode")]

        executor = _FakeExecutor(declared_specs={"geo": _spec("geo")}, preloaded={}, listing=slow)
        warmup = prepare_in_background(executor, "geo", executor._clio_namespace_specs["geo"])

        def resolve() -> None:
            results.append(_resolve(executor, ["geo"]))
            returned.set()

        worker = threading.Thread(target=resolve)
        worker.start()
        try:
            assert entered.wait(_OVERLAP_BACKSTOP_S)
            assert not returned.wait(0.1), "the first request must include the MCP schemas"
            assert mcp_prepare.is_preparing("geo")
        finally:
            release.set()
            worker.join(_OVERLAP_BACKSTOP_S)
            warmup.result(_OVERLAP_BACKSTOP_S)
        assert not worker.is_alive()
        available, mount_failures = results[0]
        assert set(available) == {"geo_geocode"}
        assert mount_failures == {}
        assert "geo_geocode" in executor._mcp_tools
        assert executor.started == ["geo"], "the turn must join the conversation's one start"

    def test_cold_namespaces_prepare_at_the_same_time(self) -> None:
        """Both cold servers start at once: each start is still in flight when the
        other begins. Proven by overlap, not by a wall-clock sum a loaded runner skews."""
        lock = threading.Lock()
        in_flight: list[str] = []
        both_in_flight = threading.Event()
        overlapped: list[str] = []

        def slow_listing(namespace: str) -> list[Any]:
            with lock:
                in_flight.append(namespace)
                if len(in_flight) == 2:
                    both_in_flight.set()
            if both_in_flight.wait(timeout=_OVERLAP_BACKSTOP_S):
                overlapped.append(namespace)
            return [_FakeTool("x")]

        executor = _FakeExecutor(
            declared_specs={"geo": _spec("geo"), "ndp": _spec("ndp")},
            preloaded={},
            listing=slow_listing,
        )
        _resolve(executor, ["geo", "ndp"])
        mcp_prepare.wait_for_namespaces(["geo", "ndp"], _OVERLAP_BACKSTOP_S)

        assert sorted(overlapped) == ["geo", "ndp"], f"started one after another: {in_flight}"

    def test_undeclared_namespace_is_never_prepared(self) -> None:
        executor = _FakeExecutor(declared_specs={}, preloaded={})

        available, mount_failures = _gateway_and_namespace_tools(
            executor, ["ghost_tool"], ["ghost"]
        )

        assert executor.started == [], "an undeclared namespace must never start a server"
        assert "ghost_tool" not in available
        assert mount_failures == {"ghost": "mcp_server_not_declared"}

    def test_a_namespace_contributes_all_of_its_tools_and_only_its_own(self) -> None:
        executor = _FakeExecutor(
            declared_specs={"geo": _spec("geo"), "ndp": _spec("ndp")},
            preloaded={"ndp_search": _FakeTool("ndp_search")},
            listing=lambda ns: [_FakeTool("geocode"), _FakeTool("reverse")],
        )

        available, mount_failures = _resolve(executor, ["geo"])

        assert set(available) == {"geo_geocode", "geo_reverse"}
        assert mount_failures == {}

    def test_a_prepared_namespace_without_tools_is_a_typed_reason(self) -> None:
        executor = _FakeExecutor(declared_specs={"geo": _spec("geo")}, preloaded={})

        available, mount_failures = _resolve(executor, ["geo"])

        assert available == {}
        assert mount_failures == {"geo": "mcp_server_listed_no_tools"}

    def test_a_gateway_builtin_of_a_cold_declared_namespace_is_prepared(self) -> None:
        executor = _FakeExecutor(
            declared_specs={"fs": _spec("fs")},
            preloaded={},
            listing=lambda ns: [_FakeTool("read_file"), _FakeTool("list_directory")],
        )

        available, mount_failures = _gateway_and_namespace_tools(executor, ["fs_read_file"], [])

        # A built-in is picked by name: its namespace's other tools stay unclaimed.
        assert set(available) == {"fs_read_file"}
        assert mount_failures == {}

    def test_a_transient_start_failure_retries_inside_the_preparation(self) -> None:
        attempts: list[int] = []

        def flaky(namespace: str) -> list[Any]:
            attempts.append(1)
            if len(attempts) == 1:
                raise ConnectionRefusedError("first attempt: transient")
            return [_FakeTool("geocode")]

        executor = _FakeExecutor(declared_specs={"geo": _spec("geo")}, preloaded={}, listing=flaky)

        available, mount_failures = _resolve(executor, ["geo"])

        assert "geo_geocode" in available
        assert mount_failures == {}
        assert len(attempts) == 2

    def test_a_terminal_failure_is_named_and_not_remembered(self) -> None:
        def missing(namespace: str) -> list[Any]:
            raise FileNotFoundError("fake-launcher")

        executor = _FakeExecutor(
            declared_specs={"geo": _spec("geo")}, preloaded={}, listing=missing
        )

        available, mount_failures = _resolve(executor, ["geo"])

        assert "geo_geocode" not in available
        assert mount_failures == {"geo": mcp_prepare.LAUNCHER_MISSING}
        assert executor.started == ["geo"]

        # The next resolve tries again: a failure is never a cached terminal state.
        executor.listing = lambda ns: [_FakeTool("geocode")]
        available, mount_failures = _resolve(executor, ["geo"])
        assert "geo_geocode" in available
        assert mount_failures == {}


class TestUnsupportedSessionAgentMountFailures:
    def test_carries_mount_failures_and_defaults_empty(self) -> None:
        exc = _UnsupportedSessionAgent(
            "geo-expert",
            reason="custom_agent_tools_unavailable",
            tools=["geo_geocode"],
            mount_failures={"geo": "launcher_cache_lock_timeout"},
        )
        assert exc.mount_failures == {"geo": "launcher_cache_lock_timeout"}

        bare = _UnsupportedSessionAgent("x", reason="unknown_or_non_executable_agent")
        assert bare.mount_failures == {}
