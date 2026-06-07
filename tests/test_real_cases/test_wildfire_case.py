"""Wildfire downwind smoke-impact — real case acceptance test (grind target).

Encodes the contract from ``benchmark/ndp-wildfire-smoke-impact/GOAL.md``.
Expected to FAIL until the case is iterated to productive; every failure that a
trace review (the agent's autonomous job, no human in the loop) explains gets
frozen here as a tighter matcher.

Run live: ``CLIO_RUN_LIVE=1 pytest tests/test_real_cases/test_wildfire_case.py
--provider argonne_metis``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_test import matcher

CASE_DIR = "benchmark/ndp-wildfire-smoke-impact"
PROMPT = Path(CASE_DIR, "prompt.txt").read_text().strip()


@matcher
def selected_by_impact_not_size(run):
    """The run reached an impact decision and, when impact is present, named a
    selected fire. Reads the merged typed `workflow_state.impact`, not prose."""
    impact = (run.extra.get("workflow_state") or {}).get("impact") or {}
    if "present" not in impact:
        return False  # analysis never emitted a typed impact decision
    if impact.get("present"):
        return bool(impact.get("selected_fire"))
    # A null-impact result is valid ONLY if it was a real overlap evaluation,
    # not an acquisition/geometry failure dressed up as "no impact".
    reason = str(impact.get("reason", "")).lower()
    failureish = ("missing", "fail", "error", "unavailable", "could not", "prevent", "no fire data")
    return not any(w in reason for w in failureish)


@pytest.mark.real_case
@pytest.mark.live
def test_wildfire_downwind_impact(agent):
    run = agent.run({
        "task": PROMPT,
        "blueprint_id": "wildfire-smoke-impact-review",
        "case_dir": CASE_DIR,
        "run_label": "acceptance",
        "timeout_s": 600,
    })

    # Runtime/harness invariants.
    assert run.error is None, run.error
    assert run.extra["blueprint_activated"], run.extra.get("active_agent_blueprint_id")

    # Data pathways: the live feature services were queried.
    assert run.called("ndp_query_arcgis_features"), run.tool_names

    # Route: acquisition -> impact analysis -> visualization -> synthesis.
    assert run.routed_to("data"), run.steps
    assert run.routed_to("analysis"), run.steps
    assert run.routed_to("visualization"), run.steps
    assert run.routed_to("synthesis"), run.steps

    # Real deliverable: a layered map PNG was rendered and exists on disk.
    assert run.called("geospatial_render_feature_map"), run.tool_names
    assert any(p.endswith(".png") for p in run.extra["artifacts"]), run.extra["artifacts"]

    # Semantics frozen from review.
    assert selected_by_impact_not_size(run)
