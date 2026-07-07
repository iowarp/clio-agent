"""Tests for the tool execution boundary."""

import json
import os
from pathlib import Path
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
    ToolRuntimeHooks,
    _ground_output_paths,
    _repair_missing_file_arguments,
    create_async_tool_executor,
    create_sync_tool_executor,
    set_tool_runtime_fallback,
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


def test_sync_mcp_tool_executor_uses_late_fallback_hooks():
    """Deferred hook install (via the app-less fallback bundle) should affect
    already-built executors."""
    fake_client = FakeClient()
    executor = SyncMCPToolExecutor(
        object(),
        timeout=1.0,
        client_factory=lambda _: fake_client,
    )
    observed: list[tuple[str, dict[str, Any], str | None, str | None]] = []

    def _observer(name, args, phase, error):
        observed.append((name, dict(args), phase, error))

    try:
        set_tool_runtime_fallback(ToolRuntimeHooks(tool_observer=_observer))

        result = executor.call_tool("fake_echo", {"value": "hello"})

        assert '"name": "fake_echo"' in result
        assert observed == [
            ("fake_echo", {"value": "hello"}, "started", None),
            ("fake_echo", {"value": "hello"}, "completed", None),
        ]

        set_tool_runtime_fallback(
            ToolRuntimeHooks(tool_observer=_observer, permission_gate=lambda _n, _a: "deny")
        )
        with pytest.raises(PermissionError, match="denied"):
            executor.call_tool("fake_echo", {"value": "blocked"})
    finally:
        set_tool_runtime_fallback(ToolRuntimeHooks())
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
        set_tool_runtime_fallback(
            ToolRuntimeHooks(
                tool_observer=lambda name, args, phase, error, result=None: observed.append(
                    (name, dict(args), phase, error, result)
                )
            )
        )

        result = executor.call_tool(
            "ndp_plot_csv_timeseries",
            {"output_path": "/missing/plot.png"},
        )
    finally:
        set_tool_runtime_fallback(ToolRuntimeHooks())
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
        set_tool_runtime_fallback(
            ToolRuntimeHooks(
                tool_observer=lambda name, args, phase, error: observed.append(
                    (name, dict(args), phase, error)
                )
            )
        )

        with pytest.raises(TimeoutError):
            executor.call_tool("ndp_search_datasets", {"search_terms": ["UCSF"]})
        with pytest.raises(TimeoutError):
            executor.call_tool("ndp_search_datasets", {"search_terms": ["SBRU"]})
        with pytest.raises(RepeatedToolFailureError, match="status='tool_failed'"):
            executor.call_tool("ndp_search_datasets", {"search_terms": ["MHDL"]})
    finally:
        set_tool_runtime_fallback(ToolRuntimeHooks())
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
        set_tool_runtime_fallback(
            ToolRuntimeHooks(
                tool_observer=lambda name, args, phase, error: observed.append(
                    (name, dict(args), phase, error)
                )
            )
        )
        result = executor.call_tool(
            "fake_echo",
            {"filepath": str(tmp_path / "typo" / "pathogen_reference.fasta")},
        )
    finally:
        set_tool_runtime_fallback(ToolRuntimeHooks())
        executor.close()

    # The substitution is surfaced verbatim as a ``[path-repair]`` note prepended
    # to the tool result the model reads back.
    assert result.startswith("[path-repair] argument 'filepath':")
    assert "substituted unique match" in result
    assert str(good.resolve()) in result.splitlines()[0]

    # The JSON body follows the note; compare the parsed arg with Path equality so
    # the assertion is independent of separators and JSON backslash escaping.
    body = result[result.index("{") :]
    repaired = json.loads(body)["args"]["filepath"]
    assert Path(repaired) == good.resolve()
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

    # Ambiguous matches stay untouched: the model's original path is preserved
    # and neither candidate is substituted in.
    kept = json.loads(result)["args"]["filepath"]
    assert Path(kept) == tmp_path / "typo" / "sample.fasta"
    assert Path(kept) != first.resolve()
    assert Path(kept) != second.resolve()


