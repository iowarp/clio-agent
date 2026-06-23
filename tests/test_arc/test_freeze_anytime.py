"""Freeze-anytime completeness + prompt/compaction invariance (issue #714).

The decisive ARC-as-source contract: at any instant, ARC's current state must hold
EVERYTHING for the active expert(s) — trajectory (thought/tool_call/observation) +
raw LM I/O (lm_io) + extract I/O (extract_io) + answer-so-far (answer), per scope —
while the PROMPT the model reads is a VIEW of only the WORKING-SET kinds. The new
atoms are part of the complete freeze-anytime state but MUST be excluded from
render_keys (the prompt) AND from _maybe_autocompact (the compaction target).

These tests drive the REAL `_RetainingReAct.forward` with the lm_io sink wired and a
scripted DummyLM (so the loop is deterministic and the lm_io boundary fires), then
assert the full state, the prompt invariance, the compaction scoping, and that two
overlapping scopes are each self-complete and don't bleed.
"""

from __future__ import annotations

import types

import dspy
from dspy.utils.dummies import DummyLM

import clio_agent.config as config
import clio_agent.gact.app as app
from clio_agent.arc.memory import ARCMemory
from clio_agent.gact import context as ctx

from .conftest import live_plane_context, make_react_agent

SID = "freeze-s1"
SCOPE = "agentA/expertX"
SCOPE_B = "agentB/expertY"


class _SinkFiringDummyLM(DummyLM):
    """A scripted DummyLM that fires the lm_io capture sink on every call — the way
    the real ``config.IOLoggingLM`` boundary does (``_clio_log_last_call`` runs on
    EVERY call path and invokes ``config._LM_IO_SINK``). DummyLM is not our
    IOLoggingLM subclass, so without this the boundary never fires and lm_io is never
    captured; this models that boundary faithfully (the boundary->sink->ARC chain
    itself is unit-tested in test_lm_io_seam.py)."""

    def __call__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        out = super().__call__(*args, **kwargs)
        sink = config._LM_IO_SINK
        if sink is not None:
            last = self.history[-1] if getattr(self, "history", None) else {}
            sink(
                {
                    "model": getattr(self, "model", "dummy/model"),
                    "messages": last.get("messages") if isinstance(last, dict) else None,
                    "content": str(out),
                    "reasoning_content": "",
                    "finish_reason": "stop",
                    "usage": last.get("usage") if isinstance(last, dict) else None,
                }
            )
        return out


def _scripted_lm() -> DummyLM:
    """One search step, then finish, then the extract."""
    return _SinkFiringDummyLM(
        [
            {
                "next_thought": "search first",
                "next_tool_name": "search",
                "next_tool_args": '{"q": "alpha"}',
            },
            {"next_thought": "done", "next_tool_name": "finish", "next_tool_args": "{}"},
            {"reasoning": "because", "answer": "FINAL_ANSWER"},
        ]
    )


def _run_turn(arc: ARCMemory, scope: str, *, turn_id: str = "TURN_1") -> None:
    """Drive ONE faked-LM expert turn through _RetainingReAct.forward in a scope,
    with the lm_io sink wired so the raw LM I/O is captured as lm_io segments."""
    agent = make_react_agent()
    fake_app = types.SimpleNamespace(state=types.SimpleNamespace(arc=arc))

    # Wire the lm_io seam exactly as build_app does, routing into THIS arc.
    config.set_lm_io_sink(lambda record: app._route_lm_io_to_arc(fake_app, record))

    sess_token = ctx.set_session_id(SID)
    app_token = ctx.set_app(fake_app)
    ctx.set_turn_id(turn_id)
    try:
        with live_plane_context(arc, session=SID, scope=scope):
            with dspy.context(lm=_scripted_lm(), adapter=dspy.ChatAdapter()):
                agent(question="find alpha")
    finally:
        ctx.reset(app_token)
        ctx.reset(sess_token)
        config.set_lm_io_sink(None)


def test_scope_holds_full_state_after_turn(arc):
    _run_turn(arc, SCOPE)

    all_segs = arc.render_segments(SID, SCOPE)
    kinds = {s.kind for s in all_segs}
    # trajectory (working set) + all three freeze-anytime atoms present:
    assert {"thought", "tool_call", "observation"} <= kinds, f"trajectory missing; got {kinds}"
    assert "lm_io" in kinds, "raw LM I/O atom missing from frozen state"
    assert "extract_io" in kinds, "extract I/O atom missing from frozen state"
    assert "answer" in kinds, "answer-so-far atom missing from frozen state"

    # correlation stamped on ALL writes (one expert span per turn).
    spans = {s.expert_span_id for s in all_segs}
    assert spans and "" not in spans, f"unstamped expert_span_id on some segment: {spans}"
    assert len(spans) == 1, f"a single turn must share one expert span; got {spans}"
    assert all(s.turn_id == "TURN_1" for s in all_segs), "turn_id not consistent across writes"

    # the answer atom carries the final message; extract_io holds input+output.
    answer = next(s for s in all_segs if s.kind == "answer")
    assert answer.content["text"] == "FINAL_ANSWER"
    extract_io = next(s for s in all_segs if s.kind == "extract_io")
    assert "input" in extract_io.content and "output" in extract_io.content


