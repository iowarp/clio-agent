"""Unit tests for ClioAgent planner-loop internals.

Covers the deterministic helpers and the _run_agent_loop branches that
test_agent_dispatch.py does not exercise -- pure parsing/formatting
helpers, capability context assembly, and the tool / unsupported-action /
step-limit branches of the loop.
All without a live LM.
"""

import json
import re
from unittest.mock import ANY, MagicMock, patch

import pytest

from clio_agent.agent import ClioAgent, _mass_spec_qc_sentence, cancellation_checker
from clio_agent.errors import CancellationError, ProviderError, RoutingError
from clio_agent.harness import RouteDecision, RunTrace, ToolObservation
from clio_agent.registry.registry import AgentCapability
from clio_agent.tools.execution import set_global_tool_observer


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


def test_mass_spec_qc_sentence_uses_readable_terms():
    sentence = _mass_spec_qc_sentence(
        {
            "ms_levels": {"1": 2, "2": 2},
            "total_ion_current_total": 25140.0,
            "total_ion_current_max": 9500.0,
        }
    )

    assert "MS level distribution" in sentence
    assert "Total ion current evidence" in sentence
    assert "25140.0" in sentence


def test_recovered_tool_failure_does_not_surface_blocking_error() -> None:
    trace = _trace()
    trace.record_tool(
        tool="ndp_stage_resource",
        params={"resource_index": 0},
        result={"error": {"message": "download timed out", "code": "resource_download_failed"}},
        duration_ms=10.0,
        ok=False,
    )
    trace.record_tool(
        tool="ndp_stage_resource",
        params={"resource_index": 1},
        result={"staged": True, "path": "/tmp/recovered.tar"},
        duration_ms=10.0,
        ok=True,
    )

    assert ClioAgent._tool_error_info_from_trace("data", trace) is None


def test_unrecovered_tool_failure_still_surfaces_partial_error() -> None:
    trace = _trace()
    trace.record_tool(
        tool="ndp_search_datasets",
        params={},
        result={"datasets": []},
        duration_ms=10.0,
        ok=True,
    )
    trace.record_tool(
        tool="ndp_stage_resource",
        params={"resource_index": 0},
        result={"error": {"message": "download timed out", "code": "resource_download_failed"}},
        duration_ms=10.0,
        ok=False,
    )

    error_info = ClioAgent._tool_error_info_from_trace("data", trace)

    assert error_info is not None
    assert error_info["details"]["partial"] is True
    assert error_info["details"]["tool"] == "ndp_stage_resource"


def test_handled_tool_failure_does_not_surface_partial_error() -> None:
    trace = _trace()
    trace.record_tool(
        tool="ndp_search_datasets",
        params={},
        result={"datasets": []},
        duration_ms=10.0,
        ok=True,
    )
    trace.record_tool(
        tool="ndp_stage_resource",
        params={"resource_index": 0},
        result={
            "error": {
                "message": "download timed out",
                "code": "resource_download_failed",
                "handled": True,
                "handled_reason": "summarized in expert answer",
            }
        },
        duration_ms=10.0,
        ok=False,
    )

    assert ClioAgent._tool_error_info_from_trace("data", trace) is None


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

    def test_embedded_object_is_recovered_from_local_model_preamble(self):
        out = ClioAgent._parse_action_json(
            'Here is the action:\n{"action": "expert", "expert": "analysis"}'
        )
        assert out == {"action": "expert", "expert": "analysis"}

    def test_embedded_object_with_nested_args_is_recovered(self):
        out = ClioAgent._parse_action_json(
            'Action:\n{"action":"tool","tool":"parquet_analyze_schema",'
            '"args":{"filepath":"D:\\\\data\\\\measurements.parquet"},"reason":"inspect"}'
            "\nDone."
        )
        assert out["action"] == "tool"
        assert out["args"]["filepath"].endswith("measurements.parquet")

    def test_trailing_text_rejected(self):
        with pytest.raises(ValueError):
            ClioAgent._parse_action_json('{"action": "answer", "answer": "x"} trailing')

    def test_dspy_adapter_trailing_bracket_artifact_allowed(self):
        out = ClioAgent._parse_action_json('{"action": "answer", "answer": "x"}]')
        assert out == {"action": "answer", "answer": "x"}

    def test_dspy_completed_marker_after_json_is_allowed(self):
        out = ClioAgent._parse_action_json(
            '{"action":"answer","answer":"ALCF_CLIO_OK","reason":"exact"}[[ ## completed ## ]]'
        )
        assert out == {
            "action": "answer",
            "answer": "ALCF_CLIO_OK",
            "reason": "exact",
        }

    def test_truncated_object_with_complete_tool_args_is_repaired(self):
        raw = (
            '{"action":"tool","tool":"parquet_analyze_schema",'
            '"args":{"filepath":"D:\\\\data\\\\measurements.parquet"},'
            '"reason":"Inspect schema"'
        )

        out = ClioAgent._parse_action_json(raw)

        assert out == {
            "action": "tool",
            "tool": "parquet_analyze_schema",
            "args": {"filepath": "D:\\data\\measurements.parquet"},
            "reason": "Inspect schema",
        }

    def test_truncated_reason_string_is_repaired_when_action_is_complete(self):
        raw = (
            '{"action":"expert","expert":"analysis",'
            '"question":"Inspect D:\\\\data\\\\observations.csv for temperature columns.",'
            '"reason":"User wants to inspect a'
        )

        out = ClioAgent._parse_action_json(raw)

        assert out == {
            "action": "expert",
            "expert": "analysis",
            "question": "Inspect D:\\data\\observations.csv for temperature columns.",
            "reason": "User wants to inspect a",
        }

    def test_truncated_expert_question_is_repaired(self):
        raw = (
            '{"action":"expert","expert":"analysis",'
            '"question":"Please inspect D:\\\\data\\\\fusion_run.h5'
        )

        out = ClioAgent._parse_action_json(raw)

        assert out == {
            "action": "expert",
            "expert": "analysis",
            "question": "Please inspect D:\\data\\fusion_run.h5",
        }

    def test_truncated_non_expert_question_is_rejected(self):
        raw = '{"action":"tool","tool":"x","question":"not part of tool schema'

        with pytest.raises(ValueError):
            ClioAgent._parse_action_json(raw)

    def test_truncated_required_string_value_is_rejected(self):
        raw = (
            '{"action":"tool","tool":"parquet_analyze_schema",'
            '"args":{"filepath":"D:\\\\data\\\\measure'
        )

        with pytest.raises(ValueError):
            ClioAgent._parse_action_json(raw)

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


class TestParseAnswerFromAdapterError:
    def test_plain_prose_answer_is_recovered_from_dspy_parse_error(self):
        error = ValueError(
            "Adapter failure\n"
            "LM Response:\n"
            "Scientific workflow agents need evidence logs because tool outputs and "
            "routing choices must be auditable.\n"
            "Expected to find output fields in response: [answer]"
        )

        answer = ClioAgent._parse_answer_from_adapter_error(error)

        assert answer is not None
        assert answer.startswith("Scientific workflow agents")

    def test_non_answer_parse_error_is_not_recovered(self):
        error = ValueError(
            "Adapter failure\n"
            'LM Response:\n{"action":"answer","answer":"x"}\n'
            "Expected to find output fields in response: [action_json]"
        )

        assert ClioAgent._parse_answer_from_adapter_error(error) is None

    def test_marker_fragment_is_not_recovered_as_answer(self):
        error = ValueError(
            "Adapter failure\n"
            "LM Response:\n[[\n"
            "Expected to find output fields in response: [answer]"
        )

        assert ClioAgent._parse_answer_from_adapter_error(error) is None


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
    def test_valid_target(self, agent):
        route = agent._route_for_selected("data", "why", 0.8)
        assert route.target == "data" and route.confidence == 0.8

    def test_utility_target_is_valid(self, agent):
        route = agent._route_for_selected("utility", "shell tool", 0.8)
        assert route.target == "utility"

    def test_dynamic_registered_target_is_valid(self, agent):
        agent.registry.register_agent(
            "ndp",
            agent,
            AgentCapability(
                keywords=["ndp"],
                description="NDP specialist",
                tools=[],
                specialization="dataset",
            ),
        )

        route = agent._route_for_selected("ndp", "registered dynamically", 0.8)

        assert route.target == "ndp"

    def test_invalid_target_surfaces_routing_error(self, agent):
        with pytest.raises(RoutingError, match="invalid route target"):
            agent._route_for_selected("bogus", "why", 0.5)

    def test_route_decision_from_dspy_rejects_invalid_target(self):
        with pytest.raises(ValueError, match="invalid route target"):
            RouteDecision.from_dspy("bogus", available_targets=["chat", "data", "none"])

    def test_route_decision_from_dspy_accepts_dynamic_target(self):
        route = RouteDecision.from_dspy("ndp", available_targets=["chat", "ndp", "none"])
        assert route.target == "ndp"


