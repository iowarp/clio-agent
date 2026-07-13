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


def test_v2_forward_fires_autocompact_trigger_each_turn(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
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
