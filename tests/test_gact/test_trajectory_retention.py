"""Tests for S4 trajectory retention + prediction-summary enrichment.

These guard the load-bearing pieces of the canonical-trace capture: the
dspy.ReAct subclass that publishes its trajectory BEFORE the final extract
(so a failed extract still exposes it for capture + re-extract repair), and
the enrichment of llm.response.completed payloads with trajectory + reasoning.
"""

import dspy
import pytest

from clio_agent.gact import app as gact_app
from clio_agent.gact import context as ctx


class _AttrDict(dict):
    """dict whose keys are also attribute-accessible (mimics dspy.Prediction)."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


class _SinkArc:
    """Minimal ARC-as-source stub: forwards each recorded event to the sink.

    ARC is the source of the highway; ``_emit_semantic_event`` routes every event
    through ``arc.record_semantic_event`` (which, in production, records ARC's view and
    DERIVES the highway via the sink wired by ``_set_app_arc``). These tests build a bare
    app to drive ``_emit_semantic_event`` through a real sink; this stub stands in for ARC
    and forwards to that sink, satisfying the fail-loud ARC-reachability check.
    """

    def __init__(self, sink):
        self._sink = sink

    def record_semantic_event(self, event):
        return self._sink.emit(event)


def test_repair_temperature_constant_no_drift():
    # Retries REUSE the base temp (cache-off resampling already varies them; bumping
    # temp would raise format drift, which the parse-error class can't tolerate).
    assert gact_app._repair_temperature(0.6, 0) == 0.6
    assert gact_app._repair_temperature(0.6, 1) == 0.6  # no bump
    assert gact_app._repair_temperature(0.6, 3) == 0.6  # no bump, no escalation
    # The ONLY lift is off greedy temp 0 (else every retry is identical/deterministic).
    assert gact_app._repair_temperature(0.0, 0) == 0.0  # original stays greedy
    assert gact_app._repair_temperature(0.0, 1) == 0.5  # retry samples at a floor
    assert gact_app._repair_temperature(0.0, 3) == 0.5


def test_extract_repair_attempts_default_and_env(monkeypatch):
    monkeypatch.delenv("CLIO_EXTRACT_REPAIR_ATTEMPTS", raising=False)
    assert gact_app._extract_repair_attempts() == 3
    monkeypatch.setenv("CLIO_EXTRACT_REPAIR_ATTEMPTS", "5")
    assert gact_app._extract_repair_attempts() == 5
    monkeypatch.setenv("CLIO_EXTRACT_REPAIR_ATTEMPTS", "0")
    assert gact_app._extract_repair_attempts() == 0


def test_repair_hint_keeps_both_ends_and_shows_output():
    # A long AdapterParseError: the model's echoed response (head) + the actionable
    # field diff (tail). The hint must surface BOTH and frame it as "what you produced".
    head = "Adapter ChatAdapter failed to parse the LM response. LM Response: " + "X" * 1500
    tail = "Expected to find output fields: [station_ids]. Actual: []."
    exc = ValueError(head + " " + tail)
    hint = gact_app._typed_output_repair_hint(exc)
    assert "what you produced and why it was rejected" in hint
    assert "Expected to find output fields" in hint  # tail survived truncation
    assert "Adapter ChatAdapter failed to parse" in hint  # head survived too
    assert "[…]" in hint  # middle elided, both ends kept


def test_retaining_react_is_react_subclass():
    cls = gact_app._retaining_react_cls()
    assert issubclass(cls, dspy.ReAct)
    # Cached: same class object on repeat calls.
    assert gact_app._retaining_react_cls() is cls


def test_retaining_react_publishes_trajectory_before_failed_extract():
    cls = gact_app._retaining_react_cls()
    inst = object.__new__(cls)  # bypass dspy.ReAct.__init__ (no LM needed)
    inst.max_iters = 3
    inst.react = object()
    inst.extract = object()
    inst.tools = {"finish": lambda: "Completed."}

    def fake_trunc(module, trajectory, **input_args):
        if module is inst.react:
            return _AttrDict(next_thought="t", next_tool_name="finish", next_tool_args={})
        # extract step fails the way a dropped-required-field model does
        raise ValueError("missing required field: station_ids")

    inst._call_with_potential_trajectory_truncation = fake_trunc

    ctx.install_trajectory_cell()
    with pytest.raises(ValueError):
        inst.forward(question="find stations near San Diego")

    retained = ctx.active_trajectory()
    assert retained is not None
    # input_args are retained so extract can be re-run over the trajectory.
    assert retained["input_args"]["question"] == "find stations near San Diego"
    # the finish step was recorded in the trajectory before extract ran
    assert any(k.startswith("tool_name_") for k in retained["trajectory"])


def test_retaining_react_success_returns_trajectory_in_prediction():
    cls = gact_app._retaining_react_cls()
    inst = object.__new__(cls)
    inst.max_iters = 3
    inst.react = object()
    inst.extract = object()
    inst.tools = {"finish": lambda: "Completed."}

    def fake_trunc(module, trajectory, **input_args):
        if module is inst.react:
            return _AttrDict(next_thought="t", next_tool_name="finish", next_tool_args={})
        return _AttrDict(answer="done", reasoning="because")

    inst._call_with_potential_trajectory_truncation = fake_trunc

    ctx.install_trajectory_cell()
    result = inst.forward(question="q")
    assert result.answer == "done"
    assert result.trajectory  # trajectory threaded into the Prediction


def test_retaining_react_emits_full_per_step_on_highway(monkeypatch):
    """Every ReAct Step (LLM response thought + tool act/observe) rides the highway,
    full and uncapped, attributed to its expert and grouped by one span."""
    cls = gact_app._retaining_react_cls()
    inst = object.__new__(cls)
    inst.max_iters = 5
    inst.react = object()
    inst.extract = object()
    big_obs = {"rows": ["A", "B"], "note": "x" * 5000}
    inst.tools = {
        "ndp_search_datasets": lambda **kw: big_obs,
        "finish": lambda: "Completed.",
    }
    inst._clio_expert_id = "ndp_dataset_discovery"

    steps = [
        _AttrDict(
            next_thought="I should search NDP",
            next_tool_name="ndp_search_datasets",
            next_tool_args={"query": "gnss"},
        ),
        _AttrDict(next_thought="Done searching", next_tool_name="finish", next_tool_args={}),
    ]
    seq = {"i": 0}

    def fake_trunc(module, trajectory, **input_args):
        if module is inst.react:
            step = steps[seq["i"]]
            seq["i"] += 1
            return step
        return _AttrDict(answer="final report", reasoning="r")

    inst._call_with_potential_trajectory_truncation = fake_trunc

    emitted: list[tuple[str, dict]] = []

    def fake_emit(app, sid, event_type, **kw):
        emitted.append((event_type, kw))
        return {}

    monkeypatch.setattr(gact_app, "_emit_semantic_event", fake_emit)

    ctx.set_app(object())
    ctx.set_session_id("sess_test")
    ctx.install_trajectory_cell()
    # Teardown via the autouse _reset_runtime_context fixture (tokenless sets).
    result = inst.forward(question="find stations near San Diego")

    step_events = [kw["payload"] for (et, kw) in emitted if et == "react.step.completed"]
    assert len(step_events) == 2  # one per ReAct Step, including the finish step

    p0 = step_events[0]
    assert p0["expert_id"] == "ndp_dataset_discovery"
    assert p0["step_index"] == 0
    assert p0["thought"] == "I should search NDP"
    assert p0["tool_name"] == "ndp_search_datasets"
    assert p0["tool_args"] == {"query": "gnss"}
    assert "x" * 5000 in str(p0["observation"])  # FULL observation, not capped
    assert p0["is_finish"] is False

    p1 = step_events[1]
    assert p1["step_index"] == 1
    assert p1["tool_name"] == "finish"
    assert p1["is_finish"] is True

    # All steps of one expert lifecycle share a span so a trajectory is groupable.
    assert p0["expert_span_id"] == p1["expert_span_id"]
    assert result.answer == "final report"


def test_retaining_react_step_capture_is_best_effort(monkeypatch):
    """A highway-emit failure must never break the expert loop."""
    cls = gact_app._retaining_react_cls()
    inst = object.__new__(cls)
    inst.max_iters = 3
    inst.react = object()
    inst.extract = object()
    inst.tools = {"finish": lambda: "Completed."}
    inst._clio_expert_id = "x"

    def fake_trunc(module, trajectory, **input_args):
        if module is inst.react:
            return _AttrDict(next_thought="t", next_tool_name="finish", next_tool_args={})
        return _AttrDict(answer="ok")

    inst._call_with_potential_trajectory_truncation = fake_trunc

    def boom(*a, **k):
        raise RuntimeError("sink exploded")

    monkeypatch.setattr(gact_app, "_emit_semantic_event", boom)
    ctx.set_app(object())
    ctx.set_session_id("sess_test")
    ctx.install_trajectory_cell()
    # Teardown via the autouse _reset_runtime_context fixture (tokenless sets).
    result = inst.forward(question="q")  # must not raise despite the sink blowing up
    assert result.answer == "ok"


def test_retaining_react_emits_expert_lifecycle_boundary(monkeypatch):
    """The expert lifecycle (start + extract output to parent) rides the highway."""
    cls = gact_app._retaining_react_cls()
    inst = object.__new__(cls)
    inst.max_iters = 3
    inst.react = object()
    inst.extract = object()
    inst.tools = {"finish": lambda: "done"}
    inst._clio_expert_id = "geospatial"

    def fake_trunc(module, trajectory, **input_args):
        if module is inst.react:
            return _AttrDict(next_thought="t", next_tool_name="finish", next_tool_args={})
        return _AttrDict(answer="the full region report", reasoning="r")

    inst._call_with_potential_trajectory_truncation = fake_trunc

    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        gact_app, "_emit_semantic_event", lambda app, sid, et, **kw: emitted.append((et, kw)) or {}
    )
    ctx.set_app(object())
    ctx.set_session_id("sess_test")
    ctx.install_trajectory_cell()
    # Teardown via the autouse _reset_runtime_context fixture (tokenless sets).
    inst.forward(question="region near San Diego")

    started = [kw["payload"] for (et, kw) in emitted if et == "expert.lifecycle.started"]
    extracted = [kw["payload"] for (et, kw) in emitted if et == "expert.extract.completed"]
    assert len(started) == 1 and len(extracted) == 1
    assert started[0]["expert_id"] == "geospatial"
    assert "region near San Diego" in str(started[0]["input"])
    # The extract output (what returns to the parent) is FULL on the highway.
    assert extracted[0]["output"] == "the full region report"
    assert extracted[0]["expert_id"] == "geospatial"
    # start + extract share the expert's correlation span.
    assert started[0]["expert_span_id"] == extracted[0]["expert_span_id"]


def test_emit_semantic_event_nests_under_active_span():
    """Events auto-nest under the active span unless a parent is pinned explicitly."""
    import types

    captured: dict = {}

    class _Sink:
        def emit(self, event):
            captured["event"] = event
            return {}

    sink = _Sink()
    app = types.SimpleNamespace(
        state=types.SimpleNamespace(
            semantic_event_sink=sink,
            arc=_SinkArc(sink),
            sessions={},
            semantic_trace_detail_level="semantic",
        )
    )

    gact_app._emit_semantic_event(app, "sid", "x.y", payload={})
    assert captured["event"].parent_span_id == ""  # no active span -> empty

    tok = ctx.set_parent_span("EXPSPAN")
    try:
        gact_app._emit_semantic_event(app, "sid", "x.y", payload={})
        assert captured["event"].parent_span_id == "EXPSPAN"  # auto-nests
        gact_app._emit_semantic_event(app, "sid", "x.y", payload={}, parent_span_id="EXPLICIT")
        assert captured["event"].parent_span_id == "EXPLICIT"  # explicit wins
    finally:
        ctx.reset(tok)


def test_react_loop_emits_full_correlated_tree_through_real_sink():
    """End-to-end: a real _RetainingReAct loop through the REAL _emit_semantic_event
    + a real sink. Asserts every event family is on the highway, with FULL content,
    correlated into the recursive tree (lm.call/tool.call -> step -> expert) and
    grouped by the agent-lifecycle turn_id. Only the LM responses are faked."""
    import types

    events: list = []

    class _Sink:
        def emit(self, ev):
            events.append(ev)
            return {}

    sink = _Sink()
    app = types.SimpleNamespace(
        state=types.SimpleNamespace(
            semantic_event_sink=sink,
            arc=_SinkArc(sink),
            sessions={},
            semantic_trace_detail_level="semantic",
        )
    )

    cls = gact_app._retaining_react_cls()
    inst = object.__new__(cls)
    inst.max_iters = 5
    inst.react = object()
    inst.extract = object()
    inst._clio_expert_id = "ndp_dataset_discovery"

    # A tool that emits a tool.call.completed like the live observer does — so we can
    # prove it nests under the active ReAct step.
    def ndp_search(**kw):
        # The real tool observer stamps turn_id from the active contextvar.
        gact_app._emit_semantic_event(
            app,
            "sid",
            "tool.call.completed",
            turn_id=ctx.active_turn_id(),
            payload={"tool": "ndp_search", "args": kw},
        )
        return {"rows": ["A", "B"], "note": "x" * 3000}

    inst.tools = {"ndp_search": ndp_search, "finish": lambda: "done"}

    steps = [
        _AttrDict(
            next_thought="search ndp", next_tool_name="ndp_search", next_tool_args={"q": "gnss"}
        ),
        _AttrDict(next_thought="wrap up", next_tool_name="finish", next_tool_args={}),
    ]
    seq = {"i": 0}

    def fake_trunc(module, trajectory, **input_args):
        if module is inst.react:
            # self.react IS the per-step LLM call; emit an lm.call like IOLoggingLM
            # (which stamps turn_id from the active contextvar).
            gact_app._emit_semantic_event(
                app,
                "sid",
                "lm.call",
                turn_id=ctx.active_turn_id(),
                payload={"content": "...", "reasoning_content": "cot"},
                detail_level="off",
            )
            step = steps[seq["i"]]
            seq["i"] += 1
            return step
        return _AttrDict(answer="final report " + "y" * 100, reasoning="r")

    inst._call_with_potential_trajectory_truncation = fake_trunc

    # Establish the turn layer (app/session/turn_id) + a fresh trajectory cell.
    # Tokenless sets; teardown via the autouse _reset_runtime_context fixture.
    ctx.set_turn_identity(app=app, session_id="sid", turn_id="msg_user_1", trace_id="")
    ctx.install_trajectory_cell()
    inst.forward(question="stations near San Diego")

    by_type: dict[str, list] = {}
    for e in events:
        by_type.setdefault(e.event_type, []).append(e)

    # Every family is on the highway.
    assert len(by_type["expert.lifecycle.started"]) == 1
    assert len(by_type["expert.extract.completed"]) == 1
    assert len(by_type["react.step.completed"]) == 2
    assert len(by_type["lm.call"]) == 2  # one LLM call per ReAct step
    assert len(by_type["tool.call.completed"]) == 1  # only the search step calls a tool

    # turn_id (the agent-lifecycle id) is on EVERY event.
    assert {e.turn_id for e in events} == {"msg_user_1"}

    # The recursive tree correlates: step0's lm.call + tool.call nest under step0's
    # span; the step nests under the expert.
    step0 = by_type["react.step.completed"][0]
    step0_span = step0.payload["step_span_id"]
    expert_span = step0.payload["expert_span_id"]
    assert step0.parent_span_id == expert_span
    assert by_type["lm.call"][0].parent_span_id == step0_span
    assert by_type["tool.call.completed"][0].parent_span_id == step0_span
    # Steps get distinct spans.
    assert by_type["react.step.completed"][1].payload["step_span_id"] != step0_span

    # FULL content survives capture (no caps): the 3000-char observation and the
    # full extract output that returns to the parent.
    assert "x" * 3000 in str(step0.payload["observation"])
    assert "y" * 100 in by_type["expert.extract.completed"][0].payload["output"]
    assert by_type["expert.extract.completed"][0].payload["expert_span_id"] == expert_span


def test_prediction_summary_attaches_trajectory_and_reasoning():
    pred = _AttrDict(
        answer="A",
        trajectory={"thought_0": "t", "tool_name_0": "finish"},
        reasoning="chain of thought",
    )
    summary = gact_app._prediction_summary(pred)
    assert summary["answer"] == "A"
    assert summary["trajectory"]  # full capture present
    assert summary["reasoning"] == "chain of thought"


def test_prediction_summary_omits_absent_trajectory_and_reasoning():
    pred = _AttrDict(answer="A")
    summary = gact_app._prediction_summary(pred)
    # Lean payload for routing/predict: no empty trajectory/reasoning keys.
    assert "trajectory" not in summary
    assert "reasoning" not in summary


class _FakeProgram:
    """Stands in for a dspy.ReAct: only extract + _format_trajectory used."""

    def __init__(self, *, fail=False):
        self.extract_calls = []
        self._fail = fail

    def _format_trajectory(self, trajectory):
        return f"FMT:{sorted(trajectory)}"

    def extract(self, **kwargs):
        self.extract_calls.append(kwargs)
        if self._fail:
            raise ValueError("still missing required field: station_ids")
        return _AttrDict(answer="fixed", station_ids=["SIO5"])


def test_reextract_reruns_only_extract_over_retained_trajectory():
    prog = _FakeProgram()
    # Install a cell pre-seeded with a populated retained trajectory; teardown via
    # the autouse _reset_runtime_context fixture.
    ctx.install_trajectory(
        {"trajectory": {"tool_name_0": "shell_bash"}, "input_args": {"question": "find stations"}}
    )
    result = gact_app._reextract_over_retained_trajectory(prog, "FILL station_ids")
    assert result is not None
    assert result.answer == "fixed"
    # extract ran exactly once (NOT the whole tool loop)
    assert len(prog.extract_calls) == 1
    call = prog.extract_calls[0]
    assert "FILL station_ids" in call["question"]  # hint steered the re-extract
    assert call["trajectory"].startswith("FMT:")  # retained trajectory formatted + passed
    # original trajectory threaded back into the Prediction
    assert result.trajectory == {"tool_name_0": "shell_bash"}


def test_reextract_returns_none_without_retained_trajectory():
    prog = _FakeProgram()
    ctx.install_trajectory_cell()  # cell present but value=None
    assert gact_app._reextract_over_retained_trajectory(prog, "hint") is None
    assert prog.extract_calls == []


def test_reextract_returns_none_when_extract_fails():
    prog = _FakeProgram(fail=True)
    ctx.install_trajectory({"trajectory": {"tool_name_0": "x"}, "input_args": {"question": "q"}})
    # extract raises again -> None, so the caller falls back to full re-ask.
    assert gact_app._reextract_over_retained_trajectory(prog, "hint") is None
