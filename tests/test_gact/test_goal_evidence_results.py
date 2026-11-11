"""The goal judge reads the recorded evidence whole, up to its own context window."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import dspy
import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.gact import goal as goal_mod
from clio_agent.gact import goal_evidence
from clio_agent.gact.goal_evidence import (
    EVIDENCE_EXCEEDS_JUDGE_CONTEXT,
    JUDGE_CONTEXT_WINDOW_UNKNOWN,
    JudgeRoom,
    _build_transcript,
    _tool_result_fact,
    bounded_inputs,
    judge_room,
)
from tests.test_arc.conftest import core_arc as core_arc
from tests.test_gact.test_goal import _app, _bind, _in_ctx, _session
from tests.test_gact.test_goal_judge_offloop import _fake_response


def test_judge_sees_command_outcome_and_whole_output_after_large_command() -> None:
    stdout = "verbose diagnostics\n" * 1000 + "32 passed in 1.4s"
    result = {
        "command": "authoring code " * 2000,
        "exit_code": 0,
        "timed_out": False,
        "stdout": stdout,
        "stderr": "",
    }
    for part in (
        SimpleNamespace(structured_content=result),
        SimpleNamespace(metadata={"result": {"structuredContent": result}}),
    ):
        parsed = json.loads(_tool_result_fact(part))
        assert parsed["exit_code"] == 0 and parsed["timed_out"] is False
        assert parsed["stdout"] == stdout
        assert "command" not in parsed


def test_inputs_are_whole_and_credentials_are_redacted_at_any_depth() -> None:
    deep: dict[str, Any] = {"api_key": "PRIVATE_KEY", "value": "x" * 5000}
    for level in range(10):
        deep = {f"level{level}": deep}
    inputs = {f"key{n}": n for n in range(60)} | {"items": list(range(50)), "deep": deep}
    rendered = json.loads(bounded_inputs(inputs))
    assert rendered["key59"] == 59 and rendered["items"] == list(range(50))
    leaf = rendered["deep"]
    for level in reversed(range(10)):
        leaf = leaf[f"level{level}"]
    assert leaf == {"api_key": "[redacted]", "value": "x" * 5000}
    assert "PRIVATE_KEY" not in bounded_inputs(inputs)


def _message(index: int, text: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"m{index}",
        role="assistant",
        created_at=f"2026-11-01T00:00:{index:02d}Z",
        parts=[SimpleNamespace(type="text", text=text)],
    )


def _messages_app(*messages: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(messages={"s": list(messages)}))


def test_every_message_injection_and_result_is_given_whole_without_a_window() -> None:
    long_text = "phase finished " * 2000
    messages = [_message(index, f"entry {index}") for index in range(60)]
    messages.append(
        SimpleNamespace(
            id="inject",
            role="system",
            created_at="2026-11-01T00:01:00Z",
            parts=[SimpleNamespace(type="injection", source="task_results", text=long_text)],
        )
    )
    transcript = _build_transcript(_messages_app(*messages), "s")
    assert "assistant: entry 0" in transcript and "assistant: entry 59" in transcript
    assert long_text.strip() in transcript
    assert EVIDENCE_EXCEEDS_JUDGE_CONTEXT not in transcript


def test_a_full_window_keeps_the_newest_entries_whole_and_says_what_is_left_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[Any, ...]] = []
    monkeypatch.setattr(goal_evidence.trace, "event", lambda *args: events.append(args))
    messages = [_message(index, f"entry {index} " + "detail " * 40) for index in range(10)]
    entry_tokens = len(_build_transcript(_messages_app(messages[0]), "s")) // 4 + 1
    room = JudgeRoom(tokens=entry_tokens * 3 + 2, window=4096)
    transcript = _build_transcript(_messages_app(*messages), "s", room=room)
    kept = [index for index in range(10) if f"entry {index} " in transcript]
    assert kept == [7, 8, 9]
    assert transcript.count("detail") == 3 * 40  # kept entries are whole
    first_line = transcript.splitlines()[0]
    assert EVIDENCE_EXCEEDS_JUDGE_CONTEXT in first_line and "4096 tokens" in first_line
    assert "the 7 earliest entries" in first_line and "progress ledger" in first_line
    assert events and events[0][2] == EVIDENCE_EXCEEDS_JUDGE_CONTEXT and events[0][6] == 7


def test_a_newest_entry_larger_than_the_window_keeps_its_most_recent_part() -> None:
    text = "start " + "middle " * 400 + "final verdict line"
    transcript = _build_transcript(
        _messages_app(_message(0, "older"), _message(1, text)),
        "s",
        room=JudgeRoom(tokens=50, window=100),
    )
    assert "final verdict line" in transcript and "start" not in transcript
    assert "earlier characters of this entry do not fit" in transcript
    assert "older" not in transcript and "the 1 earliest entries" in transcript


def test_the_judge_room_comes_from_the_judge_models_window() -> None:
    config = SimpleNamespace(model="judge-model", context_window=10_000, max_tokens=0)
    lm = SimpleNamespace(model="judge-model", _clio_provider_config=config)
    room = judge_room(lm, other_inputs="x" * 400)
    assert room == JudgeRoom(tokens=int(10_000 * 0.85) - 100, window=10_000)
    config.max_tokens = 4000  # the declared answer cannot be crowded out
    assert judge_room(lm, other_inputs="x" * 400) == JudgeRoom(tokens=5900, window=10_000)


def test_an_unknown_judge_window_gives_the_evidence_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[Any, ...]] = []
    monkeypatch.setattr(goal_evidence.trace, "event", lambda *args: events.append(args))
    unknown = SimpleNamespace(model="", context_window=0)
    assert judge_room(SimpleNamespace(_clio_provider_config=unknown), other_inputs="") is None
    assert events[0][2] == JUDGE_CONTEXT_WINDOW_UNKNOWN


class _WindowedJudgeLM(dspy.BaseLM):
    """A judge with a small declared window that records what it was asked."""

    def __init__(self, window: int) -> None:
        super().__init__(model="windowed/judge")
        self._clio_provider_config = SimpleNamespace(
            model="windowed/judge", context_window=window, max_tokens=0
        )
        self.prompts: list[str] = []

    def forward(self, prompt: Any = None, messages: Any = None, **kwargs: Any) -> Any:
        raise AssertionError("the judge is awaited through the async path")

    async def aforward(self, prompt: Any = None, messages: Any = None, **kwargs: Any) -> Any:
        self.prompts.append(json.dumps(messages))
        return _fake_response(
            "[[ ## progress ## ]]\nRecent phases verified.\n\n[[ ## met ## ]]\nFalse\n\n"
            "[[ ## reason ## ]]\nmore work\n\n[[ ## next_action ## ]]\ncontinue\n\n"
            "[[ ## completed ## ]]"
        )


def test_the_live_judge_reads_evidence_fitted_to_its_own_window(
    tmp_path: Path, arc: ARCMemory
) -> None:
    lm = _WindowedJudgeLM(window=2000)

    def body() -> None:
        app = _app(tmp_path, arc)
        app.state.agent = SimpleNamespace(_main_lm=lm, _dspy_adapter=dspy.ChatAdapter())
        sid = _session(app)
        _bind(app, sid)
        app.state.messages[sid] = [
            _message(index, f"phase {index} " + "log " * 200) for index in range(20)
        ]
        verdict = asyncio.run(goal_mod.run_llm_judge(app, sid, {"condition": "all phases"}))
        assert verdict.available and verdict.progress == "Recent phases verified."

    with pytest.warns(
        DeprecationWarning,
        match=r"Implementing custom LMs through BaseLM|Passing OpenAI-style message dictionaries",
    ):
        _in_ctx(body)
    prompt = lm.prompts[0]
    assert EVIDENCE_EXCEEDS_JUDGE_CONTEXT in prompt and "phase 19 " in prompt
    assert "phase 0 " not in prompt


def test_the_judges_progress_ledger_is_retained_whole() -> None:
    ledger = "verified milestone\n" * 1000
    decision = goal_mod.evaluate_goal(
        {"active": True, "condition": "all phases", "iters_elapsed": 0},
        llm=goal_mod.GoalJudgement(met=False, reason="more", progress=ledger),
    )
    assert decision.new_state["progress"] == ledger


def test_a_plan_is_given_whole_and_cut_only_by_the_window(tmp_path: Path) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n" + "- step verified\n" * 2000 + "- last step\n", encoding="utf-8")
    session = SimpleNamespace(metadata={"plan_file": str(plan)})
    app = SimpleNamespace(
        state=SimpleNamespace(messages={"s": [_message(0, "done")]}, sessions={"s": session})
    )
    whole = _build_transcript(app, "s")
    assert "- last step" in whole and whole.count("- step verified") == 2000
    narrow = _build_transcript(app, "s", room=JudgeRoom(tokens=100, window=1000))
    assert narrow.startswith("--- session plan (no status) ---\n# Plan")
    assert "later characters of the working state do not fit" in narrow
    assert "- last step" not in narrow and "the 1 earliest entries" in narrow