class TestAgentMaxSteps:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("CLIO_AGENT_MAX_STEPS", raising=False)
        assert ClioAgent._agent_max_steps() == 8

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
        assert "Experts:" in ctx and "Scoped tools:" in ctx
        # Experts registered in __init__ should appear.
        assert "data" in ctx and "analysis" in ctx
        assert "Tool scope rules:" in ctx
        assert "do not treat scientific data, analysis, visualization, and utility tools" in ctx

    def test_hides_internal_fs_tools_from_planner_context(self, agent):
        ctx = agent._build_capabilities_context()
        assert "fs_read_file(" not in ctx
        assert "fs_apply_edit_write(" not in ctx
        assert "fs_propose_edit(" in ctx

    def test_tools_are_listed_under_owning_experts(self, agent):
        ctx = agent._build_capabilities_context()
        scoped = ctx.split("Scoped tools:", 1)[1].split("Chat utility tools:", 1)[0]

        def block(agent_id: str) -> str:
            marker = f"- {agent_id}:"
            assert marker in scoped
            after = scoped.split(marker, 1)[1]
            next_header = re.search(r"\n- [a-z_]+:", after)
            return after[: next_header.start()] if next_header else after

        analysis_block = block("analysis")
        data_block = block("data")
        utility_block = block("utility")
        visualization_block = block("visualization")

        assert "hdf5_analyze_file(" in data_block
        assert "delegated child ndp_catalog:" in data_block
        assert "ndp_search_datasets(" in data_block
        assert "parquet_analyze_schema(" not in data_block
        assert "parquet_analyze_schema(" in analysis_block
        assert "hdf5_analyze_file(" not in analysis_block
        assert "ndp_search_datasets(" not in analysis_block
        assert "delegated child sac_format:" in analysis_block
        assert "sac_compute_trace_statistics(" in analysis_block
        assert "sac_plot_traces(" in analysis_block
        assert "plot_summary(" in visualization_block
        assert "shell_bash(" in utility_block
        assert "fs_propose_edit(" in utility_block

    def test_nested_experts_are_visible_as_parent_delegates(self, agent):
        ctx = agent._build_capabilities_context()
        expert_lines = ctx.split("Scoped tools:", 1)[0].splitlines()
        ndp_line = next(line for line in expert_lines if "delegated child ndp_catalog:" in line)
        sac_line = next(line for line in expert_lines if "delegated child sac_format:" in line)

        assert not any(line.startswith("- ndp_catalog:") for line in expert_lines)
        assert not any(line.startswith("- sac_format:") for line in expert_lines)
        assert "National Data Platform" in ndp_line
        assert "SAC waveform" in sac_line

    def test_chat_utility_tools_are_explicitly_scoped(self, agent):
        ctx = agent._build_capabilities_context()
        chat_block = ctx.split("Chat utility tools:", 1)[1].split("Tool scope rules:", 1)[0]
        assert "shell_bash(" in chat_block
        assert "hdf5_analyze_file(" not in chat_block
        assert "parquet_analyze_schema(" not in chat_block

    def test_multi_file_strategy_is_visible_to_planner(self, agent):
        ctx = agent._build_capabilities_context()
        assert "next unresolved phase" in ctx
        assert "Do not skip data acquisition/discovery before analysis" in ctx

    def test_coordinator_metadata_is_visible_to_planner(self, agent):
        ctx = agent._build_capabilities_context()
        analysis_line = next(line for line in ctx.splitlines() if line.startswith("- analysis:"))
        assert "direct files: .parquet, .csv" in analysis_line
        assert "coordinates multi-file bundles:" in analysis_line
        assert ".h5" in analysis_line
        assert ".bp5" in analysis_line
        assert "coordination intents:" not in analysis_line

    def test_expert_observation_exposes_local_paths_to_planner(self, agent):
        result = MagicMock()
        result.metadata = {"expert": "data"}
        result.tools = (
            ToolObservation(
                tool="sac_fetch_earthscope_waveform",
                params={},
                result={"path": "/tmp/example_waveform.sac", "staged": True},
                duration_ms=1.0,
                ok=True,
            ),
        )
        result.tool_provenance = ()

        observation = agent._expert_loop_observation(
            step=1,
            expert="data",
            answer="staged waveform",
            expert_result=result,
            error_info=None,
            reason="test",
        )

        assert observation["local_paths"] == ["/tmp/example_waveform.sac"]

    def test_downstream_expert_receives_current_turn_observations(self, agent):
        data_result = MagicMock()
        data_result.analysis = "staged waveform"
        data_result.recommendations = "analyze next"
        data_result.metadata = {"expert": "data"}
        data_result.tools = (
            ToolObservation(
                tool="sac_fetch_earthscope_waveform",
                params={},
                result={"path": "/tmp/example_waveform.sac", "staged": True},
                duration_ms=1.0,
                ok=True,
            ),
        )
        data_result.tool_provenance = ()
        analysis_result = MagicMock()
        analysis_result.analysis = "computed stats"
        analysis_result.recommendations = "plot next"
        analysis_result.metadata = {"expert": "analysis"}
        analysis_result.tools = ()
        analysis_result.tool_provenance = ()

        agent._plan_next_action = MagicMock(
            side_effect=[
                {"action": "expert", "expert": "data", "reason": "stage data"},
                {"action": "expert", "expert": "analysis", "reason": "analyze staged data"},
                {"action": "answer", "answer": "", "reason": "done"},
            ]
        )
        agent._dispatch_expert_action = MagicMock(
            side_effect=[
                ("data", "staged waveform", data_result, None),
                ("analysis", "computed stats", analysis_result, None),
            ]
        )

        selected, answer, result, error_info, route = agent._run_agent_loop(
            question="stage then analyze a waveform",
            session_context="",
            file_context="",
            trace=_trace(),
        )

        assert selected == "analysis"
        assert "computed stats" in answer
        assert result is analysis_result
        assert error_info is None
        second_call = agent._dispatch_expert_action.call_args_list[1]
        assert "[Current turn observations]" in second_call.kwargs["session_context"]
        assert "/tmp/example_waveform.sac" in second_call.kwargs["session_context"]

    def test_analysis_coordinator_accepts_mixed_file_bundle(self, agent, tmp_path):
        hdf5_path = tmp_path / "run.h5"
        parquet_path = tmp_path / "measurements.parquet"
        csv_path = tmp_path / "events.csv"
        for path in (hdf5_path, parquet_path, csv_path):
            path.touch()

        error = agent._expert_file_compatibility_error(
            "analysis",
            str(hdf5_path),
            question=(
                f"Compare these related files together: {hdf5_path}, "
                f"{parquet_path}, and {csv_path}."
            ),
        )

        assert error is None

    def test_analysis_still_rejects_single_hdf5_direct_route(self, agent, tmp_path):
        hdf5_path = tmp_path / "run.h5"
        hdf5_path.touch()

        error = agent._expert_file_compatibility_error(
            "analysis",
            str(hdf5_path),
            question=f"Inspect {hdf5_path}.",
        )

        assert error is not None
        assert error["expert"] == "analysis"


