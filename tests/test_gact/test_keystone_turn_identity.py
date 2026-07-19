"""Keystone (#735 unified-concurrency §3): the turn establishes its full identity
layer — app + session — for the WHOLE turn, not just turn_id/trace_id.

Before the keystone, ``_run_turn_in_background`` bare-set only ``turn_id`` /
``trace_id`` at the top of the turn; ``turn.app`` / ``turn.session_id`` were bound
only inside the narrow ``_gact_app_context`` / ``set_session_id`` wrappers around
the *dynamic-agent* forward sites. The CLIO orchestrator forward path
(``_agent_forward_compat`` under a ``contextvars.copy_context()`` executor
snapshot) had NEITHER, so ``active_app()`` resolved ``None`` and
``active_session_id()`` resolved ``""`` on the executor rail — which made the
empty-gated emitters (``_emit_react_step_event`` / ``_emit_expert_lifecycle_event``)
return early and emit nothing on the main path.

This test drives a real turn with a non-streamable fake agent (which routes
through the orchestrator sync path) and, from INSIDE the executor
``copy_context`` snapshot (the agent's ``forward``), asserts:

  (a) ``active_app()`` is non-None and ``active_session_id()`` == this turn's sid;
  (b) an emit-through-the-gate ``react.step.completed`` event actually reaches the
      live bus carrying that session (proving the emitter no longer early-returns
      on the orchestrator path).

Both fail before the keystone (app/session unbound on the rail) and pass after.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app

from .test_post_messages import FakeClioAgent

# #948 S4b: default sessions run the blueprint react ``main``; route it to each
# test's ``build_app(agent=...)`` host fake.
pytestmark = pytest.mark.usefixtures("host_agent_executor")


class _KeystoneProbeAgent(FakeClioAgent):
    """Orchestrator-path fake that records the executor-rail identity and fires
    an empty-gated emitter from inside the turn's ``copy_context`` snapshot."""

    def __init__(self) -> None:
        super().__init__(answer="probed")
        self.captured_app: Any = "unset"
        self.captured_sid: str = "unset"

    def forward(self, question: str, session_id: str) -> Any:
        from clio_agent.gact import context as _ctx  # noqa: PLC0415
        from clio_agent.gact.runtime import globals as _g  # noqa: PLC0415

        # This runs on the orchestrator executor rail (contextvars.copy_context()
        # snapshot taken at the sync forward site). Record what the rail carries.
        self.captured_app = _ctx.active_app()
        self.captured_sid = _ctx.active_session_id()

        # Drive an empty-gated emitter: it returns early when active_app() is None
        # or the session is empty, so a landed react.step.completed proves the gate
        # is open on the main path.
        _g._emit_react_step_event(
            expert_id="keystone-probe",
            expert_span_id="span-expert",
            step_span_id="span-step",
            step_index=0,
            thought="probe thought",
            reasoning="probe reasoning",
            tool_name="",
            tool_args={},
            observation="",
            is_finish=True,
        )
        return super().forward(question, session_id)


def test_keystone_binds_app_and_session_on_orchestrator_rail(tmp_path: Path) -> None:
    agent = _KeystoneProbeAgent()
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "x"}).json()["id"]
        ack = c.post(
            f"/v1/sessions/{sid}/messages",
            json={"parts": [{"type": "text", "text": "hi"}]},
        )
        assert ack.status_code == 200, ack.text

        deadline = time.monotonic() + 5.0
        status = "running"
        while time.monotonic() < deadline:
            status = c.get(f"/v1/sessions/{sid}").json()["status"]
            if status != "running":
                break
            time.sleep(0.05)
        assert status != "running", "turn never settled"

    # (a) the executor rail carried the live app + this turn's session.
    assert agent.captured_app is not None, (
        "active_app() must be non-None inside the orchestrator copy_context snapshot"
    )
    assert agent.captured_app is app, "the rail must carry THIS turn's app"
    assert agent.captured_sid == sid, (
        f"active_session_id() on the rail must equal the turn session; got {agent.captured_sid!r}"
    )

    # (b) the empty-gated react.step emitter fired on the main path with a session.
    history = app.state.bus._history.get(sid, [])
    react_steps = [
        ev
        for ev in history
        if ev.type == "semantic.event" and ev.payload.get("event_type") == "react.step.completed"
    ]
    assert react_steps, (
        "react.step.completed must reach the bus on the orchestrator path "
        "(the emitter early-returns when active_app() is None)"
    )
    assert react_steps[-1].payload.get("session_id") == sid
