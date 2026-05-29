from unittest.mock import MagicMock

from clio_agent.experts.analysis_expert import AnalysisExpert
from clio_agent.harness import ExpertRequest, ExpertResult


def test_context_sac_path_is_inspected_before_retained_synthesis(tmp_path):
    sac_path = tmp_path / "earthscope_IU_ANMO_00_BHZ_2010.sac"
    sac_path.touch()
    expert = AnalysisExpert(tool_executor=MagicMock())
    expert._should_synthesize_multi_source_evidence = MagicMock(return_value=True)
    expert._synthesize_without_tools = MagicMock(
        return_value=ExpertResult(
            analysis="stale retained synthesis",
            recommendations="do not use",
            source="deterministic",
            metadata={"expert": "analysis"},
        )
    )
    expert.sac_format_expert = MagicMock()
    expert.sac_format_expert.compute_trace_statistics.return_value = ExpertResult(
        analysis="fresh SAC stats",
        recommendations="plot next",
        source="deterministic",
        metadata={"expert": "sac_format"},
    )

    result = expert.run(
        ExpertRequest(
            question="Compute representative trace statistics.",
            file_context=(
                "[Current turn observations]\n"
                f"Data staged {sac_path} for analysis."
            ),
        )
    )

    assert result.analysis == "fresh SAC stats"
    expert.sac_format_expert.compute_trace_statistics.assert_called_once_with(
        str(sac_path)
    )
    expert._synthesize_without_tools.assert_not_called()