class TestSessionFileResolution:
    def test_quoted_bp5_path_with_spaces_is_extracted(self, agent, tmp_path):
        bp_path = tmp_path / "dataset 1" / "noise=0.01" / "data.bp5"
        bp_path.mkdir(parents=True)

        paths = agent._resolve_session_file_reference(
            f'Inspect this ADIOS output: "{bp_path}"',
            "session-1",
        )

        assert paths == bp_path

    def test_unquoted_windows_bp5_path_with_spaces_is_extracted(self, agent, tmp_path):
        bp_path = tmp_path / "dataset 1" / "noise=0.01" / "data.bp5"
        bp_path.mkdir(parents=True)

        resolved = agent._resolve_session_file_reference(f"Inspect ADIOS output: {bp_path}", "s")

        assert resolved == bp_path

    def test_bp5_suffix_is_not_truncated_to_bp(self, agent, tmp_path):
        bp_path = tmp_path / "data.bp5"
        bp_path.mkdir()

        resolved = agent._resolve_session_file_reference(f"Inspect {bp_path}", "session-1")

        assert resolved == bp_path

    def test_mixed_scientific_paths_keep_textual_order(self, agent, tmp_path):
        hdf5_path = tmp_path / "fusion_run.h5"
        parquet_path = tmp_path / "facility.parquet"
        csv_path = tmp_path / "events.csv"
        bp_path = tmp_path / "dataset 1" / "data.bp5"
        hdf5_path.touch()
        parquet_path.touch()
        csv_path.touch()
        bp_path.mkdir(parents=True)

        resolved = agent._resolve_session_file_reference(
            f'I have {hdf5_path}, {parquet_path}, {csv_path}, and "{bp_path}".',
            "session-1",
        )

        assert resolved == hdf5_path

    def test_natural_parquet_followup_prefers_last_parquet_over_later_csv(self, agent, tmp_path):
        parquet_path = tmp_path / "facility.parquet"
        csv_path = tmp_path / "events.csv"
        parquet_path.touch()
        csv_path.write_text("event_id,status\n1,ok\n", encoding="utf-8")
        conversation = MagicMock()
        conversation.messages = [
            MagicMock(content=f"Profile {parquet_path}"),
            MagicMock(content=f"Inspect {csv_path}"),
        ]
        agent.arc.get_conversation = MagicMock(return_value=conversation)

        resolved = agent._resolve_session_file_reference(
            "Create a dashboard from the Parquet file we just profiled.",
            "session-1",
        )

        assert resolved == parquet_path

    def test_multi_format_followup_does_not_collapse_to_last_file(self, agent, tmp_path):
        hdf5_path = tmp_path / "fusion_run.h5"
        parquet_path = tmp_path / "facility.parquet"
        csv_path = tmp_path / "events.csv"
        bp_path = tmp_path / "gray scott" / "data.bp5"
        hdf5_path.touch()
        parquet_path.touch()
        csv_path.write_text("event_id,status\n1,ok\n", encoding="utf-8")
        bp_path.mkdir(parents=True)
        conversation = MagicMock()
        conversation.messages = [
            MagicMock(content=f"HDF5 evidence: {hdf5_path}"),
            MagicMock(content=f"Parquet evidence: {parquet_path}"),
            MagicMock(content=f"CSV evidence: {csv_path}"),
            MagicMock(content=f"BP5 evidence: {bp_path}"),
        ]
        agent.arc.get_conversation = MagicMock(return_value=conversation)

        question = (
            "Cite the strongest retained evidence from the HDF5, Parquet, CSV, and BP5 stages."
        )

        resolved = agent._resolve_session_file_reference(question, "session-1")

        assert resolved is None
        assert agent._question_with_file_context(question, str(bp_path)) == question

    def test_followup_ignores_degraded_missing_basename_when_full_path_exists(
        self,
        agent,
        tmp_path,
    ):
        parquet_path = tmp_path / "benchmark-data" / "facility_measurements.parquet"
        parquet_path.parent.mkdir()
        parquet_path.touch()
        degraded = tmp_path / "facility_measurements.parquet"
        conversation = MagicMock()
        conversation.messages = [
            MagicMock(content=f"Profile {parquet_path}"),
            MagicMock(content=f"Assistant mentioned {degraded} without creating it."),
        ]
        agent.arc.get_conversation = MagicMock(return_value=conversation)

        resolved = agent._resolve_session_file_reference(
            "Create a dashboard from the Parquet file we just profiled.",
            "session-1",
        )

        assert resolved == parquet_path


class TestSelectedExpertForTool:
    def test_known_tool_returns_registered_owner(self, agent):
        assert agent._selected_expert_for_tool("hdf5_analyze_file") == "data"
        assert agent._selected_expert_for_tool("genomics_inspect_fasta") == "genomics"
        assert agent._selected_expert_for_tool("materials_inspect_cif") == "materials"
        assert agent._selected_expert_for_tool("geospatial_inspect_geojson") == "geospatial"
        assert agent._selected_expert_for_tool("imaging_inspect_png") == "imaging"
        assert agent._selected_expert_for_tool("mass_spec_inspect_mzml") == "mass_spec"

    def test_unknown_tool_surfaces_routing_error(self, agent):
        with pytest.raises(RoutingError, match="unknown tool") as exc_info:
            agent._selected_expert_for_tool("definitely_not_a_tool")

        assert exc_info.value.details["tool"] == "definitely_not_a_tool"
        assert "hdf5_analyze_file" in exc_info.value.details["available_tools"]


# --------------------------------------------------------------------------
# _run_agent_loop branches
# --------------------------------------------------------------------------


