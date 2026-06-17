from __future__ import annotations

from dataclasses import dataclass

from clio_agent.gact.app import (
    _compact_dynamic_delegation_output,
    _dynamic_parent_resume_prompt,
    _latest_parent_resumed_output_summary,
)


@dataclass(frozen=True)
class _Agent:
    id: str






def test_parent_resume_prompt_receives_compacted_continuation_contracts() -> None:
    child_summary = _compact_dynamic_delegation_output(
        "\n".join(
            [
                "Analysis completed from fresh SAC evidence.",
                "NEXT_EXPERT: visualization",
                "NEXT_ACTION: plot_sac_traces /tmp/fresh.sac",
                "DO_NOT_FINALIZE_BEFORE_VISUALIZATION: true",
                *[f"long note {idx}" for idx in range(180)],
            ]
        ),
        limit=700,
    )

    prompt = _dynamic_parent_resume_prompt(
        "Recover waveform evidence and produce a PNG artifact.",
        _Agent(id="main"),  # type: ignore[arg-type]
        [
            {
                "stage": "delegate.completed",
                "agent_id": "analysis",
                "status": "completed",
                "output_summary": child_summary,
            }
        ],
    )

    assert "NEXT_EXPERT: visualization" in prompt
    assert "NEXT_ACTION: plot_sac_traces /tmp/fresh.sac" in prompt
    assert "DO_NOT_FINALIZE_BEFORE_VISUALIZATION: true" in prompt


def test_latest_parent_resumed_output_summary_prefers_final_nested_parent_result() -> None:
    rows = [
        {
            "stage": "delegate.completed",
            "agent_id": "per_sample_metrics",
            "parent_id": "cohort_qc",
            "output_summary": "Initial metrics child result.",
        },
        {
            "stage": "parent.resumed",
            "agent_id": "cohort_qc",
            "resumed_from": "per_sample_metrics",
            "output_summary": "Metrics summarized by coordinator.",
        },
        {
            "stage": "delegate.completed",
            "agent_id": "manifest_reconciliation",
            "parent_id": "cohort_qc",
            "output_summary": "No manifest was provided.",
        },
        {
            "stage": "parent.resumed",
            "agent_id": "cohort_qc",
            "resumed_from": "manifest_reconciliation",
            "output_summary": "Final coordinator answer with metrics and manifest caveat.",
        },
    ]

    assert (
        _latest_parent_resumed_output_summary(rows, "cohort_qc")
        == "Final coordinator answer with metrics and manifest caveat."
    )




