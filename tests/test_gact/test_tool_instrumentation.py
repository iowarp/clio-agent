"""Default-on native tool instrumentation (owner ruling 2026-08-05).

"All tools by default need to be instrumented as a matter of definition."
These tests lock the assembly seam (``gact/agents/tool_instrumentation.py``):

* a BARE native ``dspy.Tool`` registered WITHOUT any manual wrapping, driven
  through the seam, notifies the observer (started + completed with bound
  args / verbatim result) and lands live ``tool_call``/``tool_result`` parts
  (with ``tool_title`` when curated);
* every EXECUTED call emits its ``tool_call``/``tool_result`` parts
  unconditionally (owner ruling, P5 wire semantics) — a declared
  representation may only ADD adornment, never remove the call row. A
  ``representation="chip"`` declaration (create_artifact) notifies (telemetry:
  semantic events + ledger) AND still lands the normal tool parts, plus its
  ``resource_link`` chip separately at turn finalize. ``representation="handoff"``
  is the one exception: its own runtime already emits an ``expert_handoff``
  part that IS call evidence, so the row is skipped (no double-emission);
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
# 1b. Finding A (proven leak): declare_structured_content is consumed at the  #
#     wrapper's OWN call boundary, regardless of what the observer did — an   #
#     observer exception execution.notify_tool_observer swallows, or no      #
#     observer installed at all, must never leave a stale declaration for    #
#     the NEXT unrelated call on this thread to inherit.                      #
# --------------------------------------------------------------------------- #


def test_declared_structured_content_never_leaks_when_observer_raises() -> None:
    """Leak path #2: execution.notify_tool_observer swallows ANY exception the
    observer raises. If that happens BEFORE the observer reaches its own
    pop_declared_structured_content(), the declaration used to survive —
    proven here with the REAL swallowing function, not a reimplementation."""

    from clio_agent.gact.agents.tool_instrumentation import (
        declare_structured_content,
        pop_declared_structured_content,
    )
    from clio_agent.tools import execution as _execution

    def _raising_observer(name, args, phase, error=None, result=None):
        if phase == "completed":
            raise RuntimeError("observer blew up before its own pop")

    def _notify(name, args, phase, error=None, result=None):
        _execution.notify_tool_observer(_raising_observer, name, args, phase, error, result)

    original = _execution.notify_global_tool_observer
    _execution.notify_global_tool_observer = _notify
    try:

        def leaking_tool(task: str) -> str:
            declare_structured_content({"leaked": "should never survive"})
            return f"did {task}"

        (tool,) = instrument_tools([_bare_tool(leaking_tool, "leaking_tool_a")])
        assert tool(task="t1") == "did t1"
        # Without the fix this is the leaked payload; the wrapper's own
        # finally-pop must have already consumed it.
        assert pop_declared_structured_content() is None
    finally:
        _execution.notify_global_tool_observer = original


def test_declared_structured_content_never_leaks_when_observer_is_none() -> None:
    """Leak path #3: no observer installed at all — execution.notify_tool_observer's
    ``if observer is None: return`` means the declaration is never even read."""

    from clio_agent.gact.agents.tool_instrumentation import (
        declare_structured_content,
        pop_declared_structured_content,
    )
    from clio_agent.tools import execution as _execution

    def _notify(name, args, phase, error=None, result=None):
        _execution.notify_tool_observer(None, name, args, phase, error, result)

    original = _execution.notify_global_tool_observer
    _execution.notify_global_tool_observer = _notify
    try:

        def leaking_tool(task: str) -> str:
            declare_structured_content({"leaked": "should never survive"})
            return f"did {task}"

        (tool,) = instrument_tools([_bare_tool(leaking_tool, "leaking_tool_b")])
        assert tool(task="t1") == "did t1"
        assert pop_declared_structured_content() is None
    finally:
        _execution.notify_global_tool_observer = original


def test_declared_structured_content_never_leaks_on_a_raised_tool_call() -> None:
    """The declaration must also not survive a call whose own function raises
    (the wrapper's error branch pops too)."""

    from clio_agent.gact.agents.tool_instrumentation import (
        declare_structured_content,
        pop_declared_structured_content,
    )

    def leaking_then_raising(task: str) -> str:
        declare_structured_content({"leaked": "should never survive"})
        raise RuntimeError("backend gone")

    (tool,) = instrument_tools([_bare_tool(leaking_then_raising, "leaking_tool_c")])
    with pytest.raises(RuntimeError):
        tool(task="t1")
    assert pop_declared_structured_content() is None


def test_stale_declaration_from_before_this_call_never_attaches_to_it(
    tmp_path: Path,
) -> None:
    """The observer's own started-phase clear matters independently from the
    wrapper's finally: a declaration that leaked from BEFORE this call began
    must never be attached to a DIFFERENT, later call that never declared
    anything itself — proven through the REAL observe() chain, not just the
    wrapper's own bookkeeping."""

    app, client, sid = _observing_app(tmp_path)
    try:
        from clio_agent.gact.agents.tool_instrumentation import declare_structured_content

        def plain(task: str) -> str:
            return f"did {task}"  # never declares its own structured content

        (tool,) = instrument_tools(
            [
                native_tool(
                    plain, name="plain_after_leak", desc="d", args={"task": {"type": "string"}}
                )
            ]
        )
        with _gact_app_context(app), _tool_session_context(sid):
            # Simulate a PRIOR call's declaration that leaked past its own
            # cleanup (one of the three proven leak paths) — set BEFORE this
            # call's wrapper ever runs.
            declare_structured_content({"leaked": "from an earlier, unrelated call"})
            assert tool(task="t1") == "did t1"

        parts = (app.state.live_assistant_parts or {}).get(sid, [])
        result_parts = [p for p in parts if p.type == "tool_result"]
        assert result_parts[0].structured_content is None
    finally:
        client.__exit__(None, None, None)


def test_real_p5_tool_declared_structured_content_does_not_leak_across_sequential_calls(
    tmp_path: Path,
) -> None:
    """Extends the machinery proof above onto a REAL P5-declared native tool
    (``goal_status``, the sweep this module's docstring describes for every
    native tool) instead of a synthetic fixture: two sequential calls on the
    SAME thread through the REAL observed pipeline must each carry ONLY their
    OWN declared structured_content -- the second call's Part must never
    inherit the first call's stale value (Finding A, the proven leak)."""

    from clio_agent.gact import context as _ctx
    from clio_agent.gact.goal import arm_goal, build_goal_status_tool

    app, client, sid = _observing_app(tmp_path)
    try:
        (tool,) = instrument_tools([build_goal_status_tool()])
        with _gact_app_context(app), _tool_session_context(sid):
            session_token = _ctx.set_session_id(sid)
            try:
                assert tool()["active"] is False  # first call: no goal armed
                arm_goal(app, sid, condition="ship the report")
                assert tool()["active"] is True  # second call: DIFFERENT facts
            finally:
                _ctx.reset(session_token)

        parts = (app.state.live_assistant_parts or {}).get(sid, [])
        result_parts = [p for p in parts if p.type == "tool_result"]
        assert len(result_parts) == 2
        first, second = result_parts[0].structured_content, result_parts[1].structured_content
        assert first == {
            "message": "no active goal",
            "active": False,
            "condition": "",
            "iters_elapsed": 0,
        }
        assert second is not None and second["message"].startswith("goal active:")
        # Sabotage lock: the second call's declaration is its OWN, not a leaked copy.
        assert second != first
        assert second.get("active") is True
    finally:
        client.__exit__(None, None, None)


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
# 3. Non-"row" representations: a representation may only ADD adornment, it   #
#    may never remove the call row (owner ruling, P5 wire semantics). The ONE #
#    exception is "handoff": its own runtime already emits an expert_handoff  #
#    part that IS call evidence, so the row would be a redundant second       #
#    representation of the same action.                                       #
# --------------------------------------------------------------------------- #


def test_handoff_representation_notifies_but_appends_no_tool_parts(tmp_path: Path) -> None:
    """A declared "handoff" tool notifies the observer (semantic events +
    ledger — the telemetry record) but appends NO live tool_call/tool_result
    parts: its ONE wire representation is the expert_handoff part its own
    runtime emits (spawn_agent_task / spawn_agents_parallel / run_workflow)."""

    app, client, sid = _observing_app(tmp_path)
    try:

        def declared_action(task: str) -> str:
            """Perform a represented-elsewhere action."""
            return f"did {task}"

        (tool,) = instrument_tools(
            [
                native_tool(
                    declared_action,
                    name="declared_handoff",
                    desc=declared_action.__doc__,
                    title="Declared action",
                    representation="handoff",
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
        assert started[0].payload["representation"] == "handoff"
        assert completed[0].payload["representation"] == "handoff"
        # …and the ledger keeps the call (args + result evidence).
        ledger = (app.state.tool_call_ledger or {}).get(sid, [])
        assert [row["name"] for row in ledger] == ["declared_handoff"]
        assert ledger[0]["result"] == "did t1"
        # But NO tool parts reach the live transcript (its own expert_handoff
        # part elsewhere on the wire already IS the call evidence).
        assert _live_part_types(app, sid) == []
    finally:
        client.__exit__(None, None, None)


def test_chip_representation_notifies_and_still_appends_tool_parts(tmp_path: Path) -> None:
    """A declared "chip" tool (create_artifact) gets BOTH: the normal
    tool_call/tool_result row here AND its resource_link chip, appended
    separately at turn finalize (turn_finalize.py, not under test here). The
    chip is adornment on top of the call row, never a replacement for it —
    unlike "handoff", "chip" must NOT suppress the live tool parts.

    Sabotage baseline: before the fix, ``tool_observer._make_tool_observer``'s
    ``observe`` short-circuited on ANY non-"row" representation
    (``if representation != "row": return``), so a "chip" tool's call was
    exactly as invisible on the wire as a "handoff" call — this is the
    defect #1's fix closes. Asserting real tool_call/tool_result parts here
    (not just telemetry) is what would have failed pre-fix."""

    app, client, sid = _observing_app(tmp_path)
    try:

        def declared_action(task: str) -> str:
            """Perform a chip-represented action."""
            return f"did {task}"

        (tool,) = instrument_tools(
            [
                native_tool(
                    declared_action,
                    name="declared_chip",
                    desc=declared_action.__doc__,
                    title="Declared action",
                    representation="chip",
                    args={"task": {"type": "string"}},
                )
            ]
        )
        with _gact_app_context(app), _tool_session_context(sid):
            assert tool(task="t1") == "did t1"

        # Telemetry recorded exactly like handoff…
        history = app.state.bus._history.get(sid, [])
        started = [e for e in history if e.type == "tool.call.started"]
        completed = [e for e in history if e.type == "tool.call.completed"]
        assert len(started) == 1 and len(completed) == 1
        assert started[0].payload["representation"] == "chip"
        assert completed[0].payload["representation"] == "chip"
        ledger = (app.state.tool_call_ledger or {}).get(sid, [])
        assert [row["name"] for row in ledger] == ["declared_chip"]
        # …AND the live transcript still carries the real call row (the fix).
        parts = (app.state.live_assistant_parts or {}).get(sid, [])
        call_parts = [p for p in parts if p.type == "tool_call"]
        result_parts = [p for p in parts if p.type == "tool_result"]
        assert [p.tool_name for p in call_parts] == ["declared_chip"]
        assert call_parts[0].tool_title == "Declared action"
        assert [p.tool_name for p in result_parts] == ["declared_chip"]
        assert result_parts[0].content and result_parts[0].content[0].text == "did t1"
    finally:
        client.__exit__(None, None, None)


# --------------------------------------------------------------------------- #
# 3b. Sabotage: EVERY executed call — every auto-attached tool (enumerated    #
#     from auto_tools.build_auto_react_tools) plus a plain native tool — gets #
#     tool_call evidence, success or error, on the REAL observed-call path.   #
# --------------------------------------------------------------------------- #


def test_every_auto_tool_and_a_plain_tool_lands_a_tool_call_part(tmp_path: Path) -> None:
    """Drive the real react-runtime observed-call path for every tool
    auto-attached to a dynamic react expert (``auto_tools.build_auto_react_tools``:
    create_artifact, plan_exit, write_todos, the cron triad, loop_wakeup,
    goal_status) plus a plain curated native "row" tool, and assert each
    EXECUTED call lands at least one ``tool_call`` part on the live transcript
    — whether the call itself succeeds or raises (the tool_call part is
    appended at the "started" phase, before the underlying function even
    runs, so an eventual exception never erases the call evidence).

    This is the sabotage proof for defect #1 (P5 wire semantics: "every tool
    call emits its tool_call/tool_result parts unconditionally"). Run against
    the pre-fix ``tool_observer.py`` (``if representation != "row": return``
    at both the started and completed phases), this test fails specifically
    on ``create_artifact`` — its "chip" declaration hit that short-circuit and
    the call executed with zero wire evidence, exactly the invisible-call bug
    this module closes. It passes post-fix because only "handoff" still
    short-circuits, and no auto-attached tool declares "handoff"."""

    from clio_agent.gact.agents.auto_tools import build_auto_react_tools

    app, client, sid = _observing_app(tmp_path)
    try:
        agent_def = SimpleNamespace(id="tester")
        auto_tools = {t.name: t for t in instrument_tools(build_auto_react_tools(agent_def))}

        def plain_native() -> str:
            return "ok"

        (row_tool,) = instrument_tools(
            [native_tool(plain_native, name="plain_native", desc="plain", args={})]
        )

        # Minimal args per tool: exercise its real body. Several are expected
        # to RAISE (create_artifact's own missing-input path returns rather
        # than raises; plan_exit is out-of-mode by default) — that is fine and
        # deliberate, since the invariant under test holds regardless of the
        # call's own success/failure.
        calls: dict[str, dict[str, object]] = {
            "create_artifact": {},
            "plan_exit": {"summary": "handing back for approval"},
            "write_todos": {"todos": [{"content": "step 1", "status": "pending"}]},
            "cron_create": {},
            "cron_list": {},
            "cron_delete": {"schedule_id": "does-not-exist"},
            "loop_wakeup": {"stop": True},
            "goal_status": {},
        }
        assert set(calls) == set(auto_tools), (
            "auto_tools.build_auto_react_tools grew/shrank — update this sabotage test's "
            "per-tool call table so every auto-attached tool stays covered"
        )

        exercised: list[str] = []
        with _gact_app_context(app), _tool_session_context(sid):
            for name, kwargs in calls.items():
                try:
                    auto_tools[name](**kwargs)
                except Exception:
                    pass  # only tool_call/tool_result wire evidence is under test
                exercised.append(name)
            row_tool()
            exercised.append("plain_native")

        parts = (app.state.live_assistant_parts or {}).get(sid, [])
        called_names = {p.tool_name for p in parts if p.type == "tool_call"}
        missing = [name for name in exercised if name not in called_names]
        assert not missing, f"executed calls with no tool_call part: {missing}"
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


def test_mcp_bridge_carries_the_upstream_tools_declared_title() -> None:
    """#1188 MCP half: when the upstream MCP tool declares a ``title``
    (mcp.types.Tool.title), the execution-boundary bridge stamps it, sanitized
    through the SAME sanitizer curated native titles use, so it registers at
    the assembly seam and would ride onto ``Part.tool_title``."""

    from clio_agent.tools.execution import _make_dspy_tool

    mcp_tool = SimpleNamespace(
        description="rank stations",
        title="Rank\nstations\x00",
        inputSchema={"properties": {}},
    )
    tool = _make_dspy_tool("earthscope_rank_stations", mcp_tool, lambda name, kwargs: "")
    (instrumented,) = instrument_tools([tool])
    assert declared_tool_title("earthscope_rank_stations") == "Rank stations"
    assert declared_tool_representation("earthscope_rank_stations") == "row"
    assert instrumented is tool  # unchanged: already marked observed


def test_mcp_bridge_never_invents_a_title_when_upstream_declares_none() -> None:
    """A server that declares no ``title`` leaves the field absent (raw name
    renders) — the bridge must never fabricate one."""

    from clio_agent.tools.execution import _make_dspy_tool

    mcp_tool = SimpleNamespace(description="d", inputSchema={"properties": {}})
    tool = _make_dspy_tool("fs_untitled", mcp_tool, lambda name, kwargs: "")
    instrument_tools([tool])
    assert declared_tool_title("fs_untitled") == ""


def test_mcp_tool_title_prefers_tool_title_over_annotations_title() -> None:
    """#1188 annotations fallback: ``Tool.title`` WINS when both are present,
    for either the object-shaped (execution.py) or dict-row-shaped
    (builders.py) seam."""

    from clio_agent.gact.agents.tool_instrumentation import mcp_tool_title

    obj = SimpleNamespace(
        title="Top-level Title", annotations=SimpleNamespace(title="Annotations Title")
    )
    assert mcp_tool_title(obj) == "Top-level Title"

    row = {"title": "Top-level Title", "annotations": {"title": "Annotations Title"}}
    assert mcp_tool_title(row) == "Top-level Title"


def test_mcp_tool_title_falls_back_to_annotations_title_when_tool_title_absent() -> None:
    """The shape clio-kit actually populates today (hdf5/arxiv/plot/web declare
    ``annotations={"title": ...}``, never the top-level ``Tool.title``): the
    fallback unlocks it for both the object and dict-row seam shapes."""

    from clio_agent.gact.agents.tool_instrumentation import mcp_tool_title

    obj = SimpleNamespace(title=None, annotations=SimpleNamespace(title="Open HDF5 File"))
    assert mcp_tool_title(obj) == "Open HDF5 File"

    obj_no_title_attr = SimpleNamespace(annotations=SimpleNamespace(title="Open HDF5 File"))
    assert mcp_tool_title(obj_no_title_attr) == "Open HDF5 File"

    row = {"title": "", "annotations": {"title": "Open HDF5 File"}}
    assert mcp_tool_title(row) == "Open HDF5 File"


def test_mcp_tool_title_both_absent_stays_empty() -> None:
    """Never invented: no ``Tool.title`` and no ``ToolAnnotations.title`` ->
    ``""``, across every shape either field could be missing in."""

    from clio_agent.gact.agents.tool_instrumentation import mcp_tool_title

    assert mcp_tool_title(SimpleNamespace()) == ""
    assert mcp_tool_title(SimpleNamespace(annotations=None)) == ""
    assert mcp_tool_title(SimpleNamespace(annotations=SimpleNamespace(title=None))) == ""
    assert mcp_tool_title({}) == ""
    assert mcp_tool_title({"annotations": {}}) == ""
    assert mcp_tool_title({"annotations": None}) == ""


def test_mcp_bridge_annotations_title_rides_to_declared_title_when_tool_title_absent() -> None:
    """End-to-end through the execution-boundary bridge: a clio-kit-shaped tool
    (``ToolAnnotations.title`` only, no ``Tool.title``) still curates
    ``Part.tool_title`` — this is the fleet unlock (hdf5/arxiv/plot/web, 41
    occurrences) with zero changes required on clio-kit's side."""

    from clio_agent.tools.execution import _make_dspy_tool

    mcp_tool = SimpleNamespace(
        description="open an hdf5 file",
        title=None,
        annotations=SimpleNamespace(title="Open HDF5 File"),
        inputSchema={"properties": {}},
    )
    tool = _make_dspy_tool("hdf5_open_file", mcp_tool, lambda name, kwargs: "")
    instrument_tools([tool])
    assert declared_tool_title("hdf5_open_file") == "Open HDF5 File"


def test_external_mcp_tool_row_annotations_title_fallback() -> None:
    """The external-MCP builder seam (``builders._enabled_external_mcp_dspy_tools``
    resolves via ``mcp_tool_title(tool_row)``): a persisted row carrying only an
    annotations-dict title (the shape ``_normalize_mcp_tool_annotations``
    produces in blueprints.py) still curates a title through
    ``boundary_observed_tool``."""

    from clio_agent.gact.agents.tool_instrumentation import boundary_observed_tool, mcp_tool_title

    tool_row = {
        "name": "hdf5_open_file",
        "title": "",
        "annotations": {"title": "Open HDF5 File", "readOnlyHint": False},
    }

    def f() -> str:
        return ""

    tool = boundary_observed_tool(
        f, name="hdf5_open_file", desc="d", args={}, title=mcp_tool_title(tool_row)
    )
    instrument_tools([tool])
    assert declared_tool_title("hdf5_open_file") == "Open HDF5 File"


def test_boundary_observed_tool_curates_a_title_when_given() -> None:
    """The agent-blueprint external-MCP bridge (``builders``) can hand
    ``boundary_observed_tool`` a curated/upstream title; it registers exactly
    like a native curated title and is sanitized the same way."""

    from clio_agent.gact.agents.tool_instrumentation import boundary_observed_tool

    def f() -> str:
        return ""

    tool = boundary_observed_tool(f, name="ext_rank", desc="d", args={}, title="Rank\tstations")
    instrument_tools([tool])
    assert declared_tool_title("ext_rank") == "Rank stations"

    def g() -> str:
        return ""

    untitled = boundary_observed_tool(g, name="ext_untitled", desc="d", args={})
    instrument_tools([untitled])
    assert declared_tool_title("ext_untitled") == ""


def test_recording_rewrap_propagates_the_observed_marker() -> None:
    """The blueprint recording wrapper re-constructs the tool around a new
    callable; the observed marker must survive (rebuilt_tool) or a bridged
    tool would notify twice after the seam. The curated title marker must
    survive the same re-wrap (#1188 MCP half)."""

    from clio_agent.gact.agents.builders import _recording_blueprint_tool
    from clio_agent.tools.execution import _make_dspy_tool

    mcp_tool = SimpleNamespace(description="d", title="List files", inputSchema={"properties": {}})
    bridged = _make_dspy_tool("fs_list", mcp_tool, lambda name, kwargs: "listed")
    recorded = _recording_blueprint_tool(bridged)
    assert getattr(recorded.func, TOOL_OBSERVED_ATTR, False) is True

    recorded_func = recorded.func
    (instrumented,) = instrument_tools([recorded])
    assert instrumented.func is recorded_func  # marker honored through the re-wrap
    assert declared_tool_title("fs_list") == "List files"  # title marker also survives

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
    assert declared_tool_title("create_artifact") == "Create Artifact"
    for name, title in [
        ("plan_exit", "Exit Plan"),
        ("write_todos", "Write Todos"),
        ("cron_create", "Create Cron"),
        ("cron_list", "List Crons"),
        ("cron_delete", "Delete Cron"),
        ("loop_wakeup", "Loop Wakeup"),
        ("goal_status", "Goal Status"),
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
        "spawn_agent_task": ("handoff", "Spawn Agent"),
        "spawn_agents_parallel": ("handoff", "Spawn Agents"),
        "wait_agent_tasks": ("row", "Wait"),
        "check_agent_tasks": ("row", "Check Tasks"),
        "observe_agent_tasks": ("row", "Observe"),
        "message_agent": ("row", "Message Agent"),
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
# 6b. No registered title contains a paren (owner correction 2026-08-06): a   #
#     curated title is a plain human name — the UI injects the               #
#     parenthesized call arguments itself, so a title must never smuggle its #
#     own parens back in.                                                     #
# --------------------------------------------------------------------------- #


def test_no_registered_native_title_contains_a_paren(tmp_path: Path, monkeypatch) -> None:
    """Sweeps every curated native/auto/spawn-runtime title through the real
    builder functions (not a source-grep) and asserts none carries parens.

    Sabotage baseline: before the 2026-08-06 rename this failed on
    ``spawn_agent_task``/``wait_agent_tasks``/``check_agent_tasks``/
    ``spawn_agents_parallel``/``run_workflow``/``create_artifact``/
    ``observe_agent_tasks``/``message_agent``/``plan_exit``/``goal_status``/
    ``load_skill``/``cron_create``/``cron_list``/``cron_delete`` — every one
    of them baked a ``verb(object)`` shape into the title."""

    from clio_agent.gact.agents import spawn_runtime
    from clio_agent.gact.agents.auto_tools import build_auto_react_tools
    from clio_agent.gact.agents.skill_runtime import SkillRuntime, build_load_skill_tool

    app = build_app(sessions_path=tmp_path / "s.json")
    monkeypatch.setattr(
        "clio_agent.gact.agents.resolution._runtime_declared_child_ids",
        lambda a, pid, session_id="": {"child_a"},
    )
    tools = build_auto_react_tools(SimpleNamespace(id="tester"))
    tools.append(build_load_skill_tool(SimpleNamespace(id="tester"), SkillRuntime()))
    with TestClient(app), _gact_app_context(app), _tool_session_context("sess_x"):
        tools += spawn_runtime.build_spawn_runtime_tools(
            SimpleNamespace(),
            SimpleNamespace(id="main", metadata={"agent_blueprint_id": "bp"}),
        )
    instrument_tools(tools)
    assert len(tools) >= 12, "expected the full curated native/auto/spawn-runtime set"
    for tool in tools:
        title = declared_tool_title(tool.name)
        assert "(" not in title and ")" not in title, (tool.name, title)


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
