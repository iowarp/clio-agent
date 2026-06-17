# Case 02 Mutation Plan

The benchmark must not pass because the agent memorized San Diego, P475, or a
previously observed NDP resource URL. The same EarthScope GNSS blueprint must
work from typed geospatial state and live tool evidence when the request changes.

## Registered Runner Cases

- `marketplace_earthscope_gnss_region_review`
  - Geography form: place name.
  - Prompt target: San Diego area.
  - Status: passed once with live ALCF Sophia/NDP evidence.
- `marketplace_earthscope_gnss_region_los_angeles_mutation`
  - Geography form: different place/region.
  - Prompt target: Los Angeles basin.
  - Status: passed once with live ALCF Sophia/NDP evidence after typed-state
    merge hardening.
  - Evidence:
    `trace-sophia-los-angeles-mutation-state-merge.jsonl`,
    `report-sophia-los-angeles-mutation-state-merge.md`.
  - Observed selected station/resource:
    `WWMT.CI` / `WWMT.CI.LY_.40.csv`.
  - Required proof: explicit Los Angeles geospatial state, live NDP discovery,
    station/resource selection from current evidence, staged CSV if available,
    profile/PNG if analysis-ready data exists.
- `marketplace_earthscope_gnss_region_coordinate_mutation`
  - Geography form: explicit coordinate/radius.
  - Prompt target: `34.05 N, 118.25 W`, `75 km`.
  - Status: passed once with live ALCF Sophia/NDP evidence after
    missing-path typed-state hardening, then passed again after removing
    fabricated station CSV URL hints from the station-catalog helper.
  - Evidence:
    `trace-sophia-coordinate-mutation-5790f15.jsonl`,
    `report-sophia-coordinate-mutation-5790f15.md`.
  - Observed selected station/resource:
    `MTA1` / `MTA1.CI.LY_.30.csv`.
  - Required proof: geospatial expert preserves coordinate input, downstream
    discovery consumes typed coordinates/radius, and the workflow either stages
    analysis-ready data or returns a structured acquisition blocker.

## Failure Signals

The mutation runs fail if any trace:

- uses `San Diego`, `P475`, or `P475.CI.LY_.20.csv` as a routing key when the
  user requested a different region;
- stages a station-specific CSV whose station is absent from the current
  filtered station metadata, or whose station distance is outside the requested
  coordinate/radius target;
- performs NDP/EarthScope discovery before explicit geospatial resolution;
- stages a metadata/index CSV and marks it as analysis-ready GNSS time-series
  evidence;
- invents station CSV URLs, local artifact paths, row counts, plots, or
  displacement values;
- treats station metadata as a concrete station time-series resource without a
  current NDP search or dataset/resource tool result;
- returns orchestration prose instead of a final synthesis when child evidence
  is complete.

The r50 Los Angeles strict-depth trace is the current canonical negative
example: the route looked close to correct and reached plotting, but artifact
review found off-region `YUHG.CI.LY_.20.csv` staging from a broad
EarthScope/GNSS/CSV coordinate search. r51 is the corrected positive trace:
the same coordinate/radius target staged `MTA1.CI.LY_.30.csv`, verified `MTA1`
inside the requested radius, profiled the staged CSV, plotted the staged CSV,
and synthesized from that evidence.

## Topology Evaluation

Run the same mutation prompts through three pack variants before finalizing the
EarthScope topology:

- Depth: `earthscope-gnss-region-depth`
  - Shape: `main -> geospatial -> ndp_dataset_discovery -> earthscope_station_catalog -> ndp_resource_resolver -> gnss_timeseries_analysis -> station_network_analysis -> visualization -> synthesis`.
  - Optional event-context capability may be called only when the parent
    semantically requests event catalog evidence; it is not part of the default
    NDP/GNSS CSV chain.
  - Status: structurally registered and validated; real-provider attempts
    rejected for now. The first live trace began correctly with nested
    geography, discovery, station catalog, and resolver handoffs, but then
    cycled through repeated search/stage/filter acquisition work for about
    fourteen minutes and did not reach analysis or visualization before being
    stopped. A later retry with runtime repeat guards avoided the repeated
    child handoff, but still stopped at metadata-only station catalog evidence
    instead of resolving and staging a station-specific CSV.
  - Rejected evidence:
    `trace-sophia-depth-topology-turn-guard-2e48e55.jsonl`,
    `report-sophia-depth-topology-turn-guard-2e48e55.md`.
  - Later rejected evidence:
    `trace-sophia-depth-topology-resource-discovery-2e48e55-r2.jsonl`,
    `report-sophia-depth-topology-resource-discovery-2e48e55-r2.md`.
    This run staged and filtered station metadata and found live station CSV
    candidate URLs, but did not stage a station CSV, profile it, plot it, or
    synthesize. Candidate URLs must be treated as `candidate_found`, not
    `analysis_ready`, until `ndp_stage_resource` returns a local path.
