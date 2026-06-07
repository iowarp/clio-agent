"""Offline tamper-proofs for the wildfire acceptance matchers.

Proves each structured matcher PASSES genuine results and FAILS the failure
mode it guards — without a live run. Covers both the impact path and the
genuine-null path. Satisfies the Done-criteria requirement that matchers are
proven to catch tampered/broken runs.
"""

from __future__ import annotations

from agent_test import Run, ToolCall

from tests.test_real_cases.test_wildfire_case import (
    computed_overlap_over_real_monitors,
    fused_three_layers,
    grounded_impact_decision,
    region_scoped,
)


def _render_call(layers):
    return ToolCall(name="geospatial_render_feature_map", args={}, output={"status": "success", "layers": layers})


def _impact_run() -> Run:
    return Run(
        output="brief",
        steps=[["data", "analysis", "visualization", "synthesis"]],
        tool_calls=[_render_call([
            {"name": "Smoke forecast", "features": 25},
            {"name": "Fire perimeter", "features": 66},
            {"name": "Air quality", "features": 6},
        ])],
        extra={"workflow_state": {
            "region": [-106.3, 32.6, -104.3, 34.7],
            "fire": {"selected": {"name": "SEVEN CABINS"}},
            "impact": {"present": True, "selected_fire": {"name": "SEVEN CABINS"}},
            "impact_overlap": {"monitors_total": 6, "monitors_under_smoke": 5},
        }},
    )


def _null_run() -> Run:
    """A genuine null: monitors were evaluated, none under smoke."""
    return Run(
        output="no impact",
        steps=[["data", "analysis", "visualization", "synthesis"]],
        tool_calls=[_render_call([{"name": "Fire perimeter", "features": 39}, {"name": "Air quality", "features": 2}])],
        extra={"workflow_state": {
            "region": [-88.9, 44.4, -86.9, 46.4],
            "fire": {"selected": {"name": "North Branch"}},
            "impact": {"present": False, "affected_communities": []},
            "impact_overlap": {"monitors_total": 2, "monitors_under_smoke": 0},
        }},
    )


def test_matchers_pass_impact_run():
    r = _impact_run()
    assert region_scoped(r)
    assert computed_overlap_over_real_monitors(r)
    assert grounded_impact_decision(r)
    assert fused_three_layers(r)


def test_matchers_pass_genuine_null_run():
    r = _null_run()
    assert region_scoped(r)
    assert computed_overlap_over_real_monitors(r)
    assert grounded_impact_decision(r)  # genuine null is a correct decision


def test_region_scoped_catches_bad_region():
    r = _impact_run(); r.extra["workflow_state"]["region"] = None
    assert not region_scoped(r)
    r2 = _impact_run(); r2.extra["workflow_state"]["region"] = ["{{x}}", 1, 2, 3]
    assert not region_scoped(r2)


def test_computed_overlap_catches_hollow_null():
    # overlap "ran" but over zero monitors (smoke/air never acquired) -> reject
    r = _impact_run(); r.extra["workflow_state"]["impact_overlap"] = {"monitors_total": 0, "monitors_under_smoke": 0}
    assert not computed_overlap_over_real_monitors(r)
    r2 = _impact_run(); r2.extra["workflow_state"]["impact_overlap"] = {}
    assert not computed_overlap_over_real_monitors(r2)


def test_grounded_decision_catches_unfounded_claims():
    r = _impact_run(); r.extra["workflow_state"]["impact"] = {"present": True, "selected_fire": None}
    r.extra["workflow_state"]["fire"] = {}
    assert not grounded_impact_decision(r)  # impact claimed but no fire named
    r2 = _impact_run(); r2.extra["workflow_state"]["impact"] = {}
    assert not grounded_impact_decision(r2)  # no decision emitted
    r3 = _null_run(); r3.extra["workflow_state"]["impact_overlap"] = {"monitors_total": 0, "monitors_under_smoke": 0}
    assert not grounded_impact_decision(r3)  # null claimed but nothing evaluated


def test_fused_three_layers_catches_partial_map():
    r = _impact_run(); r.tool_calls[0].output["layers"] = [{"name": "Fire", "features": 6}]
    assert not fused_three_layers(r)
