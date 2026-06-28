"""Tests for the tool execution boundary."""

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent import conf
from clio_agent.errors import CancellationError
from clio_agent.tools.execution import (
    AsyncMCPToolExecutor,
    MCPToolBridge,
    RepeatedToolFailureError,
    SyncMCPToolExecutor,
    _ground_output_paths,
    create_async_tool_executor,
    create_sync_tool_executor,
    set_global_cancellation_checker,
    set_global_permission_gate,
    set_global_tool_observer,
    tool_workspace_context,
)


class FakeClient:
    """Minimal async client shape used by MCP executors."""

    def __init__(self, *, delay: float = 0.0):
        self.delay = delay
        self.entered = False
        self.exited = False
        self.started_call = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.exited = True

    async def list_tools(self):
        return [
            SimpleNamespace(
                name="fake_echo",
                description="Echo a value.",
                inputSchema={"properties": {"value": {"type": "string"}}},
            )
        ]

    async def call_tool(self, name: str, args: dict[str, Any]):
        import asyncio

        self.started_call = True
        if self.delay:
            await asyncio.sleep(self.delay)
        return SimpleNamespace(data={"name": name, "args": args})


class FailingClient(FakeClient):
    """Fake client that raises configured errors from call_tool."""

    def __init__(self, errors: list[BaseException | None]):
        super().__init__()
        self.errors = errors
        self.calls = 0

    async def call_tool(self, name: str, args: dict[str, Any]):
        self.calls += 1
        if self.errors:
            error = self.errors.pop(0)
            if error is not None:
                raise error
        return SimpleNamespace(data={"name": name, "args": args})


class PlotLikeClient(FakeClient):
    """Fake client exposing a plot-like tool with a relative output_path default."""

    async def list_tools(self):
        return [
            SimpleNamespace(
                name="plot_timeseries",
                description="Plot a CSV time series to an image.",
                inputSchema={
                    "properties": {
                        "data_path": {"type": "string"},
                        "output_path": {"type": "string", "default": "timeseries.png"},
                    }
                },
            )
        ]


class StructuredErrorClient(FakeClient):
    """Fake client that returns a structured tool error payload."""

    async def call_tool(self, name: str, args: dict[str, Any]):
        self.started_call = True
        return SimpleNamespace(
            data={
                "error": {
                    "type": "file_policy",
                    "code": "parent_not_found",
                    "message": "Output directory does not exist",
                },
                "tool": name,
                "args": args,
            }
        )


@pytest.fixture(autouse=True)
def reload_conf() -> None:
    conf.reload()
    yield
    conf.reload()


@pytest.mark.asyncio
async def test_async_mcp_tool_executor_uses_explicit_async_lifecycle():
    """Async executor should call tools without creating a sync bridge thread."""
    fake_client = FakeClient()
    executor = create_async_tool_executor(
        object(),
        timeout=1.0,
        client_factory=lambda _: fake_client,
    )

    assert isinstance(executor, AsyncMCPToolExecutor)
    assert executor.started is False

    async with executor:
        assert executor.started is True
        assert executor.get_tool_names() == ["fake_echo"]
        result = await executor.call_tool("fake_echo", {"value": "hello"})
        assert '"name": "fake_echo"' in result
        assert not hasattr(executor, "_thread")

    assert fake_client.exited is True
    assert executor.closed is True


@pytest.mark.asyncio
async def test_async_mcp_tool_executor_timeout_cancels_tool_call():
    """Async calls should honor the configured timeout and still clean up."""
    fake_client = FakeClient(delay=0.2)
    executor = AsyncMCPToolExecutor(
        object(),
        timeout=0.01,
        client_factory=lambda _: fake_client,
    )
    await executor.start()

    try:
        with pytest.raises(TimeoutError, match="timed out"):
            await executor.call_tool("fake_echo", {"value": "slow"})
        assert fake_client.started_call is True
    finally:
        await executor.aclose()

    assert fake_client.exited is True
    assert executor.closed is True


