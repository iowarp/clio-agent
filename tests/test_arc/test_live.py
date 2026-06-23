"""Tests for the ARC live view: folding semantic events into runtime context.

Guards S5 -- the LiveRuntimeContext fold and its Invocation/Conversation
projections, plus ARCMemory's live-context wiring and lifecycle release.
"""

import pytest

from clio_agent.arc.live import EVENTS_SCOPE, LiveRuntimeContext
from clio_agent.arc.memory import ARCMemory
from clio_agent.gact.semantic_events import SemanticEvent


def _ev(event_type, *, sid="s1", turn="t1", trace="trace_t1", occurred="", **kw):
    return SemanticEvent(
        event_type=event_type,
        session_id=sid,
        trace_id=trace,
        turn_id=turn,
        occurred_at=occurred or "2026-06-14T00:00:00+00:00",
        **kw,
    )


def _earthscope_turn_events():
    return [
        _ev(
            "turn.started",
            occurred="2026-06-14T00:00:00+00:00",
            payload={"input": "stations near San Diego"},
        ),
        _ev(
            "expert.response.completed",
            occurred="2026-06-14T00:00:02+00:00",
            actor={"agent_id": "data"},
            payload={
                "answer": "Found 71 stations.",
                "reasoning": "thought about it",
                "trajectory": {
                    "thought_0": "x",
                    "tool_name_0": "shell_bash",
                    "observation_0": "ok",
                },
                "tools_called": [{"name": "shell_bash", "ok": True}],
            },
        ),
        _ev(
            "turn.completed",
            occurred="2026-06-14T00:00:03+00:00",
            actor={"agent_id": "orchestrator"},
            payload={
                "final_message": {
                    "id": "m1",
                    "parts": [{"type": "text", "text": "71 stations near San Diego."}],
                }
            },
        ),
    ]


class TestLiveFold:
    def test_view_summarizes_recent_turns(self):
        live = LiveRuntimeContext()
        for e in _earthscope_turn_events():
            live.fold(e)

        view = live.view("s1")
        assert len(view["turns"]) == 1
        turn = view["turns"][0]
        assert turn["question"] == "stations near San Diego"
        assert "71 stations near San Diego." in turn["answer"]
        assert turn["status"] == "success"
        assert "data" in turn["experts"]

    def test_project_conversation_pairs_question_and_answer(self):
        live = LiveRuntimeContext()
        for e in _earthscope_turn_events():
            live.fold(e)

        conv = live.project_conversation("s1", user_id="u1")
        assert conv is not None
        roles = [m.role for m in conv.messages]
        assert roles == ["user", "assistant"]
        assert conv.messages[0].content == "stations near San Diego"
        assert "71 stations" in conv.messages[1].content

    def test_project_invocations_per_expert(self):
        live = LiveRuntimeContext()
        for e in _earthscope_turn_events():
            live.fold(e)

        invs = live.project_invocations("s1")
        assert len(invs) == 1
        inv = invs[0]
        assert inv.agent_id == "data"
        assert inv.tier == 2
        assert inv.status == "success"
        assert inv.input == {"question": "stations near San Diego"}
        assert inv.duration_ms == pytest.approx(3000.0)  # 00:00 -> 00:03
        assert inv.performance["trajectory_steps"] == 1

    def test_turn_with_no_expert_projects_tier1(self):
        live = LiveRuntimeContext()
        live.fold(_ev("turn.started", payload={"input": "hi"}))
        live.fold(
            _ev(
                "llm.response.completed",
                actor={"agent_id": "chat"},
                payload={"selected_expert": "chat", "answer": "hello"},
            )
        )
        live.fold(_ev("turn.completed", payload={}))

        invs = live.project_invocations("s1")
        assert len(invs) == 1
        assert invs[0].tier == 1
        assert invs[0].agent_id == "chat"

    def test_release_drops_session(self):
        live = LiveRuntimeContext()
        for e in _earthscope_turn_events():
            live.fold(e)
        assert live.view("s1")
        released = live.release("s1")
        assert released == 1
        assert live.view("s1") == {}

    def test_fold_never_raises_on_garbage(self):
        live = LiveRuntimeContext()
        live.fold(object())  # missing all attributes -> swallowed
        live.fold(_ev("turn.started", sid=""))  # empty sid ignored
        assert live.view("s1") == {}


