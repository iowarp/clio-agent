"""Wildfire downwind smoke-impact — real case acceptance test (grind target).

Encodes the contract from ``benchmark/ndp-wildfire-smoke-impact/GOAL.md``.
Expected to FAIL until the case is iterated to productive; every failure that a
trace review (the agent's autonomous job, no human in the loop) explains gets
frozen here as a tighter matcher.

Run live: ``CLIO_RUN_LIVE=1 pytest tests/test_real_cases/test_wildfire_case.py
--provider argonne_sophia``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_test import matcher

CASE_DIR = "benchmark/ndp-wildfire-smoke-impact"
PROMPT = Path(CASE_DIR, "prompt.txt").read_text().strip()


@matcher
def selected_by_impact_not_size(run):
    """The chosen fire is justified by downwind impact (smoke over monitored
    population), not acreage. Reads typed state the analysis expert emits;
    lenient until the state shape is locked in by review."""
    blob = " ".join(str(s).lower() for s in run.extra.get("structured_outputs", []))
    if not blob:
        return False
    return "impact" in blob or "downwind" in blob or "affected" in blob


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