def test_prompt_unchanged_with_atoms_present(arc):
    """Byte-equality of the prompt VIEW whether or not the new atoms are present."""
    with live_plane_context(arc, session=SID, scope=SCOPE):
        # a normal working-set trajectory
        arc.append_segment(SID, SCOPE, "thought", {"text": "T0"}, step=0)
        arc.append_segment(SID, SCOPE, "tool_call", {"name": "a", "args": {}}, step=0)
        arc.append_segment(SID, SCOPE, "observation", {"text": "O0"}, step=0)
        keys_before = arc.render_segments_keys(SID, SCOPE)
        # inject all three new atoms directly
        arc.append_segment(SID, SCOPE, "lm_io", {"content": "raw"}, step=-1)
        arc.append_segment(SID, SCOPE, "extract_io", {"input": {}, "output": {}}, step=-1)
        arc.append_segment(SID, SCOPE, "answer", {"text": "FINAL"}, step=-1)
        keys_after = arc.render_segments_keys(SID, SCOPE)
    assert keys_after == keys_before  # BYTE-EQUAL: atoms never enter the prompt


def test_compaction_only_touches_working_set(arc, monkeypatch):
    """Auto-compaction folds the trajectory into one summary but leaves the
    freeze-anytime atoms untouched (not summarized away, not tombstoned)."""
    monkeypatch.setattr(app, "_last_prompt_tokens", lambda: 900)  # 0.90 >= 0.85
    monkeypatch.setattr(app, "_summarize_segments_llm", lambda segs: "COMPACT_SUMMARY")

    with live_plane_context(arc, session=SID, scope=SCOPE, window=1000):
        arc.append_segment(SID, SCOPE, "thought", {"text": "T0"}, step=0)
        arc.append_segment(SID, SCOPE, "tool_call", {"name": "a", "args": {}}, step=0)
        arc.append_segment(SID, SCOPE, "observation", {"text": "O0"}, step=0)
        arc.append_segment(SID, SCOPE, "lm_io", {"content": "raw"}, step=-1)
        arc.append_segment(SID, SCOPE, "answer", {"text": "FINAL"}, step=-1)
        agent = make_react_agent()
        agent._maybe_autocompact()

    live_kinds = [s.kind for s in arc.render_segments(SID, SCOPE)]
    assert "summary" in live_kinds  # trajectory compacted
    assert "lm_io" in live_kinds and "answer" in live_kinds  # atoms NOT compacted
    # the prompt is exactly the one compacted summary observation
    assert arc.render_segments_keys(SID, SCOPE) == {"observation_0": "COMPACT_SUMMARY"}
    # the summary text did NOT swallow the raw LM I/O
    summary = next(s for s in arc.render_segments(SID, SCOPE) if s.kind == "summary")
    assert summary.content["text"] == "COMPACT_SUMMARY"


def test_two_overlapping_scopes_each_complete(arc):
    """Two expert turns into DIFFERENT scopes: each scope is self-complete and the
    two do not bleed (distinct expert span per scope)."""
    _run_turn(arc, SCOPE, turn_id="TURN_A")
    _run_turn(arc, SCOPE_B, turn_id="TURN_B")

    for scope in (SCOPE, SCOPE_B):
        kinds = {s.kind for s in arc.render_segments(SID, scope)}
        assert {"lm_io", "extract_io", "answer"} <= kinds, (
            f"scope {scope} not self-complete; got {kinds}"
        )

    spans_a = {s.expert_span_id for s in arc.render_segments(SID, SCOPE)}
    spans_b = {s.expert_span_id for s in arc.render_segments(SID, SCOPE_B)}
    assert spans_a.isdisjoint(spans_b), "expert spans bled across scopes"
    turns_a = {s.turn_id for s in arc.render_segments(SID, SCOPE)}
    turns_b = {s.turn_id for s in arc.render_segments(SID, SCOPE_B)}
    assert turns_a == {"TURN_A"} and turns_b == {"TURN_B"}
