"""Offline tamper-proofs for the wildfire acceptance matchers.

Proves each structured matcher PASSES a genuine result and FAILS the specific
failure mode it guards — without a live run. Satisfies the Done-criteria
requirement that matchers are proven to catch tampered/broken runs.
"""

from __future__ import annotations

import copy

from agent_test import Run, ToolCall

from tests.test_real_cases.test_wildfire_case import (
    found_real_impact,
    fused_three_layers,
    region_scoped,
)


def _genuine_run() -> Run:
    return Run(
        output="brief",
        steps=[["data", "analysis", "visualization", "synthesis"]],
        tool_calls=[
            ToolCall(name="ndp_query_arcgis_features", args={}, output={"feature_count": 66}),
            ToolCall(name="geospatial_render_feature_map", args={}, output={
                "status": "success",
                "layers": [
                    {"name": "Smoke forecast", "features": 25},
                    {"name": "Fire perimeter", "features": 66},
                    {"name": "Air quality", "features": 6},
                ],
            }),
        ],
        extra={"workflow_state": {
            "region": [-85.8, 30.2, -83.8, 32.2],
            "impact": {"present": True, "selected_fire": {"name": "Pineland Road"}},
        }},
    )


def test_matchers_pass_a_genuine_run():
    run = _genuine_run()
    assert region_scoped(run)
    assert found_real_impact(run)
    assert fused_three_layers(run)


def test_region_scoped_catches_missing_or_bad_region():
    r = _genuine_run(); r.extra["workflow_state"]["region"] = None
    assert not region_scoped(r)
    r2 = _genuine_run(); r2.extra["workflow_state"]["region"] = [-85.8, 30.2]  # wrong length
    assert not region_scoped(r2)
    r3 = _genuine_run(); r3.extra["workflow_state"]["region"] = ["{{x}}", 1, 2, 3]  # template junk
    assert not region_scoped(r3)


def test_found_real_impact_catches_hollow_null():
    r = _genuine_run(); r.extra["workflow_state"]["impact"] = {"present": False}
    assert not found_real_impact(r)
    r2 = _genuine_run(); r2.extra["workflow_state"]["impact"] = {"present": True, "selected_fire": None}
    assert not found_real_impact(r2)


def test_fused_three_layers_catches_partial_map():
    r = _genuine_run()
    r.tool_calls[1].output["layers"] = [{"name": "Air quality", "features": 6}]  # one layer
    assert not fused_three_layers(r)
    r2 = _genuine_run()
    for layer in r2.tool_calls[1].output["layers"]:
        layer["features"] = 0  # rendered but empty
    assert not fused_three_layers(r2)
