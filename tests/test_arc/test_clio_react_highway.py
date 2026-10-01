"""Acceptance: ``ClioReAct`` drives the ARC live plane, the semantic-event highway, and
the auto-compaction trigger in one run.

``ClioReAct`` (via ``clio_react_record.StepRecorder``) writes per-step ARC
thought/tool_call/observation segments, puts ``react.step.completed`` +
``expert.lifecycle.started`` / ``expert.extract.completed`` on the highway, and fires
the proactive ``maybe_autocompact`` trigger at every step boundary before the model
call.

Sabotage tripwires:
* remove a lifecycle emission (``_emit_react_step_event`` /
  ``_emit_expert_lifecycle_event`` / a ``StepRecorder._write``) →
  ``test_forward_writes_arc_and_emits_highway`` goes red;
* remove the ``maybe_autocompact()`` call in ``_Loop._one_step`` →
  ``test_forward_fires_autocompact_trigger_each_step`` goes red.
"""

from __future__ import annotations

import types
from typing import Any

import dspy
import pytest

import clio_agent.gact.compaction as compaction
import clio_agent.gact.runtime.globals as runtime_globals
from clio_agent.arc.memory import ARCMemory
from clio_agent.gact import context as ctx
from clio_agent.gact.agents import clio_react
from clio_agent.gact.agents.clio_react import ClioReAct
from tests._scripted_engine import calls, scripted_lm

from .conftest import live_plane_context

SID, SCOPE = "clio-react-highway-s1", "agentA"


def _search(q: str) -> str:
    """A deterministic search tool."""
    return "SEARCH_RESULT"


def _two_step_lm() -> dspy.LM:
    """One ``search`` step then a ``submit`` step."""
    lm, _ = scripted_lm(
        [
            calls(("search", {"q": "alpha"}), text="search first"),
            calls(("submit", {"answer": "FINAL"}), text="done"),
        ]
    )
    return lm


def _build_agent() -> ClioReAct:
    return ClioReAct("question -> answer", tools=[dspy.Tool(_search, name="search")], max_iters=6)


def _run_in_plane(arc: ARCMemory, agent: ClioReAct, lm: dspy.LM) -> Any:
    fake_app = types.SimpleNamespace(state=types.SimpleNamespace(arc=arc))
    sess_token = ctx.set_session_id(SID)
    app_token = ctx.set_app(fake_app)
    try:
        with live_plane_context(arc, session=SID, scope=SCOPE):
            with dspy.context(lm=lm):
                return agent(question="find alpha")
    finally:
        ctx.reset(app_token)
        ctx.reset(sess_token)


def test_forward_writes_arc_and_emits_highway(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One forward produces BOTH the ARC live-plane writes AND the highway events —
    the loop can't silently regress to only one (or neither)."""
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))

    step_events: list[dict] = []
    lifecycle_events: list[str] = []
    monkeypatch.setattr(
        runtime_globals, "_emit_react_step_event", lambda **kw: step_events.append(kw)
    )
    monkeypatch.setattr(
        runtime_globals,
        "_emit_expert_lifecycle_event",
        lambda event_type, **kw: lifecycle_events.append(event_type),
    )

    pred = _run_in_plane(arc, _build_agent(), _two_step_lm())

    # Highway half: a react.step.completed per step + the expert lifecycle boundaries.
    assert [e["step_index"] for e in step_events] == [0, 1]
    assert [e["thought"] for e in step_events] == ["search first", "done"]
    # every call of the step, in call order, with its own result
    assert step_events[0]["tool_calls"] == [
        {
            "id": "call_0_0",
            "name": "search",
            "args": {"q": "alpha"},
            "observation": "SEARCH_RESULT",
            "is_error": False,
        }
    ]
    assert [c["name"] for c in step_events[1]["tool_calls"]] == ["submit"]
    assert step_events[0]["is_finish"] is False
    assert step_events[1]["is_finish"] is True, "no finishing (submit) step on the highway"
    assert lifecycle_events == ["expert.lifecycle.started", "expert.extract.completed"]

    # ARC half: the user message, then thought / tool_call / observation per step,
    # call id carried through.
    live = arc.render_segments(SID, SCOPE)
    assert [s.kind for s in live] == [
        "user",
        "thought",
        "tool_call",
        "observation",
        "thought",
        "tool_call",
        "observation",
    ]
    call, obs = live[2].content, live[3].content
    assert (call["name"], call["args"]) == ("search", {"q": "alpha"})
    assert obs["call_id"] == call["id"]
    assert obs["is_error"] is False
    assert (pred.answer, pred.termination_reason) == ("FINAL", "submit")