class TestARCMemoryLiveWiring:
    def test_on_semantic_event_populates_live_context(self, tmp_path):
        arc = ARCMemory(data_dir=str(tmp_path / "arc"))
        for e in _earthscope_turn_events():
            arc.on_semantic_event(e)

        view = arc.get_live_context("s1")
        assert view["turns"][0]["question"] == "stations near San Diego"
        conv = arc.project_live_conversation("s1")
        assert conv is not None and len(conv.messages) == 2

    def test_release_session_clears_live(self, tmp_path):
        arc = ARCMemory(data_dir=str(tmp_path / "arc"))
        for e in _earthscope_turn_events():
            arc.on_semantic_event(e)
        result = arc.release_session("s1")
        assert result["live"] == 1
        assert arc.get_live_context("s1") == {}

    def test_flush_and_release_clears_live(self, tmp_path):
        arc = ARCMemory(data_dir=str(tmp_path / "arc"))
        for e in _earthscope_turn_events():
            arc.on_semantic_event(e)
        arc.flush_and_release()
        assert arc.get_live_context("s1") == {}


def _multi_turn_corpus():
    """A 2-turn, multi-expert event stream exercising every folded event type."""
    return [
        # ---- turn t1: two experts + a tool call, completed ----
        _ev("turn.started", turn="t1", payload={"input": "stations near San Diego"}),
        _ev(
            "tool.call.completed",
            turn="t1",
            actor={"tool": "shell_bash"},
            status="completed",
        ),
        _ev(
            "expert.response.completed",
            turn="t1",
            occurred="2026-06-14T00:00:02+00:00",
            actor={"agent_id": "data"},
            payload={
                "answer": "Found 71 stations.",
                "reasoning": "thought about it",
                "trajectory": {"thought_0": "x", "tool_name_0": "shell_bash", "observation_0": "ok"},
                "tools_called": [{"name": "shell_bash", "ok": True}],
            },
        ),
        _ev(
            "expert.response.completed",
            turn="t1",
            occurred="2026-06-14T00:00:02+00:00",
            actor={"agent_id": "analysis"},
            payload={
                "answer": "Most are GNSS.",
                "reasoning": "more thinking here",
                "trajectory": {"thought_0": "y"},
                "tools_called": [],
            },
        ),
        _ev(
            "turn.completed",
            turn="t1",
            occurred="2026-06-14T00:00:03+00:00",
            payload={
                "final_message": {
                    "id": "m1",
                    "parts": [{"type": "text", "text": "71 stations near San Diego."}],
                }
            },
        ),
        # ---- turn t2: no expert, a routing decision, completed ----
        _ev(
            "turn.started",
            turn="t2",
            trace="trace_t2",
            occurred="2026-06-14T00:01:00+00:00",
            payload={"input": "hello"},
        ),
        _ev(
            "llm.response.completed",
            turn="t2",
            trace="trace_t2",
            actor={"agent_id": "chat"},
            payload={"selected_expert": "chat", "route_reason": "small talk", "answer": "hi"},
        ),
        _ev(
            "turn.completed",
            turn="t2",
            trace="trace_t2",
            occurred="2026-06-14T00:01:02+00:00",
            payload={},
        ),
    ]