def test_sync_mcp_tool_executor_closes_client_and_loop():
    """close() should shut down the client and background loop idempotently."""
    fake_client = FakeClient()
    executor = create_sync_tool_executor(
        object(),
        timeout=1.0,
        client_factory=lambda _: fake_client,
    )
    assert isinstance(executor, SyncMCPToolExecutor)
    thread = executor._thread

    try:
        assert executor.get_tool_names() == ["fake_echo"]
        result = executor.call_tool("fake_echo", {"value": "hello"})
        assert '"name": "fake_echo"' in result
    finally:
        executor.close()

    executor.close()
    assert executor.closed is True
    assert fake_client.exited is True
    assert not thread.is_alive()


def test_sync_tool_executor_setup_timeout_uses_config_file(monkeypatch, tmp_path):
    """Factory setup timeout resolves through config before env/default."""
    monkeypatch.chdir(tmp_path)
    config_dir = tmp_path / ".clio"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        "tools:\n  mcp:\n    setup_timeout_s: 42\n",
        encoding="utf-8",
    )
    conf.reload()
    fake_client = FakeClient()
    executor = create_sync_tool_executor(
        object(), timeout=1.0, client_factory=lambda _: fake_client
    )

    try:
        assert executor._setup_timeout == 42.0
    finally:
        executor.close()


def test_sync_tool_executor_explicit_setup_timeout_wins_over_config(monkeypatch, tmp_path):
    """Callers can still override setup timeout directly."""
    monkeypatch.chdir(tmp_path)
    config_dir = tmp_path / ".clio"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        "tools:\n  mcp:\n    setup_timeout_s: 42\n",
        encoding="utf-8",
    )
    conf.reload()
    fake_client = FakeClient()
    executor = create_sync_tool_executor(
        object(),
        timeout=1.0,
        setup_timeout=3.0,
        client_factory=lambda _: fake_client,
    )

    try:
        assert executor._setup_timeout == 3.0
    finally:
        executor.close()


def test_sync_mcp_tool_executor_timeout_cancels_tool_call():
    """Tool calls should honor the configured timeout and still clean up."""
    fake_client = FakeClient(delay=0.2)
    executor = SyncMCPToolExecutor(
        object(),
        timeout=0.01,
        client_factory=lambda _: fake_client,
    )

    try:
        with pytest.raises(TimeoutError, match="timed out"):
            executor.call_tool("fake_echo", {"value": "slow"})
        assert fake_client.started_call is True
    finally:
        executor.close()

    assert fake_client.exited is True
    assert executor.closed is True


def test_sync_mcp_tool_executor_uses_late_global_hooks():
    """Deferred GACT hook install should affect already-built executors."""
    fake_client = FakeClient()
    executor = SyncMCPToolExecutor(
        object(),
        timeout=1.0,
        client_factory=lambda _: fake_client,
    )
    observed: list[tuple[str, dict[str, Any], str | None, str | None]] = []

    try:
        set_global_tool_observer(
            lambda name, args, phase, error: observed.append((name, dict(args), phase, error))
        )

        result = executor.call_tool("fake_echo", {"value": "hello"})

        assert '"name": "fake_echo"' in result
        assert observed == [
            ("fake_echo", {"value": "hello"}, "started", None),
            ("fake_echo", {"value": "hello"}, "completed", None),
        ]

        set_global_permission_gate(lambda _name, _args: "deny")
        with pytest.raises(PermissionError, match="denied"):
            executor.call_tool("fake_echo", {"value": "blocked"})
    finally:
        set_global_permission_gate(None)
        set_global_tool_observer(None)
        executor.close()