def test_repair_returns_records_for_each_substitution(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """A unique repair yields a structured record ``{argument, requested, used}``."""
    good = tmp_path / "data" / "reference.fasta"
    good.parent.mkdir()
    good.write_text(">chrA\nACGT\n", encoding="utf-8")
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))

    requested = str(tmp_path / "typo" / "reference.fasta")
    repaired, records = _repair_missing_file_arguments({"filepath": requested})

    assert Path(repaired["filepath"]) == good.resolve()
    assert records == [
        {"argument": "filepath", "requested": requested, "used": str(good.resolve())}
    ]


def test_repair_scan_bound_leaves_args_unchanged(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Hitting the scan-entry bound aborts and leaves the argument untouched.

    A partial scan cannot prove a basename match is unique, so no substitution is
    made and no record is surfaced.
    """
    good = tmp_path / "data" / "reference.fasta"
    good.parent.mkdir()
    good.write_text(">chrA\nACGT\n", encoding="utf-8")
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    # Force the very first scanned entry to trip the ceiling.
    monkeypatch.setattr("clio_agent.tools.execution._REPAIR_SCAN_LIMIT", 0)

    requested = str(tmp_path / "typo" / "reference.fasta")
    repaired, records = _repair_missing_file_arguments({"filepath": requested})

    assert repaired == {"filepath": requested}
    assert records == []


def test_repair_scan_bound_aborts_walk_with_no_matches(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """The scan bound stops the WALK itself, not just per-match bookkeeping.

    A no-match basename is the canonical repair trigger (the mistyped file does
    not exist anywhere). The walk must abort at the entry ceiling instead of
    traversing the whole allowed-root tree looking for matches that never come.
    """
    # Tree much larger than the bound: 30 directories x 5 files = 180 entries.
    for d in range(30):
        sub = tmp_path / f"dir_{d:02d}"
        sub.mkdir()
        for f in range(5):
            (sub / f"file_{f}.txt").write_text("x", encoding="utf-8")
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setattr("clio_agent.tools.execution._REPAIR_SCAN_LIMIT", 5)

    real_scandir = os.scandir
    scanned_dirs: list[str] = []

    def counting_scandir(path: Any) -> Any:
        text = os.fspath(path) if not isinstance(path, int) else str(path)
        if text.startswith(str(tmp_path)):
            scanned_dirs.append(text)
        return real_scandir(path)

    monkeypatch.setattr("clio_agent.tools.execution.os.scandir", counting_scandir)

    requested = str(tmp_path / "typo" / "nowhere.fasta")
    repaired, records = _repair_missing_file_arguments({"filepath": requested})

    # Aborted at the bound: unchanged args, no records surfaced.
    assert repaired == {"filepath": requested}
    assert records == []
    # And the walk itself stopped: with a ceiling of 5 entries it can have
    # opened at most 2 directories, nowhere near the 31 an unbounded
    # traversal would visit.
    assert 1 <= len(scanned_dirs) <= 2


def test_repair_deadline_bound_leaves_args_unchanged(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Hitting the wall-clock deadline aborts and leaves the argument untouched."""
    good = tmp_path / "data" / "reference.fasta"
    good.parent.mkdir()
    good.write_text(">chrA\nACGT\n", encoding="utf-8")
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    # A negative budget puts the deadline in the past before the first scan entry.
    monkeypatch.setattr("clio_agent.tools.execution._REPAIR_DEADLINE_S", -1.0)

    requested = str(tmp_path / "typo" / "reference.fasta")
    repaired, records = _repair_missing_file_arguments({"filepath": requested})

    assert repaired == {"filepath": requested}
    assert records == []


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
        set_tool_runtime_fallback(
            ToolRuntimeHooks(
                cancellation_checker=lambda: next(checks, True),
                tool_observer=lambda name, args, phase, error: observed.append(
                    (name, dict(args), phase, error)
                ),
            )
        )

        with pytest.raises(CancellationError, match="tool call cancelled"):
            executor.call_tool("fake_echo", {"value": "late-cancel"})

        assert fake_client.started_call is True
        assert observed[0] == ("fake_echo", {"value": "late-cancel"}, "started", None)
        assert observed[-1][2] == "completed"
        assert observed[-1][3] is not None
        assert "CancellationError" in observed[-1][3]
    finally:
        set_tool_runtime_fallback(ToolRuntimeHooks())
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
    # Build the expected value with pathlib so the OS-native separator is used.
    assert grounded["output_path"] == str(Path("/work/space") / "plot.png")
    # Input path is untouched (not an output-arg name).
    assert grounded["data_path"] == "/data/in.csv"


def test_ground_output_paths_injects_workspace_path_when_omitted():
    """An omitted output arg with a relative schema default is injected absolute."""
    grounded = _ground_output_paths(
        {"data_path": "/data/in.csv"},
        _PLOT_SCHEMA,
        "/work/space",
    )
    assert grounded["output_path"] == str(Path("/work/space") / "timeseries.png")


def test_ground_output_paths_leaves_absolute_emitted_path_untouched(tmp_path):
    """An absolute output path the model emits is preserved verbatim."""
    # Only OS-native absolute paths are recognized as absolute by pathlib on
    # the running platform, so build one from tmp_path rather than a POSIX literal.
    absolute_out = str(tmp_path / "abs.png")
    grounded = _ground_output_paths(
        {"data_path": "/data/in.csv", "output_path": absolute_out},
        _PLOT_SCHEMA,
        "/work/space",
    )
    assert grounded["output_path"] == absolute_out


def test_ground_output_paths_noop_without_workspace_root():
    """Without a bound workspace root the args pass through unchanged."""
    args = {"data_path": "/data/in.csv", "output_path": "plot.png"}
    grounded = _ground_output_paths(args, _PLOT_SCHEMA, "")
    assert grounded == args
    # Omission injection is also gated on the workspace root.
    grounded2 = _ground_output_paths({"data_path": "/data/in.csv"}, _PLOT_SCHEMA, "")
    assert "output_path" not in grounded2


def test_ground_output_paths_ignores_absolute_schema_default(tmp_path):
    """A schema default that is already absolute is not re-grounded on omit."""
    # Use an OS-native absolute default; only those are seen as absolute here.
    schema = {
        "properties": {
            "output_path": {"type": "string", "default": str(tmp_path / "fixed" / "out.png")},
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
    grounded = json.loads(result)["args"]["output_path"]
    assert Path(grounded) == tmp_path / "plot.png"


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
    injected = json.loads(result)["args"]["output_path"]
    assert Path(injected) == tmp_path / "timeseries.png"


def test_sync_mcp_tool_executor_keeps_absolute_output_path(tmp_path):
    """An absolute output_path is passed through unchanged (no regression)."""
    fake_client = PlotLikeClient()
    executor = SyncMCPToolExecutor(
        object(),
        timeout=1.0,
        client_factory=lambda _: fake_client,
    )
    # An OS-native absolute path outside the workspace: only these are seen as
    # absolute by pathlib on the running platform.
    absolute_out = tmp_path.parent / "keep.png"
    try:
        with tool_workspace_context(tmp_path):
            result = executor.call_tool(
                "plot_timeseries",
                {"data_path": "/data/in.csv", "output_path": str(absolute_out)},
            )
    finally:
        executor.close()
    kept = json.loads(result)["args"]["output_path"]
    # The absolute path is passed through unchanged, not re-grounded under the workspace.
    assert Path(kept) == absolute_out
    assert Path(kept) != tmp_path / "keep.png"


def test_notify_tool_observer_failure_logs_reason(caplog):
    """An observer that raises is swallowed but leaves a structured warning (#772)."""
    import logging

    from clio_agent.tools.execution import notify_tool_observer

    def exploding_observer(name, args, phase, error):
        raise ValueError("observer exploded")

    with caplog.at_level(logging.WARNING, logger="clio_agent.tools.execution"):
        notify_tool_observer(exploding_observer, "fake_echo", {"value": "x"}, "end")

    matching = [r for r in caplog.records if "reason=tool_observer_failed" in r.getMessage()]
    assert matching, "expected a structured tool_observer_failed warning"
    assert "tool=fake_echo" in matching[0].getMessage()
    assert "phase=end" in matching[0].getMessage()