class TestBufferBacked:
    """One log: the observer's state IS the single ``_events`` segment log (no private
    dict, no separate folded copy) — it PROJECTS its records over that log."""

    def test_record_appends_semantic_event_segment(self, tmp_path):
        arc = ARCMemory(data_dir=str(tmp_path / "arc"))
        arc.on_semantic_event(_ev("turn.started", payload={"input": "hi"}))

        segs = arc.render_segments("s1", EVENTS_SCOPE)
        assert len(segs) == 1
        seg = segs[0]
        assert seg.kind == "semantic_event"
        assert seg.turn_id == "t1"
        assert seg.content["event_type"] == "turn.started"
        assert seg.content["payload"]["input"] == "hi"

    def test_no_private_sessions_dict(self):
        """The event-fold's separate structure is retired -- there is no
        self._sessions on the log-backed observer."""
        live = LiveRuntimeContext()
        assert not hasattr(live, "_sessions")

    def test_expert_scope_working_set_excludes_events(self, tmp_path):
        """The reserved '_events' log scope is INVISIBLE to a normal expert scope's
        working-set render: it never enters the model prompt."""
        arc = ARCMemory(data_dir=str(tmp_path / "arc"))
        # Observer records events (writes to the '_events' log scope)...
        for e in _multi_turn_corpus():
            arc.on_semantic_event(e)
        # ...and the expert writes its own working-set segments to its scope.
        arc.append_segment("s1", "agentA", "thought", {"text": "T0"}, step=0)
        arc.append_segment("s1", "agentA", "observation", {"text": "O0"}, step=0)

        ws = arc.render_working_set("s1", "agentA")
        assert {s.kind for s in ws} == {"thought", "observation"}
        assert all(s.kind != "semantic_event" for s in ws)
        assert all(s.scope == "agentA" for s in ws)

        # The trajectory projection (the model prompt) likewise carries no '_events'.
        keys = arc.render_segments_keys("s1", "agentA")
        assert keys == {"thought_0": "T0", "observation_0": "O0"}

    def test_equivalence_multi_turn_multi_expert(self, tmp_path):
        """Feeding a multi-turn, multi-expert corpus through on_semantic_event
        yields the SAME projection shapes/values the buffer-free fold produced."""
        arc = ARCMemory(data_dir=str(tmp_path / "arc"))
        for e in _multi_turn_corpus():
            arc.on_semantic_event(e)

        # view: two turns, recent-last, with the same compact shape.
        view = arc.get_live_context("s1")
        assert [t["turn_id"] for t in view["turns"]] == ["t1", "t2"]
        t1, t2 = view["turns"]
        assert t1["question"] == "stations near San Diego"
        assert "71 stations near San Diego." in t1["answer"]
        assert t1["status"] == "success"
        assert t1["experts"] == ["data", "analysis"]
        assert t1["tools"] == ["shell_bash"]
        assert t2["question"] == "hello"
        assert t2["selected_expert"] == "chat"

        # conversation: Q/A pairs across both turns.
        conv = arc.project_live_conversation("s1", user_id="u1")
        assert conv is not None
        assert [m.role for m in conv.messages] == ["user", "assistant", "user", "assistant"]
        assert conv.messages[0].content == "stations near San Diego"
        assert "71 stations" in conv.messages[1].content
        assert conv.messages[2].content == "hello"

        # invocations: two tier-2 (t1's experts) + one tier-1 (t2, no expert).
        invs = arc.project_live_invocations("s1")
        by_agent = {i.agent_id: i for i in invs}
        assert set(by_agent) == {"data", "analysis", "chat"}
        assert by_agent["data"].tier == 2
        assert by_agent["data"].status == "success"
        assert by_agent["data"].input == {"question": "stations near San Diego"}
        assert by_agent["data"].duration_ms == pytest.approx(3000.0)
        assert by_agent["data"].performance["trajectory_steps"] == 1
        assert by_agent["data"].performance["tool_count"] == 1
        assert by_agent["analysis"].tier == 2
        assert by_agent["analysis"].performance["tool_count"] == 0
        assert by_agent["chat"].tier == 1
        assert by_agent["chat"].output == {"answer": "hi"}

    def test_equivalence_matches_standalone_fold(self, tmp_path):
        """Buffer-backed-via-ARC and a standalone LiveRuntimeContext fed the SAME
        corpus produce identical projections (the fold logic did not drift)."""
        arc = ARCMemory(data_dir=str(tmp_path / "arc"))
        live = LiveRuntimeContext()
        for e in _multi_turn_corpus():
            arc.on_semantic_event(e)
            live.fold(e)

        assert arc.get_live_context("s1") == live.view("s1")

        arc_invs = arc.project_live_invocations("s1")
        live_invs = live.project_invocations("s1")
        assert len(arc_invs) == len(live_invs)
        for a, b in zip(arc_invs, live_invs, strict=True):
            assert a.agent_id == b.agent_id
            assert a.tier == b.tier
            assert a.status == b.status
            assert a.input == b.input
            assert a.output == b.output
            assert a.duration_ms == b.duration_ms
            assert a.performance == b.performance

    def test_release_returns_to_baseline(self, tmp_path):
        """release erases the '_events' log scope from the buffer (idle -> baseline)."""
        arc = ARCMemory(data_dir=str(tmp_path / "arc"))
        for e in _multi_turn_corpus():
            arc.on_semantic_event(e)
        assert arc.render_segments("s1", EVENTS_SCOPE)  # log holds the events

        result = arc.release_session("s1")
        assert result["live"] == 2  # two turns released
        assert arc.render_segments("s1", EVENTS_SCOPE) == []  # scope erased
        assert arc.get_live_context("s1") == {}
