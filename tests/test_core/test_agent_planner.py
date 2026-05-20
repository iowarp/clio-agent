"""Unit tests for ClioAgent planner-loop internals.

Covers the deterministic helpers and the _run_agent_loop branches that
test_agent_dispatch.py does not exercise -- pure parsing/formatting
helpers, capability context assembly, and the tool / unsupported-action /
step-limit branches of the loop.
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
        route=RouteDecision(target="chat", source="dspy", reason="test", confidence=0.0)
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
        assert (
            ClioAgent._parse_action_json({"action": "answer", "answer": "x"})["action"] == "answer"
        )

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
        assert (
            ClioAgent._parse_action_json({"action": "ANSWER", "answer": "x"})["action"] == "answer"
        )

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

    def test_invalid_target_surfaces_routing_error(self):
        with pytest.raises(RoutingError, match="invalid route target"):
            ClioAgent._route_for_selected("bogus", "why", 0.5)

    def test_route_decision_from_dspy_rejects_invalid_target(self):
        with pytest.raises(ValueError, match="invalid route target"):
            RouteDecision.from_dspy("bogus")


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
    def test_known_tool_returns_registered_owner(self, agent):
        assert agent._selected_expert_for_tool("hdf5_analyze_file") == "data"

    def test_unknown_tool_surfaces_routing_error(self, agent):
        with pytest.raises(RoutingError, match="unknown tool") as exc_info:
            agent._selected_expert_for_tool("definitely_not_a_tool")

        assert exc_info.value.details["tool"] == "definitely_not_a_tool"
        assert "hdf5_analyze_file" in exc_info.value.details["available_tools"]


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

    def test_unknown_tool_action_surfaces_routing_error(self, agent):
        agent._plan_next_action = MagicMock(
            return_value={"action": "tool", "tool": "definitely_not_a_tool", "args": {}}
        )
        agent._execute_tool_action = MagicMock(return_value={"value": "should not run"})

        with pytest.raises(RoutingError, match="unknown tool") as exc_info:
            agent._run_agent_loop(
                question="q",
                session_context="",
                file_context="",
                trace=_trace(),
            )

        assert exc_info.value.details["tool"] == "definitely_not_a_tool"
        agent._execute_tool_action.assert_not_called()

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

    def test_repeated_planner_errors_surface_routing_error(self, agent, monkeypatch, tmp_path):
        monkeypatch.setenv("CLIO_AGENT_MAX_STEPS", "2")
        hdf5_path = tmp_path / "run.h5"
        hdf5_path.touch()
        agent._plan_next_action = MagicMock(
            return_value={"action": "expert", "expert": "analysis", "question": "analyze it"}
        )

        with pytest.raises(RoutingError, match="without producing a valid action") as exc_info:
            agent._run_agent_loop(
                question="q",
                session_context="",
                file_context=str(hdf5_path),
                trace=_trace(),
            )

        details = exc_info.value.details
        assert details["step_limit"] == 2
        assert details["recovery_actions"] == ["retry", "reconfigure_provider", "exit"]
        assert details["planner_observations"][0]["type"] == "planner_error"

    def test_none_action_returns_out_of_scope(self, agent):
        agent._plan_next_action = MagicMock(
            return_value={"action": "none", "answer": "out of scope", "reason": "no handler"}
        )
        selected, answer, _, _, route = agent._run_agent_loop(
            question="q", session_context="", file_context="", trace=_trace()
        )
        assert selected == "none"
        assert answer == "out of scope"

    def test_none_action_without_answer_surfaces_routing_error(self, agent):
        agent._plan_next_action = MagicMock(return_value={"action": "none"})

        with pytest.raises(RoutingError, match="did not provide an explanation"):
            agent._run_agent_loop(
                question="q",
                session_context="",
                file_context="",
                trace=_trace(),
            )

    def test_experts_mode_rejects_direct_none_route(self, agent):
        agent._plan_next_action = MagicMock(
            return_value={"action": "none", "answer": "out of scope"}
        )

        with pytest.raises(RoutingError, match="routing_mode='experts'"):
            agent._run_agent_loop(
                question="q",
                session_context="",
                file_context="",
                trace=_trace(),
                routing_mode="experts",
            )

    def test_experts_mode_rejects_direct_answer_without_observations(self, agent):
        agent._plan_next_action = MagicMock(return_value={"action": "answer", "answer": "chat"})

        with pytest.raises(RoutingError, match="direct planner answer"):
            agent._run_agent_loop(
                question="q",
                session_context="",
                file_context="",
                trace=_trace(),
                routing_mode="experts",
            )

    def test_general_question_expert_action_is_kept_in_chat(self, agent):
        agent._plan_next_action = MagicMock(
            return_value={
                "action": "expert",
                "expert": "data",
                "question": "What can you do with HDF5 or Parquet data?",
            }
        )
        agent._run_chat_agent = MagicMock(return_value="capability answer")
        agent.data_expert = MagicMock()

        selected, answer, _, error_info, route = agent._run_agent_loop(
            question="What can you do with HDF5 or Parquet data?",
            session_context="",
            file_context="",
            trace=_trace(),
        )

        assert selected == "chat"
        assert answer == "capability answer"
        assert error_info is None
        assert "no concrete file" in route.reason
        agent.data_expert.assert_not_called()

    def test_none_action_with_stale_in_scope_answer_uses_chat(self, agent):
        stale = "HDF5 and Parquet are scientific data formats."
        agent._plan_next_action = MagicMock(
            return_value={"action": "none", "answer": stale, "reason": "no handler"}
        )
        agent._run_chat_agent = MagicMock(return_value="summary answer")

        selected, answer, _, error_info, route = agent._run_agent_loop(
            question="Summarize your previous answers in one sentence.",
            session_context=f"user: What can you do?\nassistant: {stale}",
            file_context="",
            trace=_trace(),
        )

        assert selected == "chat"
        assert answer == "summary answer"
        assert error_info is None
        assert "replaced" in route.reason

    def test_planner_answer_leaking_file_context_uses_chat(self, agent):
        agent._plan_next_action = MagicMock(
            return_value={
                "action": "answer",
                "answer": "The file_context is empty, so I need more context.",
            }
        )
        agent._run_chat_agent = MagicMock(return_value="clean answer")

        selected, answer, _, error_info, _ = agent._run_agent_loop(
            question="Explain one safe next step for analyzing a local data file.",
            session_context="",
            file_context="",
            trace=_trace(),
        )

        assert selected == "chat"
        assert answer == "clean answer"
        assert error_info is None

    def test_step_limit_synthesizes_partial_answer_with_error_info(self, agent, monkeypatch):
        monkeypatch.setenv("CLIO_AGENT_MAX_STEPS", "1")
        agent._plan_next_action = MagicMock(
            return_value={"action": "tool", "tool": "hdf5_analyze_file", "args": {}}
        )
        agent._execute_tool_action = MagicMock(return_value={"value": "partial"})
        agent._synthesize_agent_answer = MagicMock(return_value="synthesized")

        selected, answer, _, error_info, route = agent._run_agent_loop(
            question="q", session_context="", file_context="", trace=_trace()
        )
        assert answer == "synthesized"
        assert "step limit" in route.reason.lower()
        assert error_info is not None
        assert error_info["error"] == "routing_error"
        assert error_info["details"]["partial"] is True
        assert error_info["details"]["step_limit"] == 1
        assert error_info["details"]["recovery_actions"] == [
            "retry",
            "reconfigure_provider",
            "exit",
        ]

    def test_answer_synthesis_exception_surfaces_error_info(self, agent, monkeypatch):
        monkeypatch.setenv("CLIO_AGENT_MAX_STEPS", "1")
        agent._plan_next_action = MagicMock(
            return_value={"action": "tool", "tool": "hdf5_analyze_file", "args": {}}
        )
        agent._execute_tool_action = MagicMock(return_value={"value": "partial"})
        agent.answer_synthesizer = MagicMock(side_effect=RuntimeError("provider down"))

        result = agent.forward("summarize the file", session_id="synthesis-error")

        assert result.answer == ""
        assert result.error_info is not None
        assert result.error_info["error"] == "provider_error"
        assert "final answer" in result.error_info["message"]
        details = result.error_info["details"]
        assert details["stage"] == "answer_synthesis"
        assert details["original_error"] == "provider down"
        assert details["recovery_actions"] == ["retry", "reconfigure_provider", "exit"]

    def test_empty_answer_synthesis_surfaces_error_info(self, agent, monkeypatch):
        monkeypatch.setenv("CLIO_AGENT_MAX_STEPS", "1")
        agent._plan_next_action = MagicMock(
            return_value={"action": "tool", "tool": "hdf5_analyze_file", "args": {}}
        )
        agent._execute_tool_action = MagicMock(return_value={"value": "partial"})
        agent.answer_synthesizer = MagicMock(return_value=MagicMock(answer=""))

        result = agent.forward("summarize the file", session_id="empty-synthesis")

        assert result.answer == ""
        assert result.error_info is not None
        assert result.error_info["error"] == "provider_error"
        details = result.error_info["details"]
        assert details["stage"] == "answer_synthesis"
        assert details["original_error"] == "answer synthesizer returned an empty answer"


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
    def test_planner_accepts_raw_json_from_chat_adapter_error(self, agent):
        agent.action_planner = MagicMock(
            side_effect=ValueError(
                "Adapter ChatAdapter failed to parse the LM response.\n\n"
                'LM Response: {"action":"answer","answer":"ok","reason":"raw json"}]\n\n'
                "Expected to find output fields in the LM response: [action_json]\n\n"
                "Actual output fields parsed from the LM response: []"
            )
        )

        action = agent._plan_next_action(
            question="hi",
            session_context="",
            file_context="",
            capabilities="",
            observations=[],
        )

        assert action == {"action": "answer", "answer": "ok", "reason": "raw json"}

    def test_invalid_adapter_error_still_raises_routing_error(self, agent):
        agent.action_planner = MagicMock(
            side_effect=ValueError(
                "Adapter ChatAdapter failed to parse the LM response.\n\n"
                "LM Response: not a json action\n\n"
                "Expected to find output fields in the LM response: [action_json]\n\n"
                "Actual output fields parsed from the LM response: []"
            )
        )

        with pytest.raises(RoutingError) as excinfo:
            agent._plan_next_action(
                question="hi",
                session_context="",
                file_context="",
                capabilities="",
                observations=[],
            )

        assert "not a json action" in excinfo.value.details["original_error"]

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
    def test_chat_adapter_parse_failure_surfaces(self, agent):
        agent.chat_agent = MagicMock(
            side_effect=ValueError(
                "Adapter ChatAdapter failed to parse the LM response.\n\n"
                "LM Response: [[ ## answer ##\nUseful answer text.\n"
                "[[ ## completed ## ]]\n\n"
                "Expected to find output fields in the LM response: [answer]\n\n"
                "Actual output fields parsed from the LM response: []"
            )
        )

        with pytest.raises(ValueError, match="ChatAdapter failed"):
            agent._run_chat_agent("hi", "")

    def test_chat_summary_replaces_repeated_previous_answer(self, agent):
        repeated = "If a provider fails, retry or reconfigure the provider."
        agent.chat_agent = MagicMock(return_value=MagicMock(answer=repeated))
        session_context = (
            "assistant: CLIO helps analyze scientific data files.\n"
            "assistant: HDF5 and Parquet capabilities include schemas and statistics.\n"
            f"assistant: {repeated}"
        )

        answer = agent._run_chat_agent(
            "Summarize your previous answers in one sentence.",
            session_context,
        )

        assert answer.startswith("Previous answers covered:")
        assert "scientific data files" in answer
        assert "schemas and statistics" in answer

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