def test_sync_mcp_tool_executor_reports_structured_tool_error_result():
    """Structured error payloads should be failed telemetry without hiding evidence."""
    fake_client = StructuredErrorClient()
    executor = SyncMCPToolExecutor(
        object(),
        timeout=1.0,
        client_factory=lambda _: fake_client,
    )
    observed: list[tuple[str, dict[str, Any], str | None, str | None, Any | None]] = []

    try:
        set_global_tool_observer(
            lambda name, args, phase, error, result=None: observed.append(
                (name, dict(args), phase, error, result)
            )
        )

        result = executor.call_tool(
            "ndp_plot_csv_timeseries",
            {"output_path": "/missing/plot.png"},
        )
    finally:
        set_global_tool_observer(None)
        executor.close()

    assert '"error"' in result
    assert fake_client.started_call is True
    assert observed[0] == (
        "ndp_plot_csv_timeseries",
        {"output_path": "/missing/plot.png"},
        "started",
        None,
        None,
    )
    assert observed[-1][0] == "ndp_plot_csv_timeseries"
    assert observed[-1][2] == "completed"
    assert observed[-1][3] is not None
    assert "parent_not_found" in observed[-1][3]
    assert observed[-1][4] == result


def test_sync_mcp_tool_executor_bounds_repeated_transient_tool_failures():
    """Repeated infrastructure failures should become a fast structured blocker."""
    fake_client = FailingClient([TimeoutError("first timeout"), TimeoutError("second timeout")])
    executor = SyncMCPToolExecutor(
        object(),
        timeout=1.0,
        client_factory=lambda _: fake_client,
    )
    observed: list[tuple[str, dict[str, Any], str | None, str | None]] = []

    try:
        set_global_tool_observer(
            lambda name, args, phase, error: observed.append((name, dict(args), phase, error))
        )

        with pytest.raises(TimeoutError):
            executor.call_tool("ndp_search_datasets", {"search_terms": ["UCSF"]})
        with pytest.raises(TimeoutError):
            executor.call_tool("ndp_search_datasets", {"search_terms": ["SBRU"]})
        with pytest.raises(RepeatedToolFailureError, match="status='tool_failed'"):
            executor.call_tool("ndp_search_datasets", {"search_terms": ["MHDL"]})
    finally:
        set_global_tool_observer(None)
        executor.close()

    assert fake_client.calls == 2
    assert observed[-2] == (
        "ndp_search_datasets",
        {"search_terms": ["MHDL"]},
        "started",
        None,
    )
    assert observed[-1][0] == "ndp_search_datasets"
    assert observed[-1][2] == "completed"
    assert observed[-1][3] is not None
    assert "RepeatedToolFailureError" in observed[-1][3]


def test_sync_mcp_tool_executor_does_not_bound_non_transient_errors():
    """Argument or validation errors should keep reaching the tool for correction."""
    fake_client = FailingClient(
        [
            ValueError("missing required argument"),
            ValueError("missing required argument"),
            None,
        ]
    )
    executor = SyncMCPToolExecutor(
        object(),
        timeout=1.0,
        client_factory=lambda _: fake_client,
    )

    try:
        with pytest.raises(ValueError, match="missing required argument"):
            executor.call_tool("fake_echo", {"bad": "one"})
        with pytest.raises(ValueError, match="missing required argument"):
            executor.call_tool("fake_echo", {"bad": "two"})
        result = executor.call_tool("fake_echo", {"value": "fixed"})
    finally:
        executor.close()

    assert fake_client.calls == 3
    assert '"value": "fixed"' in result


def test_sync_mcp_tool_executor_clears_transient_failure_count_after_success():
    """A successful call should reset the transient failure circuit."""
    fake_client = FailingClient(
        [
            TimeoutError("first timeout"),
            None,
            TimeoutError("second timeout"),
            None,
        ]
    )
    executor = SyncMCPToolExecutor(
        object(),
        timeout=1.0,
        client_factory=lambda _: fake_client,
    )

    try:
        with pytest.raises(TimeoutError):
            executor.call_tool("ndp_search_datasets", {"search_terms": ["UCSF"]})
        assert '"search_terms": ["SBRU"]' in executor.call_tool(
            "ndp_search_datasets",
            {"search_terms": ["SBRU"]},
        )
        with pytest.raises(TimeoutError):
            executor.call_tool("ndp_search_datasets", {"search_terms": ["MHDL"]})
        assert '"search_terms": ["EBMD"]' in executor.call_tool(
            "ndp_search_datasets",
            {"search_terms": ["EBMD"]},
        )
    finally:
        executor.close()

    assert fake_client.calls == 4