def test_a_multi_call_step_is_one_highway_event_carrying_every_call(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A step with two calls puts ONE ``react.step.completed`` on the highway carrying
    both calls (call order, each with its own result / error flag); ``is_finish`` holds
    when any call of the step is ``submit``."""
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    step_events: list[dict] = []
    monkeypatch.setattr(
        runtime_globals, "_emit_react_step_event", lambda **kw: step_events.append(kw)
    )

    def _boom(x: str) -> str:
        """Fails."""
        raise RuntimeError("nope")

    agent = ClioReAct(
        "question -> answer",
        tools=[dspy.Tool(_search, name="search"), dspy.Tool(_boom, name="boom")],
        max_iters=6,
    )
    lm, _ = scripted_lm(
        [
            calls(("search", {"q": "alpha"}), ("boom", {"x": "1"}), text="both"),
            calls(
                ("search", {"q": "beta"}), ("submit", {"answer": "FINAL"}), text="look and finish"
            ),
        ]
    )
    pred = _run_in_plane(arc, agent, lm)

    assert len(step_events) == 2
    first = step_events[0]["tool_calls"]
    assert [(c["id"], c["name"], c["is_error"]) for c in first] == [
        ("call_0_0", "search", False),
        ("call_0_1", "boom", True),
    ]
    assert first[0]["observation"] == "SEARCH_RESULT"
    assert "Execution error in boom" in first[1]["observation"]
    assert step_events[0]["is_finish"] is False
    assert [c["name"] for c in step_events[1]["tool_calls"]] == ["search", "submit"]
    assert step_events[1]["is_finish"] is True
    assert (pred.answer, pred.termination_reason) == ("FINAL", "submit")


def test_forward_fires_autocompact_trigger_each_step(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proactive auto-compaction TRIGGER is wired into the loop: it fires at every
    step boundary before the model call (removing the call turns this red)."""
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    fired = {"n": 0}
    monkeypatch.setattr(
        compaction, "maybe_autocompact", lambda: fired.__setitem__("n", fired["n"] + 1)
    )

    _run_in_plane(arc, _build_agent(), _two_step_lm())

    # Two steps (search + submit) => the trigger fired once per model call.
    assert fired["n"] == 2, f"autocompact trigger did not fire per step; fired {fired['n']}x"


def test_escalation_closes_the_lifecycle_span_and_records_the_step(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1282 F6 (#1275 ask 3): a typed refusal escalating out of the loop must not leave
    ``expert.lifecycle.started`` without a matching close on the highway, and the step
    that died is recorded on the plane (its refused call's error observation) plus the
    ``[turn escalated]`` note, so the plane holds what the turn produced before it died."""
    from clio_agent.errors import MCPMissingRequiredClientCapabilityError

    def _refusing_tool(payload: str = "") -> str:
        """Refuses (tasks extension missing)."""
        raise MCPMissingRequiredClientCapabilityError(
            "task_echo requires the tasks extension",
            {"requiredCapabilities": {"extensions": {"io.modelcontextprotocol/tasks": {}}}},
        )

    agent = ClioReAct(
        "question -> answer",
        tools=[dspy.Tool(_search, name="search"), dspy.Tool(_refusing_tool)],
        max_iters=6,
    )
    # Step 1 succeeds (search) so there IS prior-step context by the time step 2's
    # refusal escalates.
    lm, _ = scripted_lm(
        [
            calls(("search", {"q": "alpha"}), text="search first"),
            calls(("_refusing_tool", {"payload": "x"}), text="call it"),
        ]
    )

    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    lifecycle_events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        runtime_globals,
        "_emit_expert_lifecycle_event",
        lambda event_type, **kw: lifecycle_events.append((event_type, kw)),
    )

    with pytest.raises(MCPMissingRequiredClientCapabilityError):
        _run_in_plane(arc, agent, lm)

    types_seen = [event_type for event_type, _kw in lifecycle_events]
    assert types_seen == ["expert.lifecycle.started", "expert.lifecycle.failed"], (
        "an escalated refusal must close the lifecycle span, not just open it"
    )
    failed_payload = lifecycle_events[-1][1]
    assert failed_payload["status"] == "failed"
    assert failed_payload["payload"]["reason"] == "mcp_capability_refused"

    live = arc.render_segments(SID, SCOPE)
    thoughts = [s.content["text"] for s in live if s.kind == "thought"]
    assert thoughts == ["search first", "call it"]
    observations = [s.content for s in live if s.kind == "observation"]
    assert observations[0]["text"] == "SEARCH_RESULT"
    refused = observations[1]
    assert refused["is_error"] is True
    assert "Execution error in _refusing_tool" in refused["text"]
    assert observations[-1]["text"].startswith("[turn escalated] mcp_capability_refused")


def test_generic_crash_escalates_unchanged_no_arc_enrichment(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1282 F6 scope: the escalation branch is narrowed to ``ClioError`` on purpose. A
    GENERIC crash (a bare ``RuntimeError``, not a typed clio error) must propagate
    completely UNCHANGED -- no ``expert.lifecycle.failed``, no closing ARC observation.
    That is ARC's own deliberate crash contract (``arc/working_set_fold.py`` §2.8b,
    pinned by ``test_working_set_fold_step_open.py::test_crash_leaves_step_open``): a
    hard mid-step crash leaves ONLY the step_open breadcrumb on the canonical log,
    never a synthesized closing observation authored after the fact."""
    agent = ClioReAct(
        "question -> answer", tools=[dspy.Tool(lambda: "ok", name="probe")], max_iters=6
    )
    lm, _ = scripted_lm([calls(("probe", {}), text="call probe")])

    # A HARD mid-step failure: the loop turns tool-callable errors into observations,
    # so fail the execution STAGE itself, as the ARC contract's own pin does.
    def _boom(_self: object, _tool_calls: object) -> None:
        raise RuntimeError("execution stage exploded mid-step")

    monkeypatch.setattr(clio_react._Loop, "_execute", _boom)

    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    lifecycle_events: list[str] = []
    monkeypatch.setattr(
        runtime_globals,
        "_emit_expert_lifecycle_event",
        lambda event_type, **kw: lifecycle_events.append(event_type),
    )

    with pytest.raises(RuntimeError, match="execution stage exploded mid-step"):
        _run_in_plane(arc, agent, lm)

    assert lifecycle_events == ["expert.lifecycle.started"], (
        "a generic crash must NOT close the lifecycle span -- out of F6's scope"
    )
    live = arc.render_segments(SID, SCOPE)
    assert not any(s.kind == "observation" for s in live), (
        "a generic crash must leave no synthesized closing observation -- "
        "only the pre-execution step_open breadcrumb, per the ARC crash contract"
    )
