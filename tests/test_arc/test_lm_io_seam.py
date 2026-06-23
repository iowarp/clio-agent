"""Unit tests for the lm_io capture seam (config.IOLoggingLM boundary -> ARC).

The seam is the ONE source of truth per LM call: ``config.set_lm_io_sink`` injects
a closure that ``_clio_log_last_call`` invokes with the full call record (after the
durable ``lm.call`` emit), and the gact-side ``_route_lm_io_to_arc`` lands it as an
``lm_io`` segment in ARC's ACTIVE (session, scope), correlation-stamped. config.py
imports nothing from arc/gact: the closure is injected from build_app.

These tests exercise the gact-side sink directly (the routing + scope keying +
correlation), plus the config-side injection point (one sink call per faked LM call),
without a live model.
"""

from __future__ import annotations

import types

import clio_agent.config as config
import clio_agent.gact.app as app
from clio_agent.arc.memory import ARCMemory
from clio_agent.gact import context as ctx

from .conftest import live_plane_context

SID, SCOPE = "lmio-s1", "agentA/expertX"


def _record() -> dict:
    return {
        "model": "fake/model",
        "messages": [{"role": "user", "content": "hello"}],
        "content": "hi there",
        "reasoning_content": "thinking",
        "finish_reason": "stop",
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }


def test_route_lm_io_lands_as_segment_with_correlation(tmp_path):
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    fake_app = types.SimpleNamespace(state=types.SimpleNamespace(arc=arc))
    with live_plane_context(arc, session=SID, scope=SCOPE):
        parent_token = ctx.set_parent_span("EXPERT_SPAN")
        run_token = ctx.set_run_span("RUN_SPAN")
        ctx.set_turn_id("TURN_1")
        try:
            app._route_lm_io_to_arc(fake_app, _record())
        finally:
            ctx.reset(run_token)
            ctx.reset(parent_token)

    segs = arc.render_segments(SID, SCOPE)
    lm_ios = [s for s in segs if s.kind == "lm_io"]
    assert len(lm_ios) == 1
    seg = lm_ios[0]
    assert seg.content["content"] == "hi there"
    assert seg.content["reasoning"] == "thinking"
    assert seg.content["finish_reason"] == "stop"
    assert seg.content["usage"] == {"prompt_tokens": 5, "completion_tokens": 2}
    # correlation: semantic turn id + expert span + run span all stamped
    assert seg.turn_id == "TURN_1"
    assert seg.expert_span_id == "EXPERT_SPAN"
    assert seg.run_span_id == "RUN_SPAN"


def test_lm_io_is_excluded_from_the_prompt_view(tmp_path):
    """The lm_io atom is freeze-anytime state but NOT working-set: it must never
    appear in render_segments_keys (the prompt) nor in render_working_set."""
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    fake_app = types.SimpleNamespace(state=types.SimpleNamespace(arc=arc))
    with live_plane_context(arc, session=SID, scope=SCOPE):
        keys_before = arc.render_segments_keys(SID, SCOPE)
        app._route_lm_io_to_arc(fake_app, _record())
        keys_after = arc.render_segments_keys(SID, SCOPE)
    assert keys_after == keys_before  # prompt unchanged by the lm_io write
    ws_kinds = {s.kind for s in arc.render_working_set(SID, SCOPE)}
    assert "lm_io" not in ws_kinds
    # but it IS in the complete freeze-anytime state
    assert "lm_io" in {s.kind for s in arc.render_segments(SID, SCOPE)}


def test_route_lm_io_no_scope_is_noop(tmp_path):
    """No active react scope (e.g. a Tier-1 call before delegation) => no lm_io
    segment. The durable lm.call still fired upstream; there is no scope to attach."""
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    fake_app = types.SimpleNamespace(state=types.SimpleNamespace(arc=arc))
    # No live_plane_context -> react scope is "" -> early return.
    app._route_lm_io_to_arc(fake_app, _record())
    assert arc.render_segments(SID, SCOPE) == []


def test_route_lm_io_arc_disabled_is_noop():
    """No ARC on app.state => no-op, never raises."""
    fake_app = types.SimpleNamespace(state=types.SimpleNamespace(arc=None))
    app._route_lm_io_to_arc(fake_app, _record())  # must not raise


def test_set_lm_io_sink_receives_one_record_per_call():
    """config.set_lm_io_sink injects a sink that _clio_log_last_call invokes once
    per LM call (after the lm.call emit), with the assembled record. Driven without
    a real model by faking the LM history + the trace target."""
    received: list[dict] = []
    config.set_lm_io_sink(lambda rec: received.append(rec))
    try:
        lm_cls = config._io_logging_lm_cls()
        lm = lm_cls.__new__(lm_cls)  # bypass dspy.LM.__init__ (no real provider)
        # Fake a completed call's history entry, shaped like dspy stores it.
        msg = types.SimpleNamespace(content="ANSWER", reasoning_content="REASONS")
        choice = types.SimpleNamespace(message=msg, finish_reason="stop")
        response = types.SimpleNamespace(choices=[choice])
        lm.history = [
            {
                "model": "fake/model",
                "messages": [{"role": "user", "content": "q"}],
                "response": response,
                "usage": {"prompt_tokens": 3},
                "timestamp": 0.0,
            }
        ]
        # An active GACT turn is required (the seam sits after the no-turn guard).
        app_token = ctx.set_app(types.SimpleNamespace(state=types.SimpleNamespace()))
        ctx.set_turn_identity(app=ctx.active_app(), session_id="sX", turn_id="tX", trace_id="trX")
        try:
            lm._clio_log_last_call()
        finally:
            ctx.reset(app_token)
    finally:
        config.set_lm_io_sink(None)  # always clear the global sink

    assert len(received) == 1
    rec = received[0]
    assert rec["content"] == "ANSWER"
    assert rec["reasoning_content"] == "REASONS"
    assert rec["finish_reason"] == "stop"
    assert rec["model"] == "fake/model"
