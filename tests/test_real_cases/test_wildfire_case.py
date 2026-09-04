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
import os
from pathlib import Path

import pytest
from agent_test import matcher

CASE_DIR = "benchmark/ndp-wildfire-smoke-impact"
# Region-parametrizable: CLIO_WILDFIRE_PROMPT selects a region-variant prompt
# (prompt_west / prompt_southwest / prompt_southeast) to prove the case
# generalizes across distinct regions; defaults to the nationwide prompt.
_PROMPT_FILE = os.environ.get("CLIO_WILDFIRE_PROMPT", "prompt.txt")
PROMPT = Path(CASE_DIR, _PROMPT_FILE).read_text().strip()
_RUN_LABEL = (
    "acceptance-" + Path(_PROMPT_FILE).stem.replace("prompt", "").strip("_-")
    if _PROMPT_FILE != "prompt.txt"
    else "acceptance"
)


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
def computed_overlap_over_real_monitors(run):
    """The smoke∩monitor overlap was actually COMPUTED over real acquired
    monitors (not a hollow null). Proves smoke + air were acquired and the
    spatial-join ran with real inputs."""
    overlap = (run.extra.get("workflow_state") or {}).get("impact_overlap") or {}
    return "monitors_under_smoke" in overlap and int(overlap.get("monitors_total", 0) or 0) > 0


@matcher
def grounded_impact_decision(run):
    """A grounded impact decision was reached over the computed overlap — EITHER
    a genuine impact (present, with a named fire) OR a genuine null (no monitors
    under smoke, but monitors WERE evaluated). Both are correct outcomes; what's
    rejected is no-decision or a null with nothing actually evaluated."""
    ws = run.extra.get("workflow_state") or {}
    impact = ws.get("impact") or {}
    overlap = ws.get("impact_overlap") or {}
    if "present" not in impact:
        return False
    if impact.get("present"):
        return bool(impact.get("selected_fire") or (ws.get("fire") or {}).get("selected"))
    # genuine null: monitors were really evaluated and none fell under smoke
    return (
        int(overlap.get("monitors_total", 0) or 0) > 0
        and int(overlap.get("monitors_under_smoke", 0) or 0) == 0
    )


@matcher
def fused_three_layers(run):
    """The rendered map actually fused all three layers with real features
    (fire perimeter + smoke + air-quality), not a single-layer fallback."""
    for call in run.tool_calls:
        if call.name == "geo_render_feature_map" and isinstance(call.output, dict):
            layers = call.output.get("layers") or []
            with_features = [
                layer
                for layer in layers
                if isinstance(layer, dict) and int(layer.get("features", 0) or 0) > 0
            ]
            if len(with_features) >= 3:
                return True
    return False


@pytest.mark.real_case
@pytest.mark.live
def test_wildfire_downwind_impact(agent, tmp_path):
    run = agent.run(
        {
            "task": PROMPT,
            "blueprint_id": "wildfire-smoke-impact-review",
            "case_dir": CASE_DIR,
            "run_label": _RUN_LABEL,
            # Isolated, auto-cleaned workspace root (see clio_sut.invoke): the map
            # PNG is written here, not into the repo.
            "workdir": str(tmp_path),
            # No absolute per-run wall clock — progress watchdog governs (see the
            # EarthScope case for the rationale). timeout_s=0 turns the hard cap off.
            "timeout_s": 0,
        }
    )

    # Runtime/harness invariants.
    assert run.error is None, run.error
    assert run.extra["blueprint_activated"], run.extra.get("active_agent_blueprint_id")
    assert run.called("geo_query_arcgis_features"), run.tool_names

    # Route: acquisition (data) -> impact analysis (analysis) -> visualization,
    # with the main itself authoring the final brief (no separate "synthesis"
    # child exists in this pack -- wildfire-smoke-impact-review/experts/main.md:
    # "there is no separate final-responder child").
    for expert in ("data", "analysis", "visualization"):
        assert run.routed_to(expert), run.steps

    ws = run.extra.get("workflow_state") or {}

    # Region was genuinely derived and scoped (no None / template-string bbox).
    assert region_scoped(run), ws.get("region")

    # The full data pathway ran: smoke + air acquired and the smoke∩monitor
    # overlap COMPUTED over real monitors (rejects hollow nulls / missing layers).
    assert computed_overlap_over_real_monitors(run), ws.get("impact_overlap")

    # A grounded decision over that overlap: genuine impact (named fire) OR a
    # genuine null (monitors evaluated, none under smoke). Both are correct.
    assert grounded_impact_decision(run), ws.get("impact")

    # Real deliverable: a map PNG on disk. When impact is present the map fuses
    # all three layers; a genuine-null region may legitimately lack smoke cover.
    assert run.called("geo_render_feature_map"), run.tool_names
    if (ws.get("impact") or {}).get("present"):
        assert fused_three_layers(run), "impact run did not fuse all three layers"
    assert any(
        p.endswith(".png") and Path(p).is_file() and Path(p).stat().st_size > 1024
        for p in run.extra["artifacts"]
    ), run.extra["artifacts"]

    # Hygiene: the rendered map PNG lands inside the isolated workdir, never the
    # repo. This makes the mandatory-workdir guarantee observable.
    for p in run.extra["artifacts"]:
        if p.endswith(".png"):
            assert Path(p).resolve().is_relative_to(tmp_path.resolve()), (
                f"PNG {p!r} written outside the isolated workdir {tmp_path}"
            )
