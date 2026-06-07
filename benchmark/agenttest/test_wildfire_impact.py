"""Wildfire downwind smoke-impact — acceptance test (the grind target).

These assertions encode the case contract from
``benchmark/ndp-wildfire-smoke-impact/GOAL.md``. They are expected to FAIL until
the case is iterated to productive; each failure that a trace review explains
becomes a tightened matcher here. Run live with `-m live`.

Division of labor: the matchers below guard the data pathways (tools, route,
artifact); reading the trace in `runs/` to judge semantics is the human/agent
job, and what review discovers gets frozen here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_test import matcher

CASE_DIR = "benchmark/ndp-wildfire-smoke-impact"
PROMPT = Path(CASE_DIR, "prompt.txt").read_text().strip()


@matcher
def selected_by_impact_not_size(run):
    """Specific semantics: the chosen fire is justified by downwind impact
    (smoke over monitored population), not by acreage. Reads typed state the
    analysis expert emits; lenient until the state shape is locked in review."""
    blob = " ".join(str(s).lower() for s in run.extra.get("structured_outputs", []))
    if not blob:
        return False
    return ("impact" in blob or "downwind" in blob or "affected" in blob) and "acres" not in blob.split("selected")[-1][:200]


@matcher
def no_forced_routing(run):
    """Specific semantics: experts were reached by reasoning, not a string
    contract. A real run should not carry legacy text-routing markers."""
    return "allow_text_routing" not in " ".join(run.tool_names).lower()


@pytest.mark.live
def test_wildfire_downwind_impact(agent):
    run = agent.run({
        "task": PROMPT,
        "blueprint_id": "wildfire-smoke-impact-review",
        "case_dir": CASE_DIR,
        "run_label": "acceptance",
        "timeout_s": 600,
    })

    # --- harness/runtime invariants ---
    assert run.error is None, run.error
    assert run.extra["blueprint_activated"], run.extra.get("active_agent_blueprint_id")

    # --- data pathways: the three live sources were queried ---
    assert run.called("ndp_query_arcgis_features"), run.tool_names

    # --- route: acquisition -> impact analysis -> visualization -> synthesis ---
    assert run.routed_to("data"), run.steps
    assert run.routed_to("analysis"), run.steps
    assert run.routed_to("visualization"), run.steps
    assert run.routed_to("synthesis"), run.steps

    # --- artifact: a real map PNG was rendered and exists on disk ---
    assert run.called("geospatial_render_feature_map"), run.tool_names
    assert any(p.endswith(".png") for p in run.extra["artifacts"]), run.extra["artifacts"]

    # --- semantics frozen from review ---
    assert no_forced_routing(run)
    assert selected_by_impact_not_size(run)
