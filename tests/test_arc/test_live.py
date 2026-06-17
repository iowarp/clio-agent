"""Tests for the ARC live view: folding semantic events into runtime context.

Guards S5 -- the LiveRuntimeContext fold and its Invocation/Conversation
projections, plus ARCMemory's live-context wiring and lifecycle release.
"""

import pytest

from clio_agent.arc.live import LiveRuntimeContext
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