class TestRunAgentLoop:
    def test_expert_dispatch_retries_transient_provider_error(self, agent, monkeypatch):
        monkeypatch.setenv("CLIO_TRANSIENT_PROVIDER_RETRY_DELAYS", "0")
        agent.analysis_expert = MagicMock(
            side_effect=[
                RuntimeError("litellm.RateLimitError: Tokens/minute limit exceeded"),
                MagicMock(analysis="retained evidence summary", recommendations="retry passed"),
            ]
        )

        selected, answer, expert_result, error_info = agent._dispatch_expert_action(
            expert_id="analysis",
            question="Use retained HDF5, Parquet, CSV, and BP5 evidence.",
            file_context="",
            trace=_trace(),
        )

        assert selected == "analysis"
        assert "retained evidence summary" in answer
        assert expert_result is not None
        assert error_info is None
        assert agent.analysis_expert.call_count == 2

    def test_expert_dispatch_receives_retained_session_context(self, agent):
        agent.analysis_expert = MagicMock(
            return_value=MagicMock(analysis="summary", recommendations="review")
        )
        session_context = (
            "[Session Context]\n"
            "assistant: [compact summary] HDF5 partial compression; Parquet stats; "
            "CSV conversion; BP5 ADIOS2 missing.\n\n"
            "[Available Tools]\n"
            "shell_bash: run a utility command\n"
        )

        selected, answer, _, error_info = agent._dispatch_expert_action(
            expert_id="analysis",
            question="Use retained HDF5, Parquet, CSV, and BP5 evidence.",
            file_context="",
            session_context=session_context,
            trace=_trace(),
        )

        assert selected == "analysis"
        assert "summary" in answer
        assert error_info is None
        expert_context = agent.analysis_expert.call_args.kwargs["file_context"]
        assert "[Retained session context]" in expert_context
        assert "HDF5 partial compression" in expert_context
        assert "[Available Tools]" not in expert_context
        assert "shell_bash" not in expert_context

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

    def test_ndp_tool_action_is_promoted_to_parent_data_expert(self, agent):
        agent._plan_next_action = MagicMock(
            side_effect=[
                {
                    "action": "tool",
                    "tool": "ndp_search_datasets",
                    "args": {"search_terms": ["seismic"]},
                    "reason": "search catalog",
                },
                {"action": "answer", "answer": "", "reason": "expert observed"},
            ]
        )
        expert_result = object()
        agent._dispatch_expert_action = MagicMock(
            return_value=("ndp_catalog", "NDP results\n\nstage data", expert_result, None)
        )
        agent._execute_tool_action = MagicMock(return_value={"value": "should not run"})

        selected, answer, result, error_info, route = agent._run_agent_loop(
            question="Find seismic datasets in NDP.",
            session_context="",
            file_context="",
            trace=_trace(),
        )

        assert selected == "data"
        assert "NDP results" in answer
        assert result is expert_result
        assert error_info is None
        assert route.target == "data"
        agent._dispatch_expert_action.assert_called_once()
        assert agent._dispatch_expert_action.call_args.kwargs["expert_id"] == "data"
        agent._execute_tool_action.assert_not_called()

    def test_parquet_schema_tool_executes_without_keyword_promotion(self, agent):
        agent._plan_next_action = MagicMock(
            side_effect=[
                {
                    "action": "tool",
                    "tool": "parquet_analyze_schema",
                    "args": {"filepath": "measurements.parquet"},
                    "reason": "inspect parquet",
                },
                {"action": "answer", "answer": "schema observed", "reason": "done"},
            ]
        )
        agent._dispatch_expert_action = MagicMock()
        agent._execute_tool_action = MagicMock(return_value={"columns": 3, "ok": True})

        selected, answer, result, error_info, route = agent._run_agent_loop(
            question=(
                "Based on the Parquet file we just profiled, compute column statistics "
                "for an anomaly triage view."
            ),
            session_context="",
            file_context=r"Current file: D:\data\measurements.parquet",
            trace=_trace(),
        )

        assert selected == "analysis"
        assert answer == "schema observed"
        assert result is None
        assert error_info is None
        assert route.target == "analysis"
        agent._dispatch_expert_action.assert_not_called()
        agent._execute_tool_action.assert_called_once()

    def test_direct_answer_is_not_promoted_by_retained_context_keywords(self, agent):
        agent._plan_next_action = MagicMock(
            return_value={
                "action": "answer",
                "answer": "This looks ready.",
                "reason": "direct synthesis",
            }
        )
        agent._dispatch_expert_action = MagicMock()

        selected, answer, result, error_info, route = agent._run_agent_loop(
            question=(
                "After compaction, cite the strongest evidence from all stages for "
                "collaborator review."
            ),
            session_context=(
                "[Retained session context]\n"
                "[compact summary] HDF5 fusion_run.h5, Parquet facility.parquet, "
                "CSV sensor_events.csv, and BP5 gray_scott.bp5 evidence survived.\n"
                "[exact retained evidence index]\n"
                "Identifiers:\n- plasma/electron_temperature\n- anomaly_score\n"
            ),
            file_context="",
            trace=_trace(),
        )

        assert selected == "chat"
        assert answer == "This looks ready."
        assert result is None
        assert error_info is None
        assert route.target == "chat"
        agent._dispatch_expert_action.assert_not_called()

    def test_analysis_expert_action_is_not_promoted_to_visualization(self, agent, tmp_path):
        csv_path = tmp_path / "sensor_events.csv"
        csv_path.write_text("event_id,status\n1,ok\n", encoding="utf-8")
        agent._plan_next_action = MagicMock(
            side_effect=[
                {
                    "action": "expert",
                    "expert": "analysis",
                    "question": "",
                    "reason": "needs csv data",
                },
                {"action": "answer", "answer": "", "reason": "expert observed"},
            ]
        )
        expert_result = object()
        agent._dispatch_expert_action = MagicMock(
            return_value=("analysis", "analysis observed", expert_result, None)
        )

        selected, answer, result, error_info, route = agent._run_agent_loop(
            question="Create a PNG bar chart of the status distribution.",
            session_context="",
            file_context=f"Current file: {csv_path}",
            trace=_trace(),
        )

        assert selected == "analysis"
        assert "analysis observed" in answer
        assert result is expert_result
        assert error_info is None
        assert route.target == "analysis"
        agent._dispatch_expert_action.assert_called_once()
        assert agent._dispatch_expert_action.call_args.kwargs["expert_id"] == "analysis"

    def test_analysis_tool_action_is_not_promoted_to_visualization(self, agent, tmp_path):
        parquet_path = tmp_path / "measurements.parquet"
        parquet_path.touch()
        agent._plan_next_action = MagicMock(
            side_effect=[
                {
                    "action": "tool",
                    "tool": "parquet_compute_statistics",
                    "args": {"filepath": str(parquet_path), "column": "quality_flag"},
                    "reason": "inspect before chart",
                },
                {"action": "answer", "answer": "statistics observed", "reason": "done"},
            ]
        )
        agent._dispatch_expert_action = MagicMock()
        agent._execute_tool_action = MagicMock(return_value={"mean": 1.0, "ok": True})

        selected, answer, result, error_info, route = agent._run_agent_loop(
            question="Create a compact dashboard PNG for the Parquet file we just reviewed.",
            session_context="",
            file_context=f"Current file: {parquet_path}",
            trace=_trace(),
        )

        assert selected == "analysis"
        assert answer == "statistics observed"
        assert result is None
        assert error_info is None
        assert route.target == "analysis"
        agent._dispatch_expert_action.assert_not_called()
        agent._execute_tool_action.assert_called_once()

    def test_shell_tool_is_rejected_for_scientific_file_inspection(self, agent, tmp_path):
        csv_path = tmp_path / "events.csv"
        csv_path.write_text("event_id,status\n1,ok\n", encoding="utf-8")
        agent._plan_next_action = MagicMock(
            side_effect=[
                {
                    "action": "tool",
                    "tool": "shell_bash",
                    "args": {"command": f'head -n 1 "{csv_path}"'},
                    "reason": "peek at csv",
                },
                {
                    "action": "tool",
                    "tool": "csv_read_table",
                    "args": {"filepath": str(csv_path)},
                    "reason": "inspect csv natively",
                },
                {"action": "answer", "answer": "CSV inspected.", "reason": "done"},
            ]
        )
        agent._execute_tool_action = MagicMock(return_value={"rows": 1, "columns": 2})

        selected, answer, expert_result, error_info, route = agent._run_agent_loop(
            question=f"Check the CSV schema for {csv_path}",
            session_context="",
            file_context="",
            trace=_trace(),
        )

        assert selected == "analysis"
        assert answer == "CSV inspected."
        assert expert_result is None
        assert error_info is None
        assert route.target == "analysis"
        agent._execute_tool_action.assert_called_once_with(
            "csv_read_table",
            {"filepath": str(csv_path)},
            ANY,
            question=f"Check the CSV schema for {csv_path}",
            file_context="",
            session_context="",
        )

    def test_shell_tool_still_runs_for_utility_diagnostics(self, agent):
        agent._plan_next_action = MagicMock(
            side_effect=[
                {
                    "action": "tool",
                    "tool": "shell_bash",
                    "args": {"command": "date"},
                    "reason": "get time",
                },
                {"action": "answer", "answer": "It is today.", "reason": "done"},
            ]
        )
        agent._execute_tool_action = MagicMock(return_value={"stdout": "today"})

        selected, answer, *_ = agent._run_agent_loop(
            question="What is the current time?",
            session_context="",
            file_context="",
            trace=_trace(),
        )

        assert selected == "utility"
        assert answer == "It is today."
        agent._execute_tool_action.assert_called_once()

    def test_direct_tool_action_records_owner_handoff(self, agent):
        trace = _trace()
        agent._known_tool_names = MagicMock(return_value={"parquet_analyze_schema"})
        agent.tool_executor.call_tool = MagicMock(
            return_value={"filepath": "data.parquet", "num_rows": 3, "ok": True}
        )

        result = agent._execute_tool_action(
            "parquet_analyze_schema",
            {"filepath": "data.parquet"},
            trace,
        )

        assert result["num_rows"] == 3
        assert trace.tools[0].tool == "parquet_analyze_schema"
        assert len(trace.expert_handoffs) == 1
        handoff = trace.expert_handoffs[0]
        assert handoff.agent_id == "analysis"
        assert handoff.dispatch_target == "parquet_analyze_schema"
        assert handoff.stage == "direct_tool"
        assert handoff.status == "success"

    def test_empty_answer_after_tool_uses_grounded_observation_fallback(self, agent):
        agent._plan_next_action = MagicMock(
            side_effect=[
                {
                    "action": "tool",
                    "tool": "parquet_analyze_schema",
                    "args": {"filepath": "data.parquet"},
                    "reason": "inspect parquet",
                },
                {"action": "answer", "answer": "", "reason": "answer from observations"},
            ]
        )

        def execute(tool_name, raw_args, trace, **_kwargs):
            trace.record_tool(
                tool=tool_name,
                params=raw_args,
                result={"filepath": "data.parquet", "num_rows": 3, "ok": True},
                duration_ms=1.0,
                ok=True,
            )
            return {"filepath": "data.parquet", "num_rows": 3, "ok": True}

        agent._execute_tool_action = MagicMock(side_effect=execute)
        agent._synthesize_agent_answer = MagicMock(return_value="should not run")

        result = agent.forward("Summarize data.parquet", session_id="observation-fallback")

        assert result.selected_expert == "analysis"
        assert "Parquet (parquet_analyze_schema) returned" in result.answer
        assert result.error_info is None
        agent._synthesize_agent_answer.assert_not_called()

    def test_shell_scope_validation_ignores_compiled_tool_context(self, agent, tmp_path):
        csv_path = tmp_path / "events.csv"
        csv_path.write_text("event_id,status\n1,ok\n", encoding="utf-8")

        error = agent._tool_action_scope_error(
            "shell_bash",
            selected="utility",
            question=f"What columns are in {csv_path}?",
            file_context="",
            session_context="[Available Tools]\nshell_bash: run date or shell commands",
        )

        assert error is not None
        assert error["tool"] == "shell_bash"

    def test_tool_observation_then_planner_failure_synthesizes_partial_answer(self, agent):
        agent._plan_next_action = MagicMock(
            side_effect=[
                {"action": "tool", "tool": "hdf5_analyze_file", "args": {}, "reason": "inspect"},
                RoutingError(
                    "Agent planner failed to produce an action.",
                    details={"original_error": "Planner returned invalid JSON action: None"},
                ),
            ]
        )
        agent._execute_tool_action = MagicMock(return_value={"summary": "datasets inspected"})
        agent._synthesize_agent_answer = MagicMock(return_value="Synthesized from tool results.")

        selected, answer, expert_result, error_info, route = agent._run_agent_loop(
            question="q", session_context="", file_context="", trace=_trace()
        )

        assert selected == "data"
        assert answer == "Synthesized from tool results."
        assert expert_result is None
        assert error_info is not None
        assert error_info["error"] == "routing_error"
        assert error_info["details"]["partial"] is True
        assert error_info["details"]["stage"] == "post_observation_planning"
        assert "invalid JSON" in error_info["details"]["original_error"]
        assert route.target == "data"
        agent._synthesize_agent_answer.assert_called_once()

    def test_planner_failure_without_tool_observation_still_surfaces(self, agent):
        agent._plan_next_action = MagicMock(
            side_effect=RoutingError(
                "Agent planner failed to produce an action.",
                details={"original_error": "provider refused"},
            )
        )
        agent._synthesize_agent_answer = MagicMock(return_value="should not run")

        with pytest.raises(RoutingError, match="failed to produce an action"):
            agent._run_agent_loop(question="q", session_context="", file_context="", trace=_trace())

        agent._synthesize_agent_answer.assert_not_called()

    def test_multi_file_request_is_planner_owned_when_multiple_coordinators_match(
        self,
        agent,
        tmp_path,
    ):
        first_path = tmp_path / "run.h5"
        second_path = tmp_path / "events.csv"
        first_path.touch()
        second_path.write_text("event_id,status\n1,ok\n", encoding="utf-8")
        agent.registry.register_agent(
            "second_coordinator",
            MagicMock(),
            AgentCapability(
                keywords=["triage"],
                description="Second coordinator used to prove guard ambiguity handling",
                tools=[],
                specialization="coordination",
                metadata={
                    "coordinator_intents": ["multi_file_analysis"],
                    "coordinated_file_suffixes": [".h5", ".csv"],
                },
            ),
        )
        expert_result = MagicMock()
        agent._plan_next_action = MagicMock(
            side_effect=[
                {
                    "action": "expert",
                    "expert": "analysis",
                    "question": "triage the related files",
                    "reason": "planner resolves ambiguous coordinators",
                },
                {"action": "answer", "answer": "", "reason": "expert observed"},
            ]
        )
        agent._dispatch_expert_action = MagicMock(
            return_value=("analysis", "planner triaged files", expert_result, None)
        )

        selected, answer, result, error_info, route = agent._run_agent_loop(
            question=(
                "I have two related run files and need a cross-file triage summary: "
                f"{first_path} {second_path}"
            ),
            session_context="",
            file_context="",
            trace=_trace(),
        )

        assert selected == "analysis"
        assert "planner triaged files" in answer
        assert result is expert_result
        assert error_info is None
        assert route.source == "dspy"
        assert agent._plan_next_action.call_count == 2

    def test_multi_file_request_stays_planner_owned_even_with_many_coordinators(
        self,
        agent,
        tmp_path,
    ):
        first_path = tmp_path / "run.h5"
        second_path = tmp_path / "events.csv"
        first_path.touch()
        second_path.write_text("event_id,status\n1,ok\n", encoding="utf-8")
        for index in range(25):
            agent.registry.register_agent(
                f"extra_{index}",
                MagicMock(),
                AgentCapability(
                    keywords=[f"extra_{index}"],
                    description=f"Additional expert {index}",
                    tools=[],
                    specialization="extra",
                    metadata={
                        "coordinator_intents": ["multi_file_analysis"],
                        "coordinated_file_suffixes": [f".extra{index}"],
                    },
                ),
            )
        expert_result = MagicMock()
        agent._plan_next_action = MagicMock(
            side_effect=[
                {
                    "action": "expert",
                    "expert": "analysis",
                    "question": "triage the related files",
                    "reason": "planner selected coordinator from capabilities",
                },
                {"action": "answer", "answer": "", "reason": "expert observed"},
            ]
        )
        agent._dispatch_expert_action = MagicMock(
            return_value=("analysis", "planner triaged files", expert_result, None)
        )

        selected, answer, result, error_info, route = agent._run_agent_loop(
            question=(
                "Please triage this related experiment bundle for a collaborator: "
                f"{first_path} and {second_path}"
            ),
            session_context="",
            file_context="",
            trace=_trace(),
        )

        assert selected == "analysis"
        assert "planner triaged files" in answer
        assert result is expert_result
        assert error_info is None
        assert route.source == "dspy"
        assert route.target == "analysis"
        assert agent._plan_next_action.call_count == 2

    def test_natural_multi_file_request_is_planner_owned_for_benchmarks(
        self,
        agent,
        tmp_path,
    ):
        first_path = tmp_path / "run.h5"
        second_path = tmp_path / "events.csv"
        first_path.touch()
        second_path.write_text("event_id,status\n1,ok\n", encoding="utf-8")
        expert_result = MagicMock()
        agent._plan_next_action = MagicMock(
            side_effect=[
                {
                    "action": "expert",
                    "expert": "analysis",
                    "question": "triage the related files",
                    "reason": "planner selected cross-file analysis",
                },
                {"action": "answer", "answer": "", "reason": "expert observed"},
            ]
        )
        agent._dispatch_expert_action = MagicMock(
            return_value=("analysis", "planner triaged files", expert_result, None)
        )

        selected, answer, result, error_info, route = agent._run_agent_loop(
            question=(
                "I have two related run files and need a cross-file triage summary: "
                f"{first_path} {second_path}"
            ),
            session_context="",
            file_context="",
            trace=_trace(),
        )

        assert selected == "analysis"
        assert "planner triaged files" in answer
        assert result is expert_result
        assert error_info is None
        assert route.source == "dspy"
        assert agent._plan_next_action.call_count == 2
        assert route.reason == "expert observed"

    def test_reasoning_only_bypasses_adios_guard_for_planner_benchmarks(
        self,
        agent,
        tmp_path,
    ):
        adios_path = tmp_path / "run.bp5"
        adios_path.mkdir()
        expert_result = MagicMock()
        agent._plan_next_action = MagicMock(
            side_effect=[
                {
                    "action": "expert",
                    "expert": "data",
                    "question": f"inspect {adios_path}",
                    "reason": "planner selected data expert for BP5",
                },
                {"action": "answer", "answer": "", "reason": "expert observed"},
            ]
        )
        agent._dispatch_expert_action = MagicMock(
            return_value=("data", "planner inspected bp5", expert_result, None)
        )

        selected, answer, result, error_info, route = agent._run_agent_loop(
            question=f"Inspect this ADIOS BP5 file: {adios_path}",
            session_context="",
            file_context="",
            trace=_trace(),
            routing_mode="reasoning_only",
        )

        assert selected == "data"
        assert "planner inspected bp5" in answer
        assert result is expert_result
        assert error_info is None
        assert route.source == "dspy"
        assert route.reason == "expert observed"
        assert agent._plan_next_action.call_count == 2

    def test_forward_promotes_propose_edit_observation_to_file_diffs(self, agent):
        proposed = {
            "path": "/tmp/example.py",
            "unified_diff": "--- a/example.py\n+++ b/example.py\n@@\n-old\n+new",
            "new_content": "new\n",
            "lines_added": 1,
            "lines_removed": 1,
        }
        agent._plan_next_action = MagicMock(
            side_effect=[
                {
                    "action": "tool",
                    "tool": "fs_propose_edit",
                    "args": {
                        "filepath": "/tmp/example.py",
                        "new_content": "new\n",
                    },
                    "reason": "propose edit",
                },
                {"action": "answer", "answer": "Review the proposed diff.", "reason": "done"},
            ]
        )
        agent._selected_expert_for_tool = MagicMock(return_value="data")

        def execute(tool_name, raw_args, trace, **_kwargs):
            trace.record_tool(
                tool=tool_name,
                params=raw_args,
                result=proposed,
                duration_ms=1.0,
                ok=True,
            )
            return proposed

        agent._execute_tool_action = MagicMock(side_effect=execute)

        result = agent.forward(
            "Change example.py",
            session_id="diff-promotion",
            session_edit_mode="patch",
        )

        assert result.file_diffs == [
            {
                "path": "/tmp/example.py",
                "unified_diff": proposed["unified_diff"],
                "new_content": "new\n",
                "edit_mode": "patch",
                "lines_added": 1,
                "lines_removed": 1,
            }
        ]

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

    def test_answer_action_without_text_surfaces_routing_error(self, agent):
        agent._plan_next_action = MagicMock(return_value={"action": "answer", "answer": ""})
        agent._run_chat_agent = MagicMock(return_value="fallback should not run")

        with pytest.raises(RoutingError, match="did not provide usable text") as exc_info:
            agent._run_agent_loop(
                question="Tell me about CLIO capabilities.",
                session_context="",
                file_context="",
                trace=_trace(),
            )

        assert exc_info.value.details["planner_action"]["action"] == "answer"
        agent._run_chat_agent.assert_not_called()

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

    def test_none_action_with_stale_in_scope_answer_surfaces_routing_error(self, agent):
        stale = "HDF5 and Parquet are scientific data formats."
        agent._plan_next_action = MagicMock(
            return_value={"action": "none", "answer": stale, "reason": "no handler"}
        )
        agent._run_chat_agent = MagicMock(return_value="summary answer")

        with pytest.raises(RoutingError, match="stale or in-scope answer text") as exc_info:
            agent._run_agent_loop(
                question="Summarize your previous answers in one sentence.",
                session_context=f"user: What can you do?\nassistant: {stale}",
                file_context="",
                trace=_trace(),
            )

        assert exc_info.value.details["replacement_reason"] == "stale_or_in_scope_text"
        agent._run_chat_agent.assert_not_called()

    def test_planner_answer_leaking_file_context_surfaces_routing_error(self, agent):
        agent._plan_next_action = MagicMock(
            return_value={
                "action": "answer",
                "answer": "The file_context is empty, so I need more context.",
            }
        )
        agent._run_chat_agent = MagicMock(return_value="clean answer")

        with pytest.raises(RoutingError, match="stale or invalid direct answer text") as exc_info:
            agent._run_agent_loop(
                question="Explain one safe next step for analyzing a local data file.",
                session_context="",
                file_context="",
                trace=_trace(),
            )

        assert exc_info.value.details["replacement_reason"] == "stale_or_invalid_answer_text"
        agent._run_chat_agent.assert_not_called()

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
        assert "partial" in answer
        assert "step limit" in route.reason.lower()
        assert error_info is not None
        assert error_info["error"] == "routing_error"
        assert error_info["details"]["partial"] is True
        assert error_info["details"]["stage"] == "step_limit_after_observations"
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

        assert "partial" in result.answer
        assert result.error_info is not None
        assert result.error_info["error"] == "routing_error"
        assert "step limit" in result.error_info["message"]
        details = result.error_info["details"]
        assert details["stage"] == "step_limit_after_observations"
        assert details["recovery_actions"] == ["retry", "reconfigure_provider", "exit"]

    def test_answer_synthesis_exception_uses_observation_fallback(self, agent):
        agent.answer_synthesizer = MagicMock(
            side_effect=ValueError(
                "Adapter failure\n"
                "LM Response:\n[[\n"
                "Expected to find output fields in response: [answer]"
            )
        )

        answer = agent._synthesize_agent_answer(
            question="Review data quality",
            session_context="",
            observations=[
                {
                    "step": 1,
                    "type": "tool",
                    "tool": "parquet_compute_statistics",
                    "ok": True,
                    "result": {
                        "column": "temperature_k",
                        "null_count": 18,
                        "total_count": 3000,
                    },
                }
            ],
        )

        assert "temperature_k" in answer
        assert "null_count=18" in answer

    def test_answer_synthesis_retries_transient_provider_error(self, agent, monkeypatch):
        monkeypatch.setenv("CLIO_TRANSIENT_PROVIDER_RETRY_DELAYS", "0")
        agent.answer_synthesizer = MagicMock(
            side_effect=[
                RuntimeError("litellm.RateLimitError: Tokens/minute limit exceeded"),
                MagicMock(answer="Recovered answer."),
            ]
        )

        answer = agent._synthesize_agent_answer(
            question="Review data quality",
            session_context="",
            observations=[
                {
                    "step": 1,
                    "type": "tool",
                    "tool": "parquet_analyze_schema",
                    "ok": True,
                    "result": {"num_rows": 3000},
                }
            ],
        )

        assert answer == "Recovered answer."
        assert agent.answer_synthesizer.call_count == 2

    def test_observation_fallback_labels_ndp_tools(self):
        answer = ClioAgent._fallback_answer_from_observations(
            [
                {
                    "tool": "ndp_search_datasets",
                    "ok": True,
                    "result": {
                        "datasets": {"items": [{"name": "noaa-example-dataset"}]},
                        "ok": True,
                    },
                }
            ]
        )

        assert "National Data Platform" in answer
        assert "noaa-example-dataset" in answer

    def test_empty_answer_synthesis_uses_observation_fallback(self, agent, monkeypatch):
        monkeypatch.setenv("CLIO_AGENT_MAX_STEPS", "1")
        agent._plan_next_action = MagicMock(
            return_value={"action": "tool", "tool": "hdf5_analyze_file", "args": {}}
        )
        agent._execute_tool_action = MagicMock(return_value={"value": "partial"})
        agent.answer_synthesizer = MagicMock(return_value=MagicMock(answer=""))

        result = agent.forward("summarize the file", session_id="empty-synthesis")

        assert "hdf5_analyze_file" in result.answer
        assert '"value": "partial"' in result.answer
        assert result.error_info is not None
        assert result.error_info["details"]["stage"] == "step_limit_after_observations"


