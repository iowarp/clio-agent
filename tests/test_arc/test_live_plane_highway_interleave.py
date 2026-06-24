"""Integration guard: the unified `_RetainingReAct.forward` drives BOTH the ARC
live-context-plane writes AND the semantic-event highway emission in a single run.

These two concerns were merged from two branches into one `forward` (the ARC live
plane reads/writes the prompt; the highway emits the per-step trajectory). The
foundation suite proves each half through the real loop separately
(`test_real_react_loop_trace_reconstructs_arc` for the ARC writes;
`test_trajectory_retention` for the highway events). This test locks their
CONJUNCTION — that one forward run produces both, so the interleave can't silently
regress to only one.
"""

from __future__ import annotations

import types

import dspy
import pytest
from dspy.utils.dummies import DummyLM

import clio_agent.gact.agents.runtime as agent_runtime
from clio_agent.arc.memory import ARCMemory
from clio_agent.gact import context as ctx

from .conftest import live_plane_context, make_react_agent

SID, SCOPE = "interleave-s1", "agentA"


def test_forward_writes_arc_and_emits_highway_in_one_run(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    agent = make_react_agent()

    # Capture the highway emitters without needing a real sink: the forward must
    # call them. (The full sink path is covered by test_trajectory_retention.)
    step_events: list[dict] = []
    lifecycle_events: list[str] = []
    # The retaining forward resolves the highway emitters from its own module
    # (gact.agents.runtime, #714 step 4), so patch the owner there rather than the
    # gact.app re-export shim -- a setattr on app would not intercept the call.
    monkeypatch.setattr(
        agent_runtime,
        "_emit_react_step_event",
        lambda **kw: step_events.append(kw),
    )
    monkeypatch.setattr(
        agent_runtime,
        "_emit_expert_lifecycle_event",
        lambda event_type, **kw: lifecycle_events.append(event_type),
    )

    lm = DummyLM(
        [
            {
                "next_thought": "search first",
                "next_tool_name": "search",
                "next_tool_args": '{"q": "alpha"}',
            },
            {"next_thought": "done", "next_tool_name": "finish", "next_tool_args": "{}"},
            {"reasoning": "because", "answer": "FINAL"},
        ]
    )

    fake_app = types.SimpleNamespace(state=types.SimpleNamespace(arc=arc))
    sess_token = ctx.set_session_id(SID)
    app_token = ctx.set_app(fake_app)
    try:
        with live_plane_context(arc, session=SID, scope=SCOPE):
            with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
                pred = agent(question="find alpha")
    finally:
        ctx.reset(app_token)
        ctx.reset(sess_token)

    # Highway half: a react.step.completed per non-extract step + the expert
    # lifecycle boundaries fired from this same forward.
    assert step_events, "forward emitted no react.step.completed events"
    assert any(e.get("is_finish") for e in step_events), "no finishing step on the highway"
    assert "expert.lifecycle.started" in lifecycle_events
    assert "expert.extract.completed" in lifecycle_events

    # ARC half: the loop wrote thought/tool_call/observation segments to the live
    # plane (the prompt source), in the SAME run.
    live = arc.render_segments(SID, SCOPE)
    kinds = {s.kind for s in live}
    assert {"thought", "tool_call", "observation"} <= kinds, (
        f"live plane missing produced kinds; got {kinds}"
    )
    assert str(getattr(pred, "answer", "")) == "FINAL"