def test_sync_mcp_tool_executor_repairs_unique_missing_file_arg(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """A model path typo should repair to one unique same-basename allowed-root file."""
    good = tmp_path / "data" / "pathogen_reference.fasta"
    good.parent.mkdir()
    good.write_text(">chrA\nACGT\n", encoding="utf-8")
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))

    fake_client = FakeClient()
    executor = SyncMCPToolExecutor(
        object(),
        timeout=1.0,
        client_factory=lambda _: fake_client,
    )
    observed: list[tuple[str, dict[str, Any], str | None, str | None]] = []

    try:
        set_global_tool_observer(
            lambda name, args, phase, error: observed.append((name, dict(args), phase, error))
        )
        result = executor.call_tool(
            "fake_echo",
            {"filepath": str(tmp_path / "typo" / "pathogen_reference.fasta")},
        )
    finally:
        set_global_tool_observer(None)
        executor.close()

    assert str(good.resolve()) in result
    assert observed == [
        ("fake_echo", {"filepath": str(good.resolve())}, "started", None),
        ("fake_echo", {"filepath": str(good.resolve())}, "completed", None),
    ]


def test_sync_mcp_tool_executor_does_not_repair_ambiguous_missing_file_arg(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Ambiguous same-basename matches stay untouched instead of guessing."""
    first = tmp_path / "a" / "sample.fasta"
    second = tmp_path / "b" / "sample.fasta"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text(">a\nACGT\n", encoding="utf-8")
    second.write_text(">b\nTGCA\n", encoding="utf-8")
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))

    fake_client = FakeClient()
    executor = SyncMCPToolExecutor(
        object(),
        timeout=1.0,
        client_factory=lambda _: fake_client,
    )

    try:
        result = executor.call_tool("fake_echo", {"filepath": str(tmp_path / "typo/sample.fasta")})
    finally:
        executor.close()

    assert str(tmp_path / "typo/sample.fasta") in result
    assert str(first.resolve()) not in result
    assert str(second.resolve()) not in result


def test_sync_mcp_tool_executor_reports_cooperative_cancel_after_tool_result():
    """Cancellation after a tool returns should not publish normal success telemetry."""
    fake_client = FakeClient()
    executor = SyncMCPToolExecutor(
        object(),
        timeout=1.0,
        client_factory=lambda _: fake_client,
    )
    checks = iter([False, True])
    observed: list[tuple[str, dict[str, Any], str | None, str | None]] = []

    try:
        set_global_cancellation_checker(lambda: next(checks, True))
        set_global_tool_observer(
            lambda name, args, phase, error: observed.append((name, dict(args), phase, error))
        )

        with pytest.raises(CancellationError, match="tool call cancelled"):
            executor.call_tool("fake_echo", {"value": "late-cancel"})

        assert fake_client.started_call is True
        assert observed[0] == ("fake_echo", {"value": "late-cancel"}, "started", None)
        assert observed[-1][2] == "completed"
        assert observed[-1][3] is not None
        assert "CancellationError" in observed[-1][3]
    finally:
        set_global_cancellation_checker(None)
        set_global_tool_observer(None)
        executor.close()


def test_mcp_tool_bridge_remains_sync_compatibility_shim():
    """The old bridge name should remain available without driving expert wiring."""
    fake_client = FakeClient()
    bridge = MCPToolBridge(object(), timeout=1.0, client_factory=lambda _: fake_client)

    try:
        assert isinstance(bridge, SyncMCPToolExecutor)
        assert bridge.call_tool("fake_echo", {"value": "hello"}).startswith("{")
    finally:
        bridge.close()


# ---------------------------------------------------------------------------
# Output-path workspace grounding (model-agnostic artifact hygiene).
# ---------------------------------------------------------------------------

_PLOT_SCHEMA = {
    "properties": {
        "data_path": {"type": "string"},
        "output_path": {"type": "string", "default": "timeseries.png"},
    }
}


def test_ground_output_paths_resolves_relative_emitted_path():
    """A relative output path the model emits resolves against the workspace root."""
    grounded = _ground_output_paths(
        {"data_path": "/data/in.csv", "output_path": "plot.png"},
        _PLOT_SCHEMA,
        "/work/space",
    )
    assert grounded["output_path"] == "/work/space/plot.png"
    # Input path is untouched (not an output-arg name).
    assert grounded["data_path"] == "/data/in.csv"


def test_ground_output_paths_injects_workspace_path_when_omitted():
    """An omitted output arg with a relative schema default is injected absolute."""
    grounded = _ground_output_paths(
        {"data_path": "/data/in.csv"},
        _PLOT_SCHEMA,
        "/work/space",
    )
    assert grounded["output_path"] == "/work/space/timeseries.png"


def test_ground_output_paths_leaves_absolute_emitted_path_untouched():
    """An absolute output path the model emits is preserved verbatim."""
    grounded = _ground_output_paths(
        {"data_path": "/data/in.csv", "output_path": "/tmp/abs.png"},
        _PLOT_SCHEMA,
        "/work/space",
    )
    assert grounded["output_path"] == "/tmp/abs.png"


def test_ground_output_paths_noop_without_workspace_root():
    """Without a bound workspace root the args pass through unchanged."""
    args = {"data_path": "/data/in.csv", "output_path": "plot.png"}
    grounded = _ground_output_paths(args, _PLOT_SCHEMA, "")
    assert grounded == args
    # Omission injection is also gated on the workspace root.
    grounded2 = _ground_output_paths({"data_path": "/data/in.csv"}, _PLOT_SCHEMA, "")
    assert "output_path" not in grounded2


def test_ground_output_paths_ignores_absolute_schema_default():
    """A schema default that is already absolute is not re-grounded on omit."""
    schema = {
        "properties": {
            "output_path": {"type": "string", "default": "/etc/fixed/out.png"},
        }
    }
    grounded = _ground_output_paths({"data_path": "/data/in.csv"}, schema, "/work/space")
    assert "output_path" not in grounded


def test_ground_output_paths_ignores_non_artifact_default():
    """An omitted output arg whose default is not a writable artifact is left alone."""
    schema = {
        "properties": {
            "output": {"type": "string", "default": "stdout"},
        }
    }
    grounded = _ground_output_paths({"data_path": "/data/in.csv"}, schema, "/work/space")
    assert "output" not in grounded


def test_sync_mcp_tool_executor_grounds_relative_output_path(tmp_path):
    """A relative output_path the model emits is grounded to the workspace root."""
    fake_client = PlotLikeClient()
    executor = SyncMCPToolExecutor(
        object(),
        timeout=1.0,
        client_factory=lambda _: fake_client,
    )
    try:
        with tool_workspace_context(tmp_path):
            result = executor.call_tool(
                "plot_timeseries",
                {"data_path": "/data/in.csv", "output_path": "plot.png"},
            )
    finally:
        executor.close()
    assert str(tmp_path / "plot.png") in result


def test_sync_mcp_tool_executor_injects_omitted_output_path(tmp_path):
    """An omitted output_path is injected absolute from the schema default."""
    fake_client = PlotLikeClient()
    executor = SyncMCPToolExecutor(
        object(),
        timeout=1.0,
        client_factory=lambda _: fake_client,
    )
    try:
        with tool_workspace_context(tmp_path):
            result = executor.call_tool(
                "plot_timeseries",
                {"data_path": "/data/in.csv"},
            )
    finally:
        executor.close()
    assert str(tmp_path / "timeseries.png") in result


def test_sync_mcp_tool_executor_keeps_absolute_output_path(tmp_path):
    """An absolute output_path is passed through unchanged (no regression)."""
    fake_client = PlotLikeClient()
    executor = SyncMCPToolExecutor(
        object(),
        timeout=1.0,
        client_factory=lambda _: fake_client,
    )
    try:
        with tool_workspace_context(tmp_path):
            result = executor.call_tool(
                "plot_timeseries",
                {"data_path": "/data/in.csv", "output_path": "/tmp/keep.png"},
            )
    finally:
        executor.close()
    assert "/tmp/keep.png" in result
    assert str(tmp_path / "keep.png") not in result
