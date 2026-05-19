"""Unit tests for ClioAgent planner-loop internals.

Covers the deterministic helpers and the _run_agent_loop branches that
test_agent_dispatch.py does not exercise -- pure parsing/formatting
helpers, capability context assembly, the tool / unsupported-action /
step-limit branches of the loop, and the observation fallback answer.
All without a live LM.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from clio_agent.agent import ClioAgent
from clio_agent.errors import RoutingError
from clio_agent.harness import RouteDecision, RunTrace


@pytest.fixture
def agent(tmp_path):
    """A ClioAgent with an isolated data dir."""
    a = ClioAgent(data_dir=str(tmp_path / "clio"), verbose=False)
    yield a
    a.shutdown()


def _trace() -> RunTrace:
    return RunTrace(
        route=RouteDecision(
            target="chat", source="dspy", reason="test", confidence=0.0
        )
    )


# --------------------------------------------------------------------------
# Pure static / class helpers -- no agent instance needed
# --------------------------------------------------------------------------


class TestCoerceText:
    def test_none_is_empty(self):
        assert ClioAgent._coerce_text(None) == ""

    def test_str_passthrough(self):
        assert ClioAgent._coerce_text("hello") == "hello"

    def test_scalars(self):
        assert ClioAgent._coerce_text(7) == "7"
        assert ClioAgent._coerce_text(1.5) == "1.5"
        assert ClioAgent._coerce_text(True) == "True"

    def test_list_and_dict_json(self):
        assert ClioAgent._coerce_text([1, 2]) == "[1, 2]"
        assert ClioAgent._coerce_text({"a": 1}) == '{"a": 1}'

    def test_object_with_content(self):
        obj = MagicMock(spec=["content"])
        obj.content = "from content"
        assert ClioAgent._coerce_text(obj) == "from content"

    def test_object_with_model_dump(self):
        obj = MagicMock(spec=["model_dump"])
        obj.model_dump.return_value = {"k": "v"}
        assert ClioAgent._coerce_text(obj) == '{"k": "v"}'

    def test_fallback_str(self):
        assert ClioAgent._coerce_text(object()) != ""


class TestParseActionJson:
    def test_dict_input(self):
        assert ClioAgent._parse_action_json({"action": "answer", "answer": "x"})["action"] == "answer"

    def test_plain_json_string(self):
        out = ClioAgent._parse_action_json('{"action": "tool", "tool": "t"}')
        assert out["action"] == "tool" and out["tool"] == "t"

    def test_fenced_json_block(self):
        out = ClioAgent._parse_action_json('```json\n{"action": "none"}\n```')
        assert out["action"] == "none"

    def test_embedded_object(self):
        out = ClioAgent._parse_action_json('noise before {"action": "expert"} noise after')
        assert out["action"] == "expert"

    def test_action_is_lowercased(self):
        assert ClioAgent._parse_action_json({"action": "ANSWER", "answer": "x"})["action"] == "answer"

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError):
            ClioAgent._parse_action_json("not json at all")

    def test_non_object_json_raises(self):
        with pytest.raises(ValueError):
            ClioAgent._parse_action_json("[1, 2, 3]")

    def test_unsupported_action_raises(self):
        with pytest.raises(ValueError):
            ClioAgent._parse_action_json({"action": "explode"})


class TestNormalizeToolArgs:
    def test_mapping(self):
        assert ClioAgent._normalize_tool_args({"a": 1}) == {"a": 1}

    def test_json_string(self):
        assert ClioAgent._normalize_tool_args('{"a": 1}') == {"a": 1}

    def test_invalid_string_is_empty(self):
        assert ClioAgent._normalize_tool_args("not json") == {}

    def test_non_mapping_json_is_empty(self):
        assert ClioAgent._normalize_tool_args("[1, 2]") == {}

    def test_none_is_empty(self):
        assert ClioAgent._normalize_tool_args(None) == {}


class TestDecodeToolResult:
    def test_json_string_decoded(self):
        assert ClioAgent._decode_tool_result('{"k": 1}') == {"k": 1}

    def test_non_json_string_passthrough(self):
        assert ClioAgent._decode_tool_result("plain text") == "plain text"

    def test_non_string_passthrough(self):
        assert ClioAgent._decode_tool_result({"k": 1}) == {"k": 1}


class TestFirstSentence:
    def test_truncates_at_period(self):
        assert ClioAgent._first_sentence("First one. Second one.") == "First one."

    def test_collapses_whitespace(self):
        assert ClioAgent._first_sentence("a   b\n\tc") == "a b c"

    def test_long_text_truncated(self):
        out = ClioAgent._first_sentence("x" * 400, max_chars=50)
        assert len(out) == 50 and out.endswith("...")


class TestRouteForSelected:
    def test_valid_target(self):
        route = ClioAgent._route_for_selected("data", "why", 0.8)
        assert route.target == "data" and route.confidence == 0.8

    def test_invalid_target_falls_back_to_chat(self):
        assert ClioAgent._route_for_selected("bogus", "why", 0.5).target == "chat"


class TestAgentMaxSteps:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("CLIO_AGENT_MAX_STEPS", raising=False)
        assert ClioAgent._agent_max_steps() >= 1

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("CLIO_AGENT_MAX_STEPS", "5")
        assert ClioAgent._agent_max_steps() == 5

    def test_invalid_env_uses_default(self, monkeypatch):
        monkeypatch.setenv("CLIO_AGENT_MAX_STEPS", "abc")
        assert ClioAgent._agent_max_steps() >= 1

    def test_clamped_high(self, monkeypatch):
        monkeypatch.setenv("CLIO_AGENT_MAX_STEPS", "999")
        assert ClioAgent._agent_max_steps() == 12

    def test_clamped_low(self, monkeypatch):
        monkeypatch.setenv("CLIO_AGENT_MAX_STEPS", "0")
        assert ClioAgent._agent_max_steps() == 1


# --------------------------------------------------------------------------
# Helpers that need an agent instance
# --------------------------------------------------------------------------


class TestFallbackAnswer:
    def test_no_observations(self, agent):
        out = agent._fallback_answer_from_observations([])
        assert "could not choose" in out.lower()

    def test_failed_observation_with_error(self, agent):
        obs = [{"tool": "t", "ok": False, "result": {"error": {"message": "boom"}}}]
        assert "failed" in agent._fallback_answer_from_observations(obs).lower()

    def test_failed_observation_without_error_mapping(self, agent):
        obs = [{"tool": "t", "ok": False, "result": "raw failure"}]
        assert "could not complete" in agent._fallback_answer_from_observations(obs).lower()

    def test_ok_observation_with_value(self, agent):
        obs = [{"tool": "t", "ok": True, "result": {"value": 42}}]
        assert "42" in agent._fallback_answer_from_observations(obs)

    def test_ok_observation_without_value(self, agent):
        obs = [{"tool": "t", "ok": True, "result": {"rows": 3}}]
        out = agent._fallback_answer_from_observations(obs)
        assert "completed" in out.lower()


class TestFormatObservations:
    def test_empty(self, agent):
        assert agent._format_observations_for_prompt([]) == "No observations yet"

    def test_non_empty_is_json(self, agent):
        obs = [{"step": 1, "type": "tool", "ok": True}]
        parsed = json.loads(agent._format_observations_for_prompt(obs))
        assert parsed[0]["step"] == 1


class TestBuildCapabilitiesContext:
    def test_lists_experts_and_tools(self, agent):
        ctx = agent._build_capabilities_context()
        assert "Experts:" in ctx and "Tools:" in ctx
        # Experts registered in __init__ should appear.
        assert "data" in ctx and "analysis" in ctx


class TestSelectedExpertForTool:
    def test_unknown_tool_is_chat(self, agent):
        assert agent._selected_expert_for_tool("definitely_not_a_tool") == "chat"


# --------------------------------------------------------------------------
# _run_agent_loop branches
# --------------------------------------------------------------------------


class TestRunAgentLoop:
    def test_tool_action_then_answer(self, agent):
        agent._plan_next_action = MagicMock(
            side_effect=[
                {"action": "tool", "tool": "hdf5_analyze_file", "args": {}, "reason": "inspect"},
                {"action": "answer", "answer": "all done", "reason": "have observations"},
            ]
        )
        agent._execute_tool_action = MagicMock(return_value={"value": "ok"})

        selected, answer, expert_result, error_info, route = agent._run_agent_loop(
            question="q", session_context="", file_context="", trace=_trace()
        )

        assert answer == "all done"
        assert expert_result is None
        agent._execute_tool_action.assert_called_once()

    def test_unsupported_action_is_recorded(self, agent):
        agent._plan_next_action = MagicMock(
            side_effect=[
                {"action": "mystery"},
                {"action": "answer", "answer": "recovered"},
            ]
        )
        selected, answer, _, _, _ = agent._run_agent_loop(
            question="q", session_context="", file_context="", trace=_trace()
        )
        assert answer == "recovered"

    def test_none_action_returns_out_of_scope(self, agent):
        agent._plan_next_action = MagicMock(
            return_value={"action": "none", "answer": "out of scope", "reason": "no handler"}
        )
        selected, answer, _, _, route = agent._run_agent_loop(
            question="q", session_context="", file_context="", trace=_trace()
        )
        assert selected == "none"
        assert answer == "out of scope"

    def test_step_limit_synthesizes_answer(self, agent, monkeypatch):
        monkeypatch.setenv("CLIO_AGENT_MAX_STEPS", "1")
        agent._plan_next_action = MagicMock(
            return_value={"action": "tool", "tool": "hdf5_analyze_file", "args": {}}
        )
        agent._execute_tool_action = MagicMock(return_value={"value": "partial"})
        agent._synthesize_agent_answer = MagicMock(return_value="synthesized")

        selected, answer, _, _, route = agent._run_agent_loop(
            question="q", session_context="", file_context="", trace=_trace()
        )
        assert answer == "synthesized"
        assert "step limit" in route.reason.lower()


class TestExecuteToolAction:
    def test_unknown_tool_returns_structured_error(self, agent):
        result = agent._execute_tool_action("not_a_real_tool", {}, _trace())
        assert "error" in result
        assert result["error"]["code"] == "unknown_tool"


# --------------------------------------------------------------------------
# DSPy -> LiteLLM exclusivity (iowarp/clio-agent#54).
# These tests lock in the contract that there is NO raw-HTTP side
# channel for either the planner or the chat agent. If the DSPy layer
# fails, the failure propagates -- the agent never reaches for
# requests.post.
# --------------------------------------------------------------------------


class TestPlannerNoBypass:
    def test_planner_failure_raises_routing_error(self, agent):
        agent.action_planner = MagicMock(side_effect=RuntimeError("dspy adapter blew up"))

        with pytest.raises(RoutingError) as excinfo:
            agent._plan_next_action(
                question="hi",
                session_context="",
                file_context="",
                capabilities="",
                observations=[],
            )

        assert "dspy adapter blew up" in excinfo.value.details["original_error"]
        assert isinstance(excinfo.value.__cause__, RuntimeError)

    def test_planner_failure_does_not_call_requests(self, agent):
        # Belt-and-suspenders: if requests.post is reintroduced into
        # this path, this test catches it.
        agent.action_planner = MagicMock(side_effect=RuntimeError("dspy failed"))

        with patch("requests.post") as post_mock, pytest.raises(RoutingError):
            agent._plan_next_action(
                question="hi",
                session_context="",
                file_context="",
                capabilities="",
                observations=[],
            )

        post_mock.assert_not_called()


class TestChatAgentNoBypass:
    def test_chat_failure_surfaces_underlying_exception(self, agent):
        agent.chat_agent = MagicMock(side_effect=ValueError("bad response"))

        with pytest.raises(ValueError, match="bad response"):
            agent._run_chat_agent("hi", "")

    def test_chat_empty_answer_raises(self, agent):
        result = MagicMock()
        result.answer = ""
        agent.chat_agent = MagicMock(return_value=result)

        with pytest.raises(ValueError, match="empty answer"):
            agent._run_chat_agent("hi", "")

    # NB: the "does not call requests.post" check is on the planner
    # variant; both paths share the same rule and we don't need two
    # copies of the same belt-and-suspenders test.
