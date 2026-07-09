"""Behavioral tests for the guard-free agent loop (issue #768).

The deterministic guards removed in #768 (similarity vetoes, summary
templates, the forward() answer-wipe, and deterministic-first answer
ordering) silently replaced or discarded model output. These tests pin
the correct model-decides behavior:

- Repeating a question returns the model's repeated answer (no
  similarity veto raising a synthetic RoutingError).
- A "summarize the previous answers" question returns the chat model's
  answer, never a deterministic "Previous answers covered:" template.
- A turn whose only tool failed surfaces the model's explanation answer
  with the failed-tool provenance intact, instead of a fabricated blank
  ToolError.
- When the planner produces an empty answer, LM synthesis is attempted
  before any deterministic observation dump.
"""

from __future__ import annotations

import json
from typing import Any

import dspy
import pytest

from clio_agent.agent import ClioAgent
from clio_agent.harness import RouteDecision, RunTrace


class _StubExecutor:
    """Minimal in-memory tool executor with one scripted tool result."""

    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_tool_names(self) -> list[str]:
        return ["shell_bash"]

    def to_dspy_tools(self) -> list[dspy.Tool]:
        return []

    def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        self.calls.append((name, dict(args)))
        if self._error is not None:
            raise self._error
        return self._result


def _scripted_planner(actions: list[dict[str, Any]]):
    """Return an action_planner stub yielding one scripted action per call."""
    queue = list(actions)

    def planner(**_: Any) -> dspy.Prediction:
        return dspy.Prediction(action_json=json.dumps(queue.pop(0)))

    return planner


def _trace() -> RunTrace:
    return RunTrace(
        route=RouteDecision(
            target="chat",
            source="dspy",
            reason="test route",
            confidence=0.5,
        )
    )


@pytest.fixture
def agent(tmp_path):
    instance = ClioAgent(data_dir=str(tmp_path / "agent"))
    yield instance
    instance.shutdown()


def test_repeated_question_returns_models_repeated_answer(agent) -> None:
    """No similarity veto: the model may repeat a prior answer verbatim."""
    repeated = (
        "The dataset contains hourly surface temperature readings collected "
        "from twelve coastal stations during 2024."
    )
    agent.action_planner = _scripted_planner([{"action": "answer", "answer": repeated}])

    selected, answer, _expert_result, error_info, _route = agent._run_agent_loop(
        question="What does the dataset contain?",
        session_context=f"assistant: {repeated}",
        file_context="",
        trace=_trace(),
    )

    assert answer == repeated
    assert error_info is None
    assert selected == "chat"


def test_summary_question_returns_chat_model_answer(agent) -> None:
    """Summaries come from the chat model, not a deterministic template."""
    chat_answer = "We profiled your HDF5 file and then charted the temperature trends."
    agent.chat_agent = lambda **_: dspy.Prediction(answer=chat_answer)
    session_context = (
        "assistant: The HDF5 file has two datasets under /simulation.\n"
        "assistant: The temperature histogram was written to charts/temp.png."
    )

    answer = agent._run_chat_agent(
        "Please summarize the previous answers.",
        session_context,
    )

    assert answer == chat_answer
    assert not answer.startswith("Previous answers covered:")


def test_failed_tool_turn_surfaces_model_explanation(agent) -> None:
    """A failed tool does not wipe the model's explanation into a blank ToolError."""
    explanation = (
        "The status command failed because the backend was unreachable, so no "
        "diagnostics were collected this turn."
    )
    agent.tool_executor = _StubExecutor(error=RuntimeError("backend unreachable"))
    agent._workspace_tool_executors = {}
    agent.action_planner = _scripted_planner(
        [
            {"action": "tool", "tool": "shell_bash", "args": {"command": "status"}},
            {"action": "answer", "answer": explanation},
        ]
    )

    prediction = agent.forward(question="Check the current status", session_id="guardfree-3")

    assert prediction.answer == explanation
    assert prediction.error_info is None
    # The failed tool call stays visible in provenance.
    assert len(prediction.tools_called) == 1
    tool_result = prediction.tools_called[0].result
    assert tool_result["ok"] is False
    assert "backend unreachable" in tool_result["error"]["message"]


def test_empty_planner_answer_tries_lm_synthesis_first(agent) -> None:
    """An empty planner answer goes to LM synthesis before any deterministic dump."""
    synthesized = "The listing shows two datasets: temperature and pressure."
    agent.tool_executor = _StubExecutor(result={"datasets": ["temperature", "pressure"]})
    agent._workspace_tool_executors = {}
    agent.answer_synthesizer = lambda **_: dspy.Prediction(answer=synthesized)
    agent.action_planner = _scripted_planner(
        [
            {"action": "tool", "tool": "shell_bash", "args": {"command": "list"}},
            {"action": "answer", "answer": ""},
        ]
    )

    _selected, answer, _expert_result, error_info, _route = agent._run_agent_loop(
        question="List the datasets",
        session_context="",
        file_context="",
        trace=_trace(),
    )

    assert answer == synthesized
    assert error_info is None
