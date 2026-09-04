"""Acceptance: the V2 loop drives the ARC live plane, the semantic-event highway, and
the auto-compaction trigger in one run (#901 S6 — the V2 analog of
``test_live_plane_highway_interleave`` + the ``_maybe_autocompact`` trigger).

``dspy.ReActV2.forward`` is silent (no ARC writes, no highway events, no compaction).
``clio_agent.gact.agents.reactv2_events.instrumented_forward`` re-establishes clio's
frozen wire/trace contract on the append-only V2 loop: per-step ARC
thought/tool_call/observation writes, the ``react.step.completed`` +
``expert.lifecycle.started`` / ``expert.extract.completed`` highway events (the SAME
event stream shape the classic loop emits), and the proactive ``_maybe_autocompact``
trigger fired before every ``react`` call.

Sabotage tripwires (from the task):
* remove a V2 lifecycle emission (``_emit_react_step_event`` /
  ``_emit_expert_lifecycle_event`` / an ``_arc_write``) →
  ``test_v2_forward_writes_arc_and_emits_highway`` goes red;
* remove the ``agent._maybe_autocompact()`` call in ``instrumented_forward`` →
  ``test_v2_forward_fires_autocompact_trigger_each_turn`` goes red.
"""

from __future__ import annotations

import types
from typing import Any

import dspy
import pytest
from dspy.utils.dummies import DummyLM

import clio_agent.gact.agents.reactv2_events as reactv2_events
from clio_agent.arc.memory import ARCMemory
from clio_agent.gact import context as ctx
from clio_agent.gact.agents.reactv2 import retaining_reactv2_cls

from .conftest import live_plane_context

SID, SCOPE = "v2-highway-s1", "agentA"


def _search(q: str) -> str:
    """A deterministic search tool."""
    return "SEARCH_RESULT"


def _two_step_v2_lm() -> DummyLM:
    """One ``search`` turn then a ``submit`` turn (ToolCalls/submit shape)."""
    return DummyLM(
        [
            {
                "next_thought": "search first",
                "tool_calls": {"tool_calls": [{"name": "search", "args": {"q": "alpha"}}]},
            },
            {
                "next_thought": "done",
                "tool_calls": {"tool_calls": [{"name": "submit", "args": {"answer": "FINAL"}}]},
            },
        ]
    )


def _build_agent() -> object:
    return retaining_reactv2_cls()("question -> answer", tools=[dspy.Tool(_search)], max_iters=6)


