"""Wildfire downwind smoke-impact — real case acceptance test (grind target).

Encodes the contract from ``benchmark/ndp-wildfire-smoke-impact/GOAL.md``.
Expected to FAIL until the case is iterated to productive; every failure that a
trace review (the agent's autonomous job, no human in the loop) explains gets
frozen here as a tighter matcher. This is the IMPACT case — it requires a
genuine downwind-impact result. The honest no-impact path is a separate test.

Run live: ``CLIO_RUN_LIVE=1 pytest tests/test_real_cases/test_wildfire_case.py
--provider argonne_metis``.
"""

from __future__ import annotations

import numbers
from pathlib import Path

import pytest

from agent_test import matcher

CASE_DIR = "benchmark/ndp-wildfire-smoke-impact"
PROMPT = Path(CASE_DIR, "prompt.txt").read_text().strip()


@matcher
def region_scoped(run):
    """A real numeric region bbox was derived (not None, not a template string)."""
    region = (run.extra.get("workflow_state") or {}).get("region")
    if not isinstance(region, (list, tuple)) or len(region) != 4:
        return False
    return all(isinstance(v, numbers.Number) and not isinstance(v, bool) for v in region)


@matcher
def found_real_impact(run):
    """Genuine impact: a typed impact decision that is present AND a real fire is
    named — either on the impact object or the grounded `fire.selected` (derived
    from the live fire query). Read from typed workflow_state, not prose."""
    ws = run.extra.get("workflow_state") or {}
    impact = ws.get("impact") or {}
    named_fire = impact.get("selected_fire") or (ws.get("fire") or {}).get("selected")
    return bool(impact.get("present")) and bool(named_fire)


@matcher
def fused_three_layers(run):
    """The rendered map actually fused all three layers with real features
    (fire perimeter + smoke + air-quality), not a single-layer fallback."""
    for call in run.tool_calls:
        if call.name == "geospatial_render_feature_map" and isinstance(call.output, dict):
            layers = call.output.get("layers") or []
            with_features = [
                layer for layer in layers
                if isinstance(layer, dict) and int(layer.get("features", 0) or 0) > 0
            ]
            if len(with_features) >= 3:
                return True
    return False


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
    assert run.called("ndp_query_arcgis_features"), run.tool_names

    # Route: acquisition -> impact analysis -> visualization -> synthesis.
    for expert in ("data", "analysis", "visualization", "synthesis"):
        assert run.routed_to(expert), run.steps

    # Region was genuinely derived and scoped (no None / template-string bbox).
    assert region_scoped(run), (run.extra.get("workflow_state") or {}).get("region")

    # Genuine downwind impact with a selected fire (not a hollow null).
    assert found_real_impact(run), (run.extra.get("workflow_state") or {}).get("impact")

    # Real deliverable: a non-empty 3-layer map PNG on disk.
    assert run.called("geospatial_render_feature_map"), run.tool_names
    assert fused_three_layers(run), "map did not fuse all three layers with features"
    assert any(
        p.endswith(".png") and Path(p).is_file() and Path(p).stat().st_size > 1024
        for p in run.extra["artifacts"]
    ), run.extra["artifacts"]
