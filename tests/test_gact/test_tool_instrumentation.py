"""Default-on native tool instrumentation (owner ruling 2026-08-05).

"All tools by default need to be instrumented as a matter of definition."
These tests lock the assembly seam (``gact/agents/tool_instrumentation.py``):

* a BARE native ``dspy.Tool`` registered WITHOUT any manual wrapping, driven
  through the seam, notifies the observer (started + completed with bound
  args / verbatim result) and lands live ``tool_call``/``tool_result`` parts
  (with ``tool_title`` when curated);
* a ``representation="handoff"`` / ``"chip"`` declaration notifies (telemetry:
  semantic events + ledger) but appends NO tool parts — one representation per
  action on the wire, explicit at definition;
* MCP-bridged tools (constructed by the execution-boundary bridge) notify
  exactly ONCE — the seam never double-wraps a marked callable, including
  through the blueprint recording re-wrap;
* the CI guard (``scripts/check_tool_instrumentation.py``) fails on a
  synthetic bare ``dspy.Tool`` fixture and passes the sanctioned files.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import dspy
import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.agents.tool_instrumentation import (
    declared_tool_representation,
    declared_tool_title,
    instrument_tools,
    native_tool,
    sanitize_tool_title,
)
from clio_agent.gact.app import build_app
from clio_agent.gact.runtime.globals import _gact_app_context, _tool_session_context
from clio_agent.gact.tool_observer import _install_tool_runtime_hooks
from clio_agent.tools import execution as _execution
from clio_agent.tools.execution import TOOL_OBSERVED_ATTR
from scripts.check_tool_instrumentation import check_tool_instrumentation

pytestmark = pytest.mark.usefixtures("host_agent_executor")


# --------------------------------------------------------------------------- #
# Helpers.                                                                     #
# --------------------------------------------------------------------------- #


def _bare_tool(func, name: str):
    """A bare dspy.Tool exactly as an unmigrated/future call site would build it."""

    return dspy.Tool(func=func, name=name, desc=func.__doc__ or name)


def _observing_app(tmp_path: Path):
    """A built app with its per-app tool-runtime hooks installed + one session."""

    app = build_app(sessions_path=tmp_path / "s.json")
    client = TestClient(app)
    client.__enter__()
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    _install_tool_runtime_hooks(app)
    return app, client, sid


def _live_part_types(app, sid: str) -> list[tuple[str, str]]:
    parts = (getattr(app.state, "live_assistant_parts", {}) or {}).get(sid, [])
    return [(p.type, str(p.tool_name or "")) for p in parts]


# --------------------------------------------------------------------------- #
# 1. A bare native tool through the seam is observed by definition.            #
# --------------------------------------------------------------------------- #


def test_bare_native_tool_through_seam_notifies_started_and_completed() -> None:
    """No manual shim anywhere: the seam alone makes a bare tool notify with
    its bound args and verbatim result, and surfaces the error on raise."""

    calls: list[tuple[str, dict, str, str | None, object]] = []

    def _capture(name, args, phase, error=None, result=None):
        calls.append((name, args, phase, error, result))

    original = _execution.notify_global_tool_observer
    _execution.notify_global_tool_observer = _capture
    try:

        def frobnicate(dataset: str, limit: int = 10) -> str:
            """Frobnicate a dataset."""
            return f"frobnicated {dataset} x{limit}"

        (tool,) = instrument_tools([_bare_tool(frobnicate, "frobnicate")])
        # The seam wrapped the callable (a bare tool is never left invisible)…
        assert getattr(tool.func, TOOL_OBSERVED_ATTR, False) is True
        out = tool(dataset="d1")
        assert out == "frobnicated d1 x10"
        assert [(c[0], c[2]) for c in calls] == [
            ("frobnicate", "started"),
            ("frobnicate", "completed"),
        ]
        assert calls[0][1] == {"dataset": "d1", "limit": 10}  # bound args, defaults applied
        assert calls[1][4] == "frobnicated d1 x10"  # verbatim result

        calls.clear()

        def boom() -> str:
            raise RuntimeError("backend gone")

        (boom_tool,) = instrument_tools([_bare_tool(boom, "boom")])
        with pytest.raises(RuntimeError):
            boom_tool()
        assert [(c[0], c[2], c[3]) for c in calls] == [
            ("boom", "started", None),
            ("boom", "completed", "backend gone"),
        ]
    finally:
        _execution.notify_global_tool_observer = original


def test_seam_is_idempotent() -> None:
    """Instrumenting twice never double-wraps (the marker is the idempotence)."""

    def probe() -> str:
        return "ok"

    (tool,) = instrument_tools([_bare_tool(probe, "probe")])
    wrapped_once = tool.func
    (tool,) = instrument_tools([tool])
    assert tool.func is wrapped_once


# --------------------------------------------------------------------------- #
# 2. Full chain: seam -> notify -> per-app observer -> live parts + title.     #
# --------------------------------------------------------------------------- #


def test_row_tool_lands_tool_parts_with_curated_title(tmp_path: Path) -> None:
    """A curated native tool driven through the REAL runtime chain lands a
    ``tool_call`` part carrying ``tool_title`` and a ``tool_result`` part."""

    app, client, sid = _observing_app(tmp_path)
    try:

        def rank_stations(city: str) -> str:
            """Rank stations for a city."""
            return f"ranked {city}"

        (tool,) = instrument_tools(
            [
                native_tool(
                    rank_stations,
                    name="rank_stations",
                    desc=rank_stations.__doc__,
                    title="Rank stations",
                    args={"city": {"type": "string"}},
                )
            ]
        )
        with _gact_app_context(app), _tool_session_context(sid):
            assert tool(city="LA") == "ranked LA"

        parts = (app.state.live_assistant_parts or {}).get(sid, [])
        call_parts = [p for p in parts if p.type == "tool_call"]
        result_parts = [p for p in parts if p.type == "tool_result"]
        assert [p.tool_name for p in call_parts] == ["rank_stations"]
        assert call_parts[0].tool_title == "Rank stations"
        assert call_parts[0].input == {"city": "LA"}
        assert [p.tool_name for p in result_parts] == ["rank_stations"]
        assert result_parts[0].content and result_parts[0].content[0].text == "ranked LA"
        # tool_title rides the wire projection (omitempty keeps it only when set).
        assert call_parts[0].to_wire().get("tool_title") == "Rank stations"

        types = [e.type for e in app.state.bus._history.get(sid, [])]
        assert "tool.call.started" in types and "tool.call.completed" in types
    finally:
        client.__exit__(None, None, None)


def test_uncurated_bare_tool_lands_parts_without_title(tmp_path: Path) -> None:
    """A bare (undeclared) tool defaults to representation="row" with no title."""

    app, client, sid = _observing_app(tmp_path)
    try:

        def plain() -> str:
            return "done"

        (tool,) = instrument_tools([_bare_tool(plain, "plain")])
        with _gact_app_context(app), _tool_session_context(sid):
            tool()
        parts = (app.state.live_assistant_parts or {}).get(sid, [])
        call_parts = [p for p in parts if p.type == "tool_call"]
        assert [p.tool_name for p in call_parts] == ["plain"]
        assert call_parts[0].tool_title == ""
        assert "tool_title" not in call_parts[0].to_wire()  # omitempty
    finally:
        client.__exit__(None, None, None)


# --------------------------------------------------------------------------- #
# 3. Non-"row" representations: telemetry yes, tool parts no.                  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("representation", ["handoff", "chip"])
def test_non_row_representation_notifies_but_appends_no_tool_parts(
    tmp_path: Path, representation: str
) -> None:
    """A declared handoff/chip tool notifies the observer (semantic events +
    ledger — the telemetry record) but appends NO live tool_call/tool_result
    parts: its ONE wire representation is the expert_handoff / resource_link
    part its own runtime emits."""

    app, client, sid = _observing_app(tmp_path)
    try:

        def declared_action(task: str) -> str:
            """Perform a represented-elsewhere action."""
            return f"did {task}"

        (tool,) = instrument_tools(
            [
                native_tool(
                    declared_action,
                    name=f"declared_{representation}",
                    desc=declared_action.__doc__,
                    title="Declared action",
                    representation=representation,
                    args={"task": {"type": "string"}},
                )
            ]
        )
        with _gact_app_context(app), _tool_session_context(sid):
            assert tool(task="t1") == "did t1"

        # Telemetry recorded: bus lifecycle events carry the declared representation…
        history = app.state.bus._history.get(sid, [])
        started = [e for e in history if e.type == "tool.call.started"]
        completed = [e for e in history if e.type == "tool.call.completed"]
        assert len(started) == 1 and len(completed) == 1
        assert started[0].payload["representation"] == representation
        assert completed[0].payload["representation"] == representation
        # …and the ledger keeps the call (args + result evidence).
        ledger = (app.state.tool_call_ledger or {}).get(sid, [])
        assert [row["name"] for row in ledger] == [f"declared_{representation}"]
        assert ledger[0]["result"] == "did t1"
        # But NO tool parts reach the live transcript (one representation per action).
        assert _live_part_types(app, sid) == []
    finally:
        client.__exit__(None, None, None)


# --------------------------------------------------------------------------- #
# 4. MCP-bridged tools notify exactly once (no seam double-wrap).              #
# --------------------------------------------------------------------------- #


def test_mcp_bridged_tool_is_not_double_wrapped() -> None:
    """The bridge (execution._make_dspy_tool) marks its callables observed —
    they notify inside the boundary's call path — so the seam must leave them
    untouched: exactly ONE notification path per call."""

    from clio_agent.tools.execution import _make_dspy_tool

    boundary_notifications: list[str] = []

    def call_tool(name, kwargs):
        # Stands in for SyncMCPToolExecutor.call_tool, which notifies the
        # observer itself; count those boundary notifications.
        boundary_notifications.append(name)
        return "boundary result"

    mcp_tool = SimpleNamespace(description="read a file", inputSchema={"properties": {}})
    tool = _make_dspy_tool("fs_read_file", mcp_tool, call_tool)
    assert getattr(tool.func, TOOL_OBSERVED_ATTR, False) is True

    seam_notifications: list[tuple[str, str]] = []

    def _capture(name, args, phase, error=None, result=None):
        seam_notifications.append((name, phase))

    original = _execution.notify_global_tool_observer
    _execution.notify_global_tool_observer = _capture
    try:
        bridged_func = tool.func
        (tool,) = instrument_tools([tool])
        # Not wrapped: same callable, and calling it adds ZERO seam notifications.
        assert tool.func is bridged_func
        assert tool() == "boundary result"
        assert seam_notifications == []
        assert boundary_notifications == ["fs_read_file"]
    finally:
        _execution.notify_global_tool_observer = original


def test_recording_rewrap_propagates_the_observed_marker() -> None:
    """The blueprint recording wrapper re-constructs the tool around a new
    callable; the observed marker must survive (rebuilt_tool) or a bridged
    tool would notify twice after the seam."""

    from clio_agent.gact.agents.builders import _recording_blueprint_tool
    from clio_agent.tools.execution import _make_dspy_tool

    mcp_tool = SimpleNamespace(description="d", inputSchema={"properties": {}})
    bridged = _make_dspy_tool("fs_list", mcp_tool, lambda name, kwargs: "listed")
    recorded = _recording_blueprint_tool(bridged)
    assert getattr(recorded.func, TOOL_OBSERVED_ATTR, False) is True

    recorded_func = recorded.func
    (instrumented,) = instrument_tools([recorded])
    assert instrumented.func is recorded_func  # marker honored through the re-wrap

    # Sabotage twin: an UNMARKED callable in the same shape IS wrapped.
    def unmarked() -> str:
        return "x"

    bare = _bare_tool(unmarked, "unmarked")
    (wrapped,) = instrument_tools([bare])
    assert wrapped.func is not unmarked
    assert getattr(wrapped.func, TOOL_OBSERVED_ATTR, False) is True


# --------------------------------------------------------------------------- #
# 5. Declared-presentation registry: the shipped declarations.                 #
# --------------------------------------------------------------------------- #


def test_auto_react_tools_carry_their_declared_presentation() -> None:
    """The auto-attached set declares through the factory: create_artifact is
    the chip; the rest are curated rows."""

    from clio_agent.gact.agents.auto_tools import build_auto_react_tools

    instrument_tools(build_auto_react_tools(SimpleNamespace(id="tester")))
    assert declared_tool_representation("create_artifact") == "chip"
    assert declared_tool_title("create_artifact") == "Create artifact"
    for name, title in [
        ("plan_exit", "Exit plan mode"),
        ("write_todos", "Update todo list"),
        ("cron_create", "Schedule future turn"),
        ("cron_list", "List scheduled turns"),
        ("cron_delete", "Cancel scheduled turn"),
        ("loop_wakeup", "Continue or stop loop"),
        ("goal_status", "Check goal status"),
    ]:
        assert declared_tool_representation(name) == "row", name
        assert declared_tool_title(name) == title, name


def test_spawn_runtime_tools_declare_handoff_for_spawn_and_row_for_collectors(
    tmp_path: Path, monkeypatch
) -> None:
    """spawn/fan-out present as their expert_handoff part (declared handoff);
    the collectors + observe/message are real rows with curated titles."""

    from clio_agent.gact.agents import spawn_runtime

    app = build_app(sessions_path=tmp_path / "s.json")
    monkeypatch.setattr(
        "clio_agent.gact.agents.resolution._runtime_declared_child_ids",
        lambda a, pid, session_id="": {"child_a"},
    )
    with TestClient(app), _gact_app_context(app), _tool_session_context("sess_x"):
        tools = spawn_runtime.build_spawn_runtime_tools(
            SimpleNamespace(),
            SimpleNamespace(id="main", metadata={"agent_blueprint_id": "bp"}),
        )
    instrument_tools(tools)
    expected = {
        "spawn_agent_task": ("handoff", "Spawn agent"),
        "spawn_agents_parallel": ("handoff", "Spawn agents in parallel"),
        "wait_agent_tasks": ("row", "Wait for agents"),
        "check_agent_tasks": ("row", "Check agent tasks"),
        "observe_agent_tasks": ("row", "Observe agent tasks"),
        "message_agent": ("row", "Message agent"),
    }
    assert {t.name for t in tools} == set(expected)
    for name, (representation, title) in expected.items():
        assert declared_tool_representation(name) == representation, name
        assert declared_tool_title(name) == title, name


def test_invalid_representation_is_a_typed_error() -> None:
    """An unknown representation fails LOUDLY at declaration (never coerced)."""

    def f() -> str:
        return ""

    with pytest.raises(ValueError, match="unknown representation"):
        native_tool(f, name="f", desc="", args={}, title="", representation="banner")


# --------------------------------------------------------------------------- #
# 6. Title sanitization (untrusted-input discipline for our own strings).      #
# --------------------------------------------------------------------------- #


def test_title_sanitization_strips_controls_and_clamps() -> None:
    assert sanitize_tool_title("Wait for agents") == "Wait for agents"
    assert sanitize_tool_title("Spawn\nagent\r\x00\x1b[31m") == "Spawn agent [31m"
    assert sanitize_tool_title("  spaced\t\ttitle  ") == "spaced title"
    assert sanitize_tool_title(None) == ""
    assert len(sanitize_tool_title("x" * 500)) == 80


# --------------------------------------------------------------------------- #
# 7. The CI guard: baseline 0 on bare constructions.                           #
# --------------------------------------------------------------------------- #


def test_guard_fails_on_a_synthetic_bare_dspy_tool(tmp_path: Path) -> None:
    scan_root = tmp_path / "src"
    scan_root.mkdir()
    (scan_root / "sneaky.py").write_text(
        "import dspy\n\n\ndef build():\n    return dspy.Tool(func=len, name='sneaky')\n",
        encoding="utf-8",
    )
    (scan_root / "aliased.py").write_text(
        "from dspy import Tool as T\n\n\ndef build():\n    return T(len, name='aliased')\n",
        encoding="utf-8",
    )
    violations = check_tool_instrumentation(scan_root, rel_to=tmp_path)
    assert {(v.rel, v.line) for v in violations} == {("src/sneaky.py", 5), ("src/aliased.py", 5)}

    # A sanctioned file with the same construction passes.
    sanctioned = check_tool_instrumentation(
        scan_root, rel_to=tmp_path, sanctioned=frozenset({"src/sneaky.py", "src/aliased.py"})
    )
    assert sanctioned == []


def test_guard_passes_the_real_tree() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    assert check_tool_instrumentation(repo_root / "src" / "clio_agent", rel_to=repo_root) == []
