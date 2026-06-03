from __future__ import annotations

from dataclasses import dataclass

from clio_agent.gact.app import (
    _compact_dynamic_delegation_output,
    _dynamic_answer_has_pending_child_work,
    _dynamic_parent_resume_prompt,
)


@dataclass(frozen=True)
class _Agent:
    id: str


def test_compact_delegation_output_preserves_continuation_contracts() -> None:
    sac_path = "/tmp/clio-seismic-staging/earthscope_IU_ANMO_00_BHZ.sac"
    output = "\n".join(
        [
            "Fresh bounded waveform evidence was recovered.",
            f"Staged path: {sac_path}",
            "NEXT_EXPERT: visualization",
            f"NEXT_ACTION: plot_sac_traces {sac_path}",
            "DO_NOT_FINALIZE_BEFORE_VISUALIZATION: true",
            "Trace statistics:",
            "- npts: 1200",
            "- delta_s: 0.05",
            *[f"verbose supporting note {idx}" for idx in range(220)],
        ]
    )

    compact = _compact_dynamic_delegation_output(output, limit=900)

    assert "Retained delegation continuation contracts:" in compact
    assert "NEXT_EXPERT: visualization" in compact
    assert f"NEXT_ACTION: plot_sac_traces {sac_path}" in compact
    assert "DO_NOT_FINALIZE_BEFORE_VISUALIZATION: true" in compact
    assert sac_path in compact
    assert "npts: 1200" in compact


def test_dynamic_answer_has_pending_child_work_detects_delegation_prose() -> None:
    assert _dynamic_answer_has_pending_child_work(
        "Delegating this review through mass_spec and search_readiness."
    )
    assert _dynamic_answer_has_pending_child_work(
        "This should route to the child expert before final synthesis."
    )


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