def test_v2_forward_writes_arc_and_emits_highway(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One V2 forward run produces BOTH the ARC live-plane writes AND the highway
    events — the append-only loop can't silently regress to only one (or neither)."""
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    agent = _build_agent()

    step_events: list[dict] = []
    lifecycle_events: list[str] = []
    monkeypatch.setattr(
        reactv2_events, "_emit_react_step_event", lambda **kw: step_events.append(kw)
    )
    monkeypatch.setattr(
        reactv2_events,
        "_emit_expert_lifecycle_event",
        lambda event_type, **kw: lifecycle_events.append(event_type),
    )

    fake_app = types.SimpleNamespace(state=types.SimpleNamespace(arc=arc))
    sess_token = ctx.set_session_id(SID)
    app_token = ctx.set_app(fake_app)
    try:
        with live_plane_context(arc, session=SID, scope=SCOPE):
            with dspy.context(lm=_two_step_v2_lm(), adapter=dspy.ChatAdapter()):
                pred = agent(question="find alpha")
    finally:
        ctx.reset(app_token)
        ctx.reset(sess_token)

    # Highway half: a react.step.completed per turn + the expert lifecycle boundaries.
    assert step_events, "V2 forward emitted no react.step.completed events"
    assert any(e.get("is_finish") for e in step_events), "no finishing (submit) step on the highway"
    assert "expert.lifecycle.started" in lifecycle_events
    assert "expert.extract.completed" in lifecycle_events

    # ARC half: the SAME thought/tool_call/observation kinds the classic loop writes.
    live = arc.render_segments(SID, SCOPE)
    kinds = {s.kind for s in live}
    assert {"thought", "tool_call", "observation"} <= kinds, (
        f"live plane missing produced kinds; got {kinds}"
    )
    assert str(getattr(pred, "answer", "")) == "FINAL"


def test_v2_forward_fires_autocompact_trigger_each_turn(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proactive auto-compaction TRIGGER is wired into the V2 loop: forward calls
    ``agent._maybe_autocompact`` before every ``react`` call (removing the call in
    ``instrumented_forward`` turns this red)."""
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    agent = _build_agent()

    calls = {"n": 0}
    monkeypatch.setattr(agent, "_maybe_autocompact", lambda: calls.__setitem__("n", calls["n"] + 1))

    fake_app = types.SimpleNamespace(state=types.SimpleNamespace(arc=arc))
    sess_token = ctx.set_session_id(SID)
    app_token = ctx.set_app(fake_app)
    try:
        with live_plane_context(arc, session=SID, scope=SCOPE):
            with dspy.context(lm=_two_step_v2_lm(), adapter=dspy.ChatAdapter()):
                agent(question="find alpha")
    finally:
        ctx.reset(app_token)
        ctx.reset(sess_token)

    # Two turns (search + submit) => the trigger fired at least twice, one per send.
    assert calls["n"] >= 2, f"autocompact trigger did not fire per turn; fired {calls['n']}x"


def test_v2_forward_escalation_closes_the_lifecycle_span_and_publishes_history(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1282 F6 (#1275 ask 3): a D1-escalated typed refusal propagating out of
    ``instrumented_forward`` must not leave ``expert.lifecycle.started`` with
    no matching close on the highway, and must not skip publishing the
    retained History (the S4 repair entry's only read of what the turn
    produced before it died). Sabotage: removing the F6 except branch in
    ``instrumented_forward`` turns this red (no ``expert.lifecycle.failed``,
    ``active_trajectory()`` stays whatever it was before this forward)."""
    from clio_agent.errors import MCPMissingRequiredClientCapabilityError

    def _refusing_tool(payload: str = "") -> str:
        raise MCPMissingRequiredClientCapabilityError(
            "task_echo requires the tasks extension",
            {"requiredCapabilities": {"extensions": {"io.modelcontextprotocol/tasks": {}}}},
        )

    agent = retaining_reactv2_cls()(
        "question -> answer",
        tools=[dspy.Tool(_search), dspy.Tool(_refusing_tool)],
        max_iters=6,
    )
    # Turn 1 succeeds (search) so there IS retained prior-turn history by the
    # time turn 2's refusal escalates -- proving the retain captures what the
    # turn actually produced before it died, not merely an empty stand-in.
    lm = DummyLM(
        [
            {
                "next_thought": "search first",
                "tool_calls": {"tool_calls": [{"name": "search", "args": {"q": "alpha"}}]},
            },
            {
                "next_thought": "call it",
                "tool_calls": {
                    "tool_calls": [{"name": "_refusing_tool", "args": {"payload": "x"}}]
                },
            },
        ]
    )

    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    lifecycle_events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        reactv2_events,
        "_emit_expert_lifecycle_event",
        lambda event_type, **kw: lifecycle_events.append((event_type, kw)),
    )

    fake_app = types.SimpleNamespace(state=types.SimpleNamespace(arc=arc))
    sess_token = ctx.set_session_id(SID)
    app_token = ctx.set_app(fake_app)
    try:
        with live_plane_context(arc, session=SID, scope=SCOPE):
            with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
                with pytest.raises(MCPMissingRequiredClientCapabilityError):
                    agent(question="find alpha")
            # The trajectory cell was installed by reactv2.py's own forward()
            # before instrumented_forward ran; F6's except branch published
            # into it before re-raising, so it must NOT still be None. Read
            # INSIDE live_plane_context's own scope -- its __exit__ resets
            # the contextvar token it set, which would otherwise mask this.
            retained = ctx.active_trajectory()
    finally:
        ctx.reset(app_token)
        ctx.reset(sess_token)

    types_seen = [event_type for event_type, _kw in lifecycle_events]
    assert "expert.lifecycle.started" in types_seen
    assert "expert.lifecycle.failed" in types_seen, (
        "an escalated refusal must close the lifecycle span, not just open it"
    )
    failed_payload = next(
        kw for event_type, kw in lifecycle_events if event_type == "expert.lifecycle.failed"
    )
    assert failed_payload["status"] == "failed"
    assert failed_payload["payload"]["reason"] == "mcp_capability_refused"

    assert retained is not None, "the retained History must be published before re-raising"
    assert retained["termination_reason"] == "escalated_error"
    assert retained["history"], "the turn's messages up to the escalation must be retained"


def test_v2_forward_generic_crash_escalates_unchanged_no_arc_enrichment(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1282 F6 scope (re-verify round): the except branch above is narrowed
    to ``ClioError`` on purpose. A GENERIC crash (a bare ``RuntimeError``, not
    a typed clio error) must propagate completely UNCHANGED -- no
    ``expert.lifecycle.failed``, no closing ARC observation, no
    retained-history publish. That is ARC's own deliberate crash contract
    (``arc/working_set_fold.py`` §2.8b, ``emit_step_open``'s own docstring,
    pinned by
    ``test_working_set_fold_step_open.py::test_crash_leaves_step_open``): a
    hard mid-step crash leaves ONLY the step_open breadcrumb on the
    canonical log, never a synthesized closing observation authored after
    the fact. An earlier version of the F6 fix caught ``Exception``
    unconditionally and broke exactly this contract -- this is the
    regression arm for that."""

    agent = retaining_reactv2_cls()(
        "question -> answer", tools=[dspy.Tool(lambda: "ok", name="probe")], max_iters=6
    )
    lm = DummyLM(
        [
            {
                "next_thought": "call probe",
                "tool_calls": {"tool_calls": [{"name": "probe", "args": {}}]},
            }
        ]
    )

    # A HARD mid-step failure, mirroring test_working_set_fold_step_open.py's
    # OWN sabotage technique exactly: dspy wraps *tool-callable* errors into
    # observations (D1's own escalation only intervenes for a TYPED refusal),
    # so a bare tool exception never reaches instrumented_forward's outer
    # except at all -- to model a genuine unrecovered crash, fail the
    # execution STAGE itself (the loop's own agent._execute_tool_calls call),
    # exactly like the ARC contract's own pin does.
    def _boom(_tool_calls: Any) -> Any:
        raise RuntimeError("execution stage exploded mid-step")

    monkeypatch.setattr(agent, "_execute_tool_calls", _boom)

    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    lifecycle_events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        reactv2_events,
        "_emit_expert_lifecycle_event",
        lambda event_type, **kw: lifecycle_events.append((event_type, kw)),
    )

    fake_app = types.SimpleNamespace(state=types.SimpleNamespace(arc=arc))
    sess_token = ctx.set_session_id(SID)
    app_token = ctx.set_app(fake_app)
    try:
        with live_plane_context(arc, session=SID, scope=SCOPE):
            with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
                with pytest.raises(RuntimeError, match="execution stage exploded mid-step"):
                    agent(question="find alpha")
            retained = ctx.active_trajectory()
    finally:
        ctx.reset(app_token)
        ctx.reset(sess_token)

    types_seen = [event_type for event_type, _kw in lifecycle_events]
    assert "expert.lifecycle.started" in types_seen
    assert "expert.lifecycle.failed" not in types_seen, (
        "a generic crash must NOT close the lifecycle span -- out of F6's scope"
    )
    assert retained is None, "a generic crash must NOT publish retained history either"

    live = arc.render_segments(SID, SCOPE)
    assert not any(s.kind == "observation" for s in live), (
        "a generic crash must leave no synthesized closing observation -- "
        "only the pre-execution step_open breadcrumb, per the ARC crash contract"
    )
