"""EarthScope GNSS region — real case acceptance test.

The real EarthScope case test (supersedes the EarthScope-specific
``scripts/run_demo_benchmark.py`` lane): a live CLIO session through the
``earthscope-gnss-region`` blueprint must resolve the geography, acquire real
EarthScope/NDP GNSS station CSV evidence, analyze it, render a PNG, and
synthesize — judged on the *structured* trace, not on the model's prose.

Provider/model are injected, not in the test. As a blast-radius guardrail the
matrix is restricted to one provider (Metis) and at most 2 models; normal
operation is gpt-oss-120b. Pin a cell with ``--provider/--model``; the active
cell is recorded in run.extra.

Run live:
  ``CLIO_RUN_LIVE=1 pytest tests/test_real_cases/test_earthscope_case.py \
      -o addopts="" --provider argonne_metis``

The matchers below guard the data pathway with structured checks (on-region
station from the filter result, real non-empty PNG, full acquisition pipeline).
Judging whether the synthesis is *honest* about the evidence is the agent's
trace-review job; what review finds gets frozen as new matchers here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest
from agent_test import matcher

CASE_DIR = "benchmark/case02-earthscope-csv-seismic-geography"
PROMPT = Path(CASE_DIR, "prompt.txt").read_text().strip()


@dataclass(frozen=True)
class GrindCell:
    """One committed acceptance cell of the EarthScope grind.

    ``region`` swaps the prompt's default "San Diego" geography ("" keeps it);
    ``expect`` flips the acceptance: a positive cell must run the full pipeline
    and plot a real in-region station, a negative cell must honestly find no
    coverage and NOT fabricate a station or PNG.
    """

    label: str
    region: str
    expect: str


# The committed acceptance matrix (was the /tmp grind shell's loop + env): five
# San Diego positives for repeatability, one Seattle alt-positive, one Chicago
# negative. Selectable per cell with ``pytest -k <label>``.
EARTHSCOPE_CELLS: tuple[GrindCell, ...] = (
    *(GrindCell(f"sandiego_{i}", "", "positive") for i in range(1, 6)),
    GrindCell("seattle_alt", "Seattle", "positive"),
    GrindCell("chicago_neg", "Chicago", "negative"),
)


def _tool_result(run, name):
    for call in run.tool_calls:
        if call.name == name and isinstance(call.output, dict):
            return call.output
    return None


def _staged_station_id(run) -> str:
    """The profiled/plotted station CSV encodes the station id, e.g.
    ``P475.CI.LY_.20.csv`` -> ``P475`` (ignores the metadata catalog CSV)."""
    for call in run.tool_calls:
        if call.name in ("plot_plot_timeseries", "pandas_profile_csv"):
            base = Path(str((call.args or {}).get("data_path", ""))).name
            if base and not base.startswith("earthscope_converted"):
                return base.split(".")[0]
    return ""


@matcher
def ran_acquisition_to_plot_pipeline(run):
    """The real pipeline ran: spatial filter -> stage -> profile -> plot."""
    n = run.tool_names
    return (
        "geo_filter_points_by_radius" in n
        and "ndp_stage_resource" in n
        and "pandas_profile_csv" in n
        and any("plot" in x for x in n)
    )


@matcher
def staged_station_on_region(run):
    """Structured on-region check: the station whose CSV was staged/plotted must
    appear in the filter result within the requested radius. This is what would
    catch an off-region staging (the r50 failure), and it reads structured tool
    output, not synthesis prose.

    A large in-radius result (e.g. 27 stations, seattle_alt) can blow the
    model-tool-result size cap: ``geo_filter_points_by_radius``'s recorded
    output is then a typed truncation envelope (``_clio.status=="truncated"``)
    carrying only ``head``/``tail`` preserved-text excerpts instead of the full
    ``points`` list. That case is verified from the preserved text via regex
    instead of the dict lookup below (nearest-first ordering means the staged
    station's own row is almost always in ``head``); the full-dict path is
    unchanged."""
    fr = _tool_result(run, "geo_filter_points_by_radius")
    if not fr:
        return False
    sid = _staged_station_id(run)
    if not sid:
        return False
    clio_meta = fr.get("_clio")
    if isinstance(clio_meta, dict) and str(clio_meta.get("status") or "") == "truncated":
        text = str(fr.get("head") or "") + str(fr.get("tail") or "")
        radius_match = re.search(r"radius_km=([0-9.]+)", text)
        distance_match = re.search(
            r"'distance_km': ([0-9.]+), 'id': '" + re.escape(sid) + r"'", text
        )
        if not radius_match or not distance_match:
            # Honest fail: the sid's row fell in the truncated middle (neither
            # head nor tail preserved it), so it cannot be verified on-region.
            return False
        return float(distance_match.group(1)) <= float(radius_match.group(1))
    radius = float(fr.get("radius_km") or 0)
    distances = {str(p.get("id")): p.get("distance_km") for p in fr.get("points", [])}
    if sid not in distances or distances[sid] is None:
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
@pytest.mark.parametrize("cell", EARTHSCOPE_CELLS, ids=[c.label for c in EARTHSCOPE_CELLS])
def test_earthscope_gnss_region(agent, gact_server, cell, tmp_path):
    prompt = PROMPT.replace("San Diego", cell.region) if cell.region else PROMPT
    run = agent.run(
        {
            "task": prompt,
            "blueprint_id": "earthscope-gnss-region",
            "case_dir": CASE_DIR,
            "run_label": cell.label,
            # Isolated, auto-cleaned workspace root: the agent writes the staged CSV
            # and the rendered PNG here, NOT into the repo (see clio_sut.invoke).
            "workdir": str(tmp_path),
            # Normalized run trace lands next to the full semantic trace in the
            # fixture's durable per-cell dir (inspectable later, not wiped /tmp).
            "trace_path": str(gact_server.trace_dir / f"{cell.label}.run.jsonl"),
            # No absolute per-run wall clock: an agentic run may take as long as it
            # keeps ADVANCING. The SUT's no-progress watchdog (no_progress_s) and
            # per-call timeouts bound genuine stalls; a slow but progressing model
            # (e.g. a 120B reasoning model over the full pipeline) must not be killed
            # by a fixed ceiling. timeout_s=0 turns the hard cap OFF.
            "timeout_s": 0,
        }
    )

    # Runtime/harness invariants.
    assert run.error is None, run.error
    assert run.extra["blueprint_activated"], run.extra.get("active_agent_blueprint_id")

    if cell.expect == "negative":
        # No EarthScope GNSS coverage in the requested region: the agent must
        # resolve the geography, find no in-region stations, and STOP honestly --
        # never fabricate a distant station or a plot. So: no station was
        # staged/profiled/plotted, and no PNG was produced.
        assert not _staged_station_id(run), (
            f"no-coverage region staged a station (fabrication): "
            f"{_staged_station_id(run)}; filter={_tool_result(run, 'geo_filter_points_by_radius')}"
        )
        assert not produced_nonempty_png(run), (
            f"no-coverage region fabricated a PNG: {run.extra.get('artifacts')}"
        )
        return

    # Route: geospatial -> data -> analysis -> visualization, with the main
    # itself authoring the final answer (no separate "synthesis" child exists in
    # this pack -- earthscope-gnss-region/experts/main.md: "there is no separate
    # final-responder child"). A positive cell must reach visualization: the
    # PNG matchers below require its output.
    assert run.routed_to("data"), run.steps
    assert run.routed_to("visualization"), run.steps

    # Data pathway: the full real pipeline ran (not just "some tool fired").
    assert ran_acquisition_to_plot_pipeline(run), run.tool_names

    # Provenance: the staged station is genuinely within the requested radius.
    assert staged_station_on_region(run), (
        f"staged station off-region or unverifiable; "
        f"station={_staged_station_id(run)}, filter={_tool_result(run, 'geo_filter_points_by_radius')}"
    )

    # Real deliverable: a non-empty PNG on disk.
    assert produced_nonempty_png(run), run.extra.get("artifacts")

    # Hygiene: the rendered PNG lands inside the isolated workdir, never the
    # repo. This makes the mandatory-workdir guarantee observable.
    pngs = [p for p in run.extra.get("artifacts", []) if p.endswith(".png")]
    assert pngs, run.extra.get("artifacts")
    for p in pngs:
        assert Path(p).resolve().is_relative_to(tmp_path.resolve()), (
            f"PNG {p!r} written outside the isolated workdir {tmp_path}"
        )
