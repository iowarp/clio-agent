"""#880 — delegation-continuation invariants that SURVIVE the deleted summary layer.

``tests/test_gact/test_delegation_contract_compaction.py`` was retired wholesale when
the server-authored ``return_summary.py`` layer was deleted. But three of its pins
guarded invariants of code paths that STILL EXIST and are MORE load-bearing now that
``output`` carries the child's answer verbatim (a potentially 4000-char JSON blob):

* :func:`_expert_handoff_fields` — the typed parent/child/stage/status extraction a
  client consumes instead of parsing a prose label.
* :func:`_dynamic_parent_resume_prompt` — the parent resume prompt receives the child's
  ``output`` VERBATIM with no truncation (critical: a long structured answer must not be
  silently clipped before the parent routes on it).
* :func:`_latest_parent_resumed_output` (renamed from ``*_output_summary``) — prefers the
  FINAL nested ``parent.resumed`` result when several are present.

These are re-pinned here (the renderer/summary pins were correctly retired with
``return_summary.py``); the code paths they exercise are live production code.
"""

from __future__ import annotations

from dataclasses import dataclass

from clio_agent.gact.app import (
    _dynamic_parent_resume_prompt,
    _expert_handoff_fields,
    _latest_parent_resumed_output,
)
from clio_agent.gact.types import Part
from tests.test_gact.earthscope_schema import EARTHSCOPE_WORKFLOW_STATE_SCHEMA


@dataclass(frozen=True)
class _Agent:
    id: str


def test_expert_handoff_part_carries_structured_fields_from_row() -> None:
    """An ``expert_handoff`` Part exposes the delegation as typed fields (parent/child/
    stage/status) drawn from the structured row, so a client never parses the prose
    ``text`` label to attribute the handoff."""

    row = {
        "agent_id": "geospatial",  # the child that received the delegation
        "parent_id": "main",  # the parent that made it
        "stage": "delegate.completed",
        "status": "completed",
        "output": "staged waveform",  # the child's answer rides ``output`` verbatim
    }
    fields = _expert_handoff_fields(row)
    assert fields == {
        "parent_agent": "main",
        "child_agent": "geospatial",
        "stage": "delegate.completed",
        "status": "completed",
    }

    part = Part(
        type="expert_handoff",
        agent_id=fields["parent_agent"],
        parent_agent=fields["parent_agent"],
        child_agent=fields["child_agent"],
        stage=fields["stage"],
        status=fields["status"],
        text="",  # the UI consumes the structured fields, not the string
    )
    # The handoff is fully described without the prose ``text``.
    assert part.text == ""
    assert part.parent_agent == "main"
    assert part.child_agent == "geospatial"
    assert part.stage == "delegate.completed"
    assert part.status == "completed"
    # The generating party is the parent.
    assert part.agent_id == "main"


def test_parent_resume_prompt_receives_genuine_child_output_verbatim() -> None:
    """The child's GENUINE ``output`` flows to the parent VERBATIM — no truncation.

    More load-bearing after #880: ``output`` may be a long structured answer, and the
    parent must see all of it (including the last line) to route correctly. A
    re-introduced heuristic truncation would drop routing-critical tail content.
    """
    child_output = "\n".join(
        [
            "Analysis completed from fresh SAC evidence.",
            "NEXT_EXPERT: visualization",
            "NEXT_ACTION: plot_sac_traces /tmp/fresh.sac",
            "DO_NOT_FINALIZE_BEFORE_VISUALIZATION: true",
            *[f"long note {idx}" for idx in range(180)],
        ]
    )

    prompt = _dynamic_parent_resume_prompt(
        "Recover waveform evidence and produce a PNG artifact.",
        _Agent(id="main"),  # type: ignore[arg-type]
        [
            {
                "stage": "delegate.completed",
                "agent_id": "analysis",
                "status": "completed",
                "output": child_output,
            }
        ],
        schema=EARTHSCOPE_WORKFLOW_STATE_SCHEMA,
    )

    assert "NEXT_EXPERT: visualization" in prompt
    assert "NEXT_ACTION: plot_sac_traces /tmp/fresh.sac" in prompt
    assert "DO_NOT_FINALIZE_BEFORE_VISUALIZATION: true" in prompt
    # Full output flows: even the last long note survives, and nothing is truncated.
    assert "long note 179" in prompt
    assert "truncated" not in prompt


def test_latest_parent_resumed_output_prefers_final_nested_parent_result() -> None:
    """``_latest_parent_resumed_output`` returns the LAST ``parent.resumed`` output for
    the coordinator, so the feed to the empty-answer fallback / strict-depth completion
    is the final coordinator answer, not an earlier intermediate one."""
    rows = [
        {
            "stage": "delegate.completed",
            "agent_id": "per_sample_metrics",
            "parent_id": "cohort_qc",
            "output": "Initial metrics child result.",
        },
        {
            "stage": "parent.resumed",
            "agent_id": "cohort_qc",
            "resumed_from": "per_sample_metrics",
            "output": "Metrics summarized by coordinator.",
        },
        {
            "stage": "delegate.completed",
            "agent_id": "manifest_reconciliation",
            "parent_id": "cohort_qc",
            "output": "No manifest was provided.",
        },
        {
            "stage": "parent.resumed",
            "agent_id": "cohort_qc",
            "resumed_from": "manifest_reconciliation",
            "output": "Final coordinator answer with metrics and manifest caveat.",
        },
    ]

    assert (
        _latest_parent_resumed_output(rows, "cohort_qc")
        == "Final coordinator answer with metrics and manifest caveat."
    )
