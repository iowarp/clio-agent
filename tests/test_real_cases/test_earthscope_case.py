"""EarthScope GNSS region — real case acceptance test.

The real EarthScope case test (supersedes the EarthScope-specific
``scripts/run_demo_benchmark.py`` lane): a live CLIO session through the
``earthscope-gnss-region`` blueprint must resolve the geography, acquire real
EarthScope/NDP GNSS station CSV evidence, analyze it, render a PNG, and
synthesize — judged on the *structured* trace, not on the model's prose.

Provider/model are injected, not in the test: the SUT discovers cells from the
live provider registry (model = the provider's server-side default_model).
Pin one with ``--provider``; the active cell is recorded in run.extra.

Run live:
  ``CLIO_RUN_LIVE=1 pytest tests/test_real_cases/test_earthscope_case.py \
      -o addopts="" --provider argonne_sophia``

The matchers below guard the data pathway with structured checks (on-region
station from the filter result, real non-empty PNG, full acquisition pipeline).
Judging whether the synthesis is *honest* about the evidence is the agent's
trace-review job; what review finds gets frozen as new matchers here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_test import matcher

CASE_DIR = "benchmark/case02-earthscope-csv-seismic-geography"
PROMPT = Path(CASE_DIR, "prompt.txt").read_text().strip()


def _tool_result(run, name):
    for call in run.tool_calls:
        if call.name == name and isinstance(call.output, dict):
            return call.output
    return None


def _staged_station_id(run) -> str:
    """The profiled/plotted station CSV encodes the station id, e.g.
    ``P475.CI.LY_.20.csv`` -> ``P475`` (ignores the metadata catalog CSV)."""
    for call in run.tool_calls:
        if call.name in ("ndp_plot_csv_timeseries", "ndp_profile_csv_resource"):
            base = Path(str((call.args or {}).get("filepath", ""))).name
            if base and not base.startswith("earthscope_converted"):
                return base.split(".")[0]
    return ""


@matcher
def ran_acquisition_to_plot_pipeline(run):
    """The real pipeline ran: spatial filter -> stage -> profile -> plot."""
    n = run.tool_names
    return (
        "ndp_filter_earthscope_station_catalog" in n
        and "ndp_stage_resource" in n
        and "ndp_profile_csv_resource" in n
        and any("plot" in x for x in n)
    )


@matcher
def staged_station_on_region(run):
    """Structured on-region check: the station whose CSV was staged/plotted must
    appear in the filter result within the requested radius. This is what would
    catch an off-region staging (the r50 failure), and it reads structured tool
    output, not synthesis prose."""
    fr = _tool_result(run, "ndp_filter_earthscope_station_catalog")
    if not fr:
        return False
    radius = float(fr.get("radius_km") or 0)
    distances = {str(s.get("station")): s.get("distance_km") for s in fr.get("stations", [])}
    sid = _staged_station_id(run)
    if not sid or sid not in distances or distances[sid] is None:
        return False
    return float(distances[sid]) <= radius


@matcher
def produced_nonempty_png(run):
    """A real (non-trivial) PNG was rendered and exists on disk."""
    return any(
        p.endswith(".png") and Path(p).is_file() and Path(p).stat().st_size > 1024
        for p in run.extra.get("artifacts", [])
    )


@pytest.mark.real_case
@pytest.mark.live
def test_earthscope_gnss_region(agent):
    run = agent.run({
        "task": PROMPT,
        "blueprint_id": "earthscope-gnss-region",
        "case_dir": CASE_DIR,
        "run_label": "acceptance",
        "timeout_s": 600,
    })

    # Runtime/harness invariants.
    assert run.error is None, run.error
    assert run.extra["blueprint_activated"], run.extra.get("active_agent_blueprint_id")

    # Route: geography/acquisition -> analysis -> visualization -> synthesis.
    assert run.routed_to("data"), run.steps
    assert run.routed_to("synthesis"), run.steps

    # Data pathway: the full real pipeline ran (not just "some tool fired").
    assert ran_acquisition_to_plot_pipeline(run), run.tool_names

    # Provenance: the staged station is genuinely within the requested radius.
    assert staged_station_on_region(run), (
        f"staged station off-region or unverifiable; "
        f"station={_staged_station_id(run)}, filter={_tool_result(run, 'ndp_filter_earthscope_station_catalog')}"
    )

    # Real deliverable: a non-empty PNG on disk.
    assert produced_nonempty_png(run), run.extra.get("artifacts")