class TestExecuteToolAction:
    def test_repair_filepath_arg_from_explicit_context(self, tmp_path):
        actual = tmp_path / "data" / "sensor_events.csv"
        actual.parent.mkdir()
        actual.write_text("event_id,status\n1,ok\n", encoding="utf-8")
        degraded = tmp_path / "sensor_events.csv"

        args = ClioAgent._repair_filepath_arg_from_context(
            {"filepath": str(degraded)},
            question=f"Inspect {actual}",
            file_context="",
        )

        assert args["filepath"] == str(actual)

    def test_repair_filepath_arg_from_session_context(self, tmp_path):
        actual = tmp_path / "data" / "facility_measurements.parquet"
        actual.parent.mkdir()
        actual.touch()
        degraded = tmp_path / "facility_measurements.parquet"

        args = ClioAgent._repair_filepath_arg_from_context(
            {"filepath": str(degraded)},
            question="Create a dashboard from the Parquet file we just profiled.",
            file_context="",
            session_context=f"assistant: Profiled Parquet file {actual}",
        )

        assert args["filepath"] == str(actual)

    def test_repair_filepath_arg_keeps_unmatched_missing_path(self, tmp_path):
        degraded = tmp_path / "missing.csv"

        args = ClioAgent._repair_filepath_arg_from_context(
            {"filepath": str(degraded)},
            question="Inspect a CSV file.",
            file_context="",
        )

        assert args["filepath"] == str(degraded)

    def test_repair_question_filepaths_from_explicit_context(self, tmp_path):
        actual = tmp_path / "data" / "sensor_events.csv"
        actual.parent.mkdir()
        actual.write_text("event_id,status\n1,ok\n", encoding="utf-8")
        degraded = tmp_path / "sensor_events.csv"

        text = ClioAgent._repair_question_filepaths_from_context(
            f"Inspect {degraded}",
            source_question=f"Please inspect {actual}",
            file_context="",
        )

        assert str(actual) in text
        assert str(degraded) not in text

    def test_repair_question_relative_filepath_from_explicit_context(self, tmp_path):
        actual = tmp_path / "data" / "sensor_events.csv"
        actual.parent.mkdir()
        actual.write_text("event_id,status\n1,ok\n", encoding="utf-8")

        text = ClioAgent._repair_question_filepaths_from_context(
            "Inspect sensor_events.csv",
            source_question=f"Please inspect {actual}",
            file_context="",
        )

        assert text == f"Inspect {actual}"

    def test_repair_question_drive_relative_windows_filepath_from_explicit_context(
        self,
        tmp_path,
    ):
        actual_path = tmp_path / "clio-benchmark-data" / "facility_measurements_dirty.parquet"
        actual_path.parent.mkdir()
        actual_path.touch()
        actual = str(actual_path)
        degraded = actual.replace(":\\", ":", 1)

        text = ClioAgent._repair_question_filepaths_from_context(
            f"Review {degraded}",
            source_question=f"Review {actual}",
            file_context="",
        )

        assert text == f"Review {actual}"

    def test_unknown_tool_returns_structured_error(self, agent):
        result = agent._execute_tool_action("not_a_real_tool", {}, _trace())
        assert "error" in result
        assert result["error"]["code"] == "unknown_tool"

    def test_visualization_tool_notifies_global_observer(self, agent, tmp_path):
        observed = []

        def observer(name, args, phase, error):
            observed.append((name, dict(args), phase, error))

        def fake_tool(filepath: str, output_path: str) -> dict[str, str]:
            return {"filepath": filepath, "output_path": output_path}

        output_path = tmp_path / "plot.png"
        set_global_tool_observer(observer)
        try:
            result = agent._execute_visualization_tool(
                "plot_summary",
                fake_tool,
                {"filepath": "data.parquet", "output_path": str(output_path)},
            )
        finally:
            set_global_tool_observer(None)

        assert result == {"filepath": "data.parquet", "output_path": str(output_path)}
        assert observed == [
            (
                "plot_summary",
                {"filepath": "data.parquet", "output_path": str(output_path)},
                "started",
                None,
            ),
            (
                "plot_summary",
                {"filepath": "data.parquet", "output_path": str(output_path)},
                "completed",
                None,
            ),
        ]

    def test_visualization_tool_notifies_global_observer_on_error(self, agent, tmp_path):
        observed = []

        def observer(name, args, phase, error):
            observed.append((name, dict(args), phase, error))

        def failing_tool(filepath: str, output_path: str) -> dict[str, str]:
            raise ValueError(f"cannot render {filepath} to {output_path}")

        output_path = tmp_path / "plot.png"
        set_global_tool_observer(observer)
        try:
            result = agent._execute_visualization_tool(
                "plot_summary",
                failing_tool,
                {"filepath": "data.parquet", "output_path": str(output_path)},
            )
        finally:
            set_global_tool_observer(None)

        assert "error" in result
        assert observed[0] == (
            "plot_summary",
            {"filepath": "data.parquet", "output_path": str(output_path)},
            "started",
            None,
        )
        assert observed[1][0] == "plot_summary"
        assert observed[1][1] == {"filepath": "data.parquet", "output_path": str(output_path)}
        assert observed[1][2] == "completed"
        assert observed[1][3] is not None
        assert "cannot render data.parquet" in observed[1][3]

    def test_run_local_tool_notifies_global_observer(self, agent):
        observed = []

        def observer(name, args, phase, error):
            observed.append((name, dict(args), phase, error))

        def local_echo(value: str) -> str:
            return f"ok:{value}"

        set_global_tool_observer(observer)
        try:
            result = agent._run_local_tool("local_echo", local_echo, "hello")
        finally:
            set_global_tool_observer(None)

        assert result == "ok:hello"
        assert observed == [
            ("local_echo", {"value": "hello"}, "started", None),
            ("local_echo", {"value": "hello"}, "completed", None),
        ]

    def test_run_local_tool_notifies_global_observer_on_error(self, agent):
        observed = []

        def observer(name, args, phase, error):
            observed.append((name, dict(args), phase, error))

        def failing_tool(value: str) -> str:
            raise RuntimeError(f"failed {value}")

        set_global_tool_observer(observer)
        try:
            with pytest.raises(RuntimeError, match="failed hello"):
                agent._run_local_tool("local_fail", failing_tool, value="hello")
        finally:
            set_global_tool_observer(None)

        assert observed[0] == ("local_fail", {"value": "hello"}, "started", None)
        assert observed[1][0] == "local_fail"
        assert observed[1][1] == {"value": "hello"}
        assert observed[1][2] == "completed"
        assert observed[1][3] is not None
        assert "failed hello" in observed[1][3]

    def test_run_local_tool_reports_cancellation_instead_of_success(self, agent):
        observed = []
        cancelled = False

        def observer(name, args, phase, error):
            observed.append((name, dict(args), phase, error))

        def local_echo(value: str) -> str:
            nonlocal cancelled
            cancelled = True
            return f"ok:{value}"

        set_global_tool_observer(observer)
        try:
            with cancellation_checker(lambda: cancelled):
                with pytest.raises(CancellationError):
                    agent._run_local_tool("local_echo", local_echo, "hello")
        finally:
            set_global_tool_observer(None)

        assert observed[0] == ("local_echo", {"value": "hello"}, "started", None)
        assert observed[1][0] == "local_echo"
        assert observed[1][1] == {"value": "hello"}
        assert observed[1][2] == "completed"
        assert "turn cancelled by client" in observed[1][3]

    def test_visualization_tool_reports_cancellation_instead_of_success(
        self,
        agent,
        tmp_path,
    ):
        observed = []
        cancelled = False

        def observer(name, args, phase, error):
            observed.append((name, dict(args), phase, error))

        def fake_tool(filepath: str, output_path: str) -> dict[str, str]:
            nonlocal cancelled
            cancelled = True
            return {"filepath": filepath, "output_path": output_path}

        output_path = tmp_path / "plot.png"
        set_global_tool_observer(observer)
        try:
            with cancellation_checker(lambda: cancelled):
                with pytest.raises(CancellationError):
                    agent._execute_visualization_tool(
                        "plot_summary",
                        fake_tool,
                        {"filepath": "data.parquet", "output_path": str(output_path)},
                    )
        finally:
            set_global_tool_observer(None)

        assert observed[0] == (
            "plot_summary",
            {"filepath": "data.parquet", "output_path": str(output_path)},
            "started",
            None,
        )
        assert observed[1][0] == "plot_summary"
        assert observed[1][1] == {"filepath": "data.parquet", "output_path": str(output_path)}
        assert observed[1][2] == "completed"
        assert "turn cancelled by client" in observed[1][3]