- Width: `earthscope-gnss-region-width`
  - Shape: `main` calls geospatial, dataset discovery, station catalog,
    resource resolver, optional event context, GNSS profile, station network,
    visualization, and synthesis as direct children.
  - Status: accepted once with live ALCF Sophia/NDP evidence for the
    coordinate/radius mutation.
  - Evidence:
    `trace-sophia-width-topology-2e48e55.jsonl`,
    `report-sophia-width-topology-2e48e55.md`.
  - Observed selected station/resource:
    `MTA1` / `MTA1.CI.LY_.30.csv`.
  - Caveats: one run only; child session logs remain absent; keep reviewing
    semantic handoff events, tool evidence, artifacts, and synthesis quality.
- Domain grouping: `earthscope-gnss-region`
  - Shape: `main -> data/analysis/visualization/synthesis`, with geospatial
    retained as an explicit root child before data acquisition.
  - Status: accepted once for the latest coordinate/radius trace and earlier
    San Diego/Los Angeles runs, but still requires repeated real-provider
    mutation review before benchmark-final status.

The preferred production target is domain grouping only if the trace still
exposes geospatial resolution, acquisition, analysis, visualization, and
synthesis as inspectable evidence boundaries.

Topology validation is not a benchmark pass. The width and depth packs must run
against the same mutable geography/resource prompts with live NDP/EarthScope
tool evidence, and each run needs trace review before it counts.

## Trace Review Requirement

The mutation cases are not finished when pytest says the harness accepted a
route. Each accepted run needs a trace review that checks:

- the geospatial state came from the requested place, region, bbox, or explicit
  coordinates rather than a remembered benchmark city;
- NDP/EarthScope discovery consumed that typed state;
- station/resource identifiers and URLs came from current tool evidence;
- `acquisition.status=staged` and `analysis_ready=true` include a local staged
  path that exists on disk;
- analysis and visualization operate on the staged file, not on stale local
  files or prose-only resource names;
- final synthesis responds to the user instead of leaving orchestration
  instructions as the final answer.

Trace review must inspect failures as carefully as passes. The
`f56f641` coordinate run produced a useful blocker because it revealed guessed
station CSV URLs such as `.00` suffixes. That was not a model-only prompt
problem; it was bad helper semantics. The corrected `5790f15` run performed
live station-resource searches, staged `MTA1.CI.LY_.30.csv`, profiled it,
generated `MTA1_CI_LY_30_timeseries.png`, and synthesized from that evidence.
The width-topology `2e48e55` run repeated the same coordinate mutation through
a broader expert layout and produced live data, analysis, visualization, and
synthesis. The depth-topology attempts are the opposite kind of evidence: they
showed that a structurally valid chain can still be operationally poor. First it
looped through acquisition cycles; after repeat guards, it exited too early with
metadata-only acquisition. The remaining fix must teach the chain to distinguish
`metadata_found` from `station_csv_search_required` from `analysis_ready`, and
to continue resolver work when station search terms exist. The latest depth
retry reached station CSV candidate discovery, so the next invariant is stricter:
`analysis_ready=true` requires `acquisition.status=staged` and a local staged
CSV path that exists; station CSV URLs alone are not sufficient.

Latest trace-review invariant from r50/r51: tests may encode the off-region
staging guard after the fact, but the benchmark decision comes from reading the
trace and inspecting artifacts. A run is not accepted until the selected
station/resource can be traced from the requested geography through NDP
discovery, staging, analysis, visualization, and final synthesis.