# --------------------------------------------------------------------------
# DSPy -> LiteLLM exclusivity (iowarp/clio-agent#54).
# These tests lock in the contract that there is NO raw-HTTP side
# channel for either the planner or the chat agent. If the DSPy layer
# fails, the failure propagates -- the agent never reaches for
# requests.post.
# --------------------------------------------------------------------------


class TestPlannerNoBypass:
    def test_planner_strips_duplicate_available_tools_context(self, agent):
        agent.action_planner = MagicMock(
            return_value=MagicMock(action_json='{"action":"answer","answer":"ok"}')
        )
        session_context = (
            "[Session Context]\n"
            "user: compare these files\n\n"
            "[Available Tools]\n"
            "hdf5_list_datasets: list HDF5 datasets\n"
            "parquet_analyze_schema: inspect parquet schema\n\n"
            "[Routing History]\n"
            "previous query -> analysis"
        )

        action = agent._plan_next_action(
            question="compare files",
            session_context=session_context,
            file_context="",
            capabilities="analysis: cross-file scientific analysis",
            observations=[],
        )

        assert action == {"action": "answer", "answer": "ok"}
        planner_context = agent.action_planner.call_args.kwargs["session_context"]
        assert "[Session Context]" in planner_context
        assert "user: compare these files" in planner_context
        assert "[Routing History]" in planner_context
        assert "previous query -> analysis" in planner_context
        assert "[Available Tools]" not in planner_context
        assert "hdf5_list_datasets" not in planner_context

    def test_planner_context_is_never_empty_after_tool_section_strip(self):
        assert (
            ClioAgent._planner_session_context("[Available Tools]\nhdf5_list_datasets: list")
            == "No prior context"
        )

    def test_chat_strips_available_tools_context(self, agent):
        agent.chat_agent = MagicMock(return_value=MagicMock(answer="ok"))
        session_context = (
            "[Session Context]\n"
            "user: what can you do?\n\n"
            "[Available Tools]\n"
            "hdf5_list_datasets: list HDF5 datasets\n"
            "shell_bash: run a shell command\n\n"
            "[Routing History]\n"
            "previous query -> analysis"
        )

        assert agent._run_chat_agent("what tools do you have?", session_context) == "ok"

        chat_context = agent.chat_agent.call_args.kwargs["session_context"]
        assert "[Session Context]" in chat_context
        assert "user: what can you do?" in chat_context
        assert "[Routing History]" in chat_context
        assert "previous query -> analysis" in chat_context
        assert "[Available Tools]" not in chat_context
        assert "hdf5_list_datasets" not in chat_context
        assert "shell_bash" not in chat_context

    def test_chat_context_is_never_empty_after_tool_section_strip(self):
        assert (
            ClioAgent._chat_session_context("[Available Tools]\nhdf5_list_datasets: list")
            == "No prior context"
        )

    def test_qwopus_planner_question_uses_no_think_control(self, agent):
        agent._provider_config.provider = "lm_studio"
        agent._provider_config.model = "qwopus3.5-9b-v3"
        agent.action_planner = MagicMock(
            return_value=MagicMock(action_json='{"action":"answer","answer":"ok"}')
        )

        agent._plan_next_action(
            question="triage these scientific files",
            session_context="",
            file_context="",
            capabilities="analysis: cross-file analysis",
            observations=[],
        )

        planner_question = agent.action_planner.call_args.kwargs["question"]
        assert planner_question.startswith("/no_think\n")
        assert "Return only the action_json JSON object" in planner_question
        assert planner_question.endswith("triage these scientific files")

    def test_non_reasoning_planner_question_is_unchanged(self, agent):
        agent._provider_config.provider = "lm_studio"
        agent._provider_config.model = "granite-4-h-tiny"

        assert agent._planner_question("hello") == "hello"

    def test_planner_retries_with_compact_capabilities(self, agent):
        agent.action_planner = MagicMock(
            side_effect=[
                RuntimeError("truncated full prompt"),
                MagicMock(action_json='{"action":"expert","expert":"analysis","question":""}'),
            ]
        )
        capabilities = (
            "Experts:\n"
            "- analysis: Cross-file analysis.; tools: parquet_analyze_schema, csv_read_table\n"
            "Scoped tools:\n"
            "- analysis:\n"
            "  - parquet_analyze_schema(filepath): Inspect the schema and metadata.\n"
            "Routing strategy: delegate natural multi-file review to expert:analysis."
        )

        action = agent._plan_next_action(
            question="triage files",
            session_context="",
            file_context="",
            capabilities=capabilities,
            observations=[],
        )

        assert action == {"action": "expert", "expert": "analysis", "question": ""}
        assert agent.action_planner.call_count == 2
        retry_capabilities = agent.action_planner.call_args.kwargs["capabilities"]
        assert "- parquet_analyze_schema(filepath)" in retry_capabilities
        assert "Inspect the schema and metadata" not in retry_capabilities

    def test_planner_retries_transient_provider_error_before_compacting(self, agent, monkeypatch):
        monkeypatch.setenv("CLIO_TRANSIENT_PROVIDER_RETRY_DELAYS", "0")
        agent.action_planner = MagicMock(
            side_effect=[
                RuntimeError("litellm.RateLimitError: Tokens/minute limit exceeded"),
                MagicMock(action_json='{"action":"answer","answer":"ok"}'),
            ]
        )

        action = agent._plan_next_action(
            question="hi",
            session_context="",
            file_context="",
            capabilities="analysis: parquet",
            observations=[],
        )

        assert action == {"action": "answer", "answer": "ok"}
        assert agent.action_planner.call_count == 2

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
    def test_chat_utility_question_uses_scoped_react_surface(self, agent):
        tool_agent = MagicMock(return_value=MagicMock(answer="It is today."))
        agent._build_chat_tool_agent = MagicMock(return_value=tool_agent)
        agent.chat_agent = MagicMock(return_value=MagicMock(answer="plain chat"))
        trace = _trace()

        answer = agent._run_chat_agent("What is the current time?", "", trace=trace)

        assert answer == "It is today."
        agent._build_chat_tool_agent.assert_called_once_with(
            trace=trace,
            question="What is the current time?",
            session_context="",
        )
        tool_agent.assert_called_once()
        agent.chat_agent.assert_not_called()

    def test_non_utility_chat_still_uses_plain_chat_agent(self, agent):
        agent.chat_agent = MagicMock(return_value=MagicMock(answer="plain chat"))
        agent._build_chat_tool_agent = MagicMock()

        answer = agent._run_chat_agent("Explain what CLIO can do.", "", trace=_trace())

        assert answer == "plain chat"
        agent.chat_agent.assert_called_once()
        agent._build_chat_tool_agent.assert_not_called()

    def test_chat_scoped_tool_records_through_normal_tool_action_path(self, agent):
        trace = _trace()
        source_tool = MagicMock()
        source_tool.name = "shell_bash"
        source_tool.desc = "Run a shell command."
        source_tool.args = {"command": {"type": "string"}}
        agent._execute_tool_action = MagicMock(return_value={"stdout": "today"})

        tool = agent._chat_scoped_tool(
            source_tool,
            trace,
            "What time is it?",
            "",
        )
        result = tool.func(command="date")

        assert result == {"stdout": "today"}
        agent._execute_tool_action.assert_called_once_with(
            "shell_bash",
            {"command": "date"},
            trace,
            question="What time is it?",
            file_context="",
            session_context="",
        )

    def test_chat_adapter_parse_failure_recovers_visible_model_answer(self, agent):
        agent.chat_agent = MagicMock(
            side_effect=ValueError(
                "Adapter ChatAdapter failed to parse the LM response.\n\n"
                "LM Response: [[ ## answer ##\nUseful answer text.\n"
                "[[ ## completed ## ]]\n\n"
                "Expected to find output fields in the LM response: [answer]\n\n"
                "Actual output fields parsed from the LM response: []"
            )
        )

        assert agent._run_chat_agent("hi", "") == "Useful answer text."

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
