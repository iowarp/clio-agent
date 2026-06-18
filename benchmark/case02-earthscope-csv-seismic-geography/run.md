# Case 02 Run Record

Status: San Diego acceptance run passed; Los Angeles and coordinate mutations
have passed with live data/acquisition/analysis/visualization evidence; width
topology passed once; strict depth topology now has a latest reviewed r51
full-chain pass with live staging/profile/visualization/synthesis evidence. It
is not benchmark-final. Bay Area mutation runs and the r50 Los Angeles trace
prove the case can fail semantically even when the route looks plausible: the
agent may surface a clean final story while staging an off-region station
resource or hiding acquisition/tool instability. The current blocker is no
longer "can CLIO ever acquire/analyze/visualize real EarthScope CSV data"; it is
"can CLIO do so consistently under mutable geography/resource inputs, with
trace-auditable typed state, no off-region artifacts, and synthesis that matches
the actual evidence."

Date: 2026-06-05

Provider:

- Provider: ALCF Sophia
- Provider id: `argonne`
- API base: `https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1`
- Model: `openai/gpt-oss-120b`
- Temperature: `1`
- Max tokens: `32000`

Agent Blueprint:

- Marketplace pack: `earthscope-gnss-region`
- Topology variants registered for comparison:
  - `earthscope-gnss-region` (domain-grouped)
  - `earthscope-gnss-region-width` (root-owned width)
  - `earthscope-gnss-region-depth` (strict depth chain)
- Local source during run: `external/clio-agent-marketplace/earthscope-gnss-region`
- Canonical marketplace status: pack is in the marketplace submodule used by
  this checkout. Always cite the marketplace repository and commit when
  publishing benchmark evidence.
- Latest marketplace commit for the current evidence:
  `2e48e55 Add EarthScope topology variants`.

Command shape:

```bash
uv run python scripts/run_demo_benchmark.py \
  --backend http://127.0.0.1:<port> \
  --marketplace-source external/clio-agent-marketplace \
  --case marketplace_earthscope_gnss_region_review \
  --output benchmark/case02-earthscope-csv-seismic-geography/trace-sophia-final-acceptance.jsonl \
  --report benchmark/case02-earthscope-csv-seismic-geography/report-sophia-final-acceptance.md
```

Canonical evidence files in this folder:

- `prompt.txt`: public prompt for the accepted San Diego run.
- `trace.jsonl`: copied from `trace-sophia-final-acceptance.jsonl`.
- `report.md`: copied from `report-sophia-final-acceptance.md`.

Known caveats:

- The accepted run proves one real San Diego execution path with live NDP/EarthScope data, analysis, visualization, and synthesis.
- The Los Angeles mutation proves the same workflow under changed geography and changed station/resource evidence.
- The coordinate mutation proves the same workflow from explicit coordinates and radius, including live NDP/EarthScope catalog discovery, staged station CSV acquisition, profile analysis, station/network analysis, visualization, and synthesis.
- An earlier coordinate mutation trace exposed a real semantic failure: the model retained a missing local staged path and routed into analysis as if `analysis_ready=true` were trustworthy. The runtime now treats `acquisition.status=staged` plus `analysis_ready=true` as analysis-ready only when the referenced local staged path exists.
- A later coordinate mutation trace exposed a second data-acquisition failure:
  the station-catalog helper was still emitting guessed `.00` raw CSV URLs from
  station identifiers. That made the agent look semantically plausible while
  trying fabricated resources. The NDP tool now returns `resource_discovery`
  search terms instead of candidate raw URLs, and the resource resolver must
  perform live NDP search before staging station CSVs.
- Reports may still list stale requested output paths under input references, but artifact evidence must only count verified files on disk.
- Pytest cases for this folder are guardrails for route/artifact invariants. The benchmark result remains the reviewed JSONL trace, generated report, and artifact inspection.
- Width topology has one accepted real-provider trace. Depth topology now has
  one reviewed full-chain r30 trace. The benchmark acceptance bar remains trace
  and artifact review: unit tests guard typed-state regressions, but they do
  not prove scientific quality or demo readiness.
- The harness now rejects artifact-producing cases when the user-facing answer
  does not cite the produced PNG or when final no-data language contradicts
  staged/analysis-ready typed evidence in the trace. This was added after the
  Bay Area r3 false positive.

Latest strict-depth evidence:

- Rejected semantic trace:
  - `trace-sophia-depth-topology-current-r50.jsonl`
  - `report-sophia-depth-topology-current-r50.md`
  - Result: useful failure. The workflow reached plotting, but trace/artifact
    review found an off-region `YUHG.CI.LY_.20.csv` station resource staged
    from a broad EarthScope/GNSS/CSV coordinate search. This run is the example
    for why harness PASS/FAIL and final-answer plausibility are insufficient.
- Passing trace after trace-driven fixes:
  - `trace-sophia-depth-topology-current-r51.jsonl`
  - `report-sophia-depth-topology-current-r51.md`
  - Prompt target: center `34.05 N, 118.25 W`, radius `75 km`.
  - Reviewed route: `main -> geospatial -> ndp_dataset_discovery ->
    earthscope_station_catalog -> ndp_resource_resolver ->
    seismic_event_catalog -> gnss_timeseries_analysis ->
    station_network_analysis -> visualization -> synthesis`.
  - Selected live station/resource: `MTA1` / `MTA1.CI.LY_.30.csv`.
  - Manual provenance check: `MTA1` appears in the staged
    `earthscope_converted_data.csv` station metadata at latitude `34.05522077`,
    longitude `-118.24550778`, `0.713 km` from the requested center. The
    station CSV contains `time`, `east`, `north`, and `up` columns, and the
    profile/plot tools consumed that exact staged station CSV path.
  - Verified artifacts:
    - `/home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/earthscope_converted_data.csv`
    - `/home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30.csv`
    - `/home/jcernuda/clio-agent/.clio/artifacts/plots/MTA1_CI_LY_30_timeseries.png`
  - Manual review result: PNG is non-empty and final synthesis cites the staged
    CSV/PNG. The trace now preserves local expert summaries separately from
    parent-facing return summaries, so geospatial, discovery, resolver,
    analysis, visualization, and synthesis evidence can be audited separately.

Remaining strict-depth caveats:

- Downstream compact state may still drop some station provenance fields even
  though upstream rows contain them. Decide whether every downstream row must
  carry station id, geographic grounding, and distance.
- r51 covers one successful Los Angeles coordinate/radius case. The same
  trace-review standard still needs mutable geography/resource replays and
  width/domain topology comparison.
- Tests in this area are regression guards for trace-derived bugs. They do not
  replace live trace inspection, artifact inspection, and scientific
  plausibility review.

Earlier strict-depth evidence:

- Passing trace:
  - `trace-sophia-depth-topology-resource-discovery-2e48e55-r32.jsonl`
  - `report-sophia-depth-topology-resource-discovery-2e48e55-r32.md`
- Prompt target: center `34.05 N, 118.25 W`, radius `75 km`.
- Reviewed route: `main -> geospatial -> ndp_dataset_discovery ->
  earthscope_station_catalog -> ndp_resource_resolver ->
  seismic_event_catalog -> gnss_timeseries_analysis ->
  station_network_analysis -> visualization -> synthesis`.
- Tools: `ndp_search_datasets`, `ndp_get_dataset_details`,
  `ndp_stage_resource`, `ndp_filter_earthscope_station_catalog`,
  `ndp_profile_csv_resource`, and `ndp_plot_csv_timeseries`.
- Selected live station/resource: `MTA1` / `MTA1.CI.LY_.30.csv`.
- Manual provenance check: `MTA1` appears in the staged
  `earthscope_converted_data.csv` station metadata at latitude `34.05522077`,
  longitude `-118.24550778`, inside the requested coordinate/radius target.
  The station CSV contains `time`, `east`, `north`, and `up` columns, and the
  profile/plot tools consumed that exact staged station CSV path.
- Verified artifacts:
  - `/home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/earthscope_converted_data.csv`
  - `/home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30.csv`
  - `/home/jcernuda/clio-agent/.clio/artifacts/MTA1_time_series.png`
- Manual review result: the final assistant text part cites the staged station
  CSV and generated PNG and does not claim unsupported no-data status. The
  report and raw trace prove the tool sequence and artifacts, while the compact
  handoff state still drops some dataset/source fields in later summaries. That
  compact-state provenance loss remains a hardening item even though the local
  artifact and metadata inspection grounds this specific run.

Bay Area strict-depth mutation evidence:

- Prompt target: center `37.77 N, 122.42 W`, radius `75 km`.
- Rejected r1:
  - `trace-sophia-depth-topology-bay-area-mutation-r1.jsonl`
  - `report-sophia-depth-topology-bay-area-mutation-r1.md`
  - Root cause: station-specific CSV candidate state was not enough to route to
    resolver staging, so the station catalog treated a time-series CSV like
    station metadata.
- Rejected r2:
  - `trace-sophia-depth-topology-bay-area-mutation-r2.jsonl`
  - `report-sophia-depth-topology-bay-area-mutation-r2.md`
  - Route reached resolver and grounded the metadata-only blocker, but the root
    final answer fell back to stale geospatial JSON.
- Rejected r3 after manual review:
  - `trace-sophia-depth-topology-bay-area-mutation-r3.jsonl`
  - original report `report-sophia-depth-topology-bay-area-mutation-r3.md`
  - tightened re-render `report-sophia-depth-topology-bay-area-mutation-r3-rerendered.md`
  - The run found and staged `WWMT.CI.LY_.40.csv`, profiled it, generated
    `/home/jcernuda/clio-agent/.clio/artifacts/WWMT.CI.LY_.40_timeseries.png`,
    and traversed the full chain through synthesis. It is rejected because the
    final visible answer contradicted that evidence by saying no EarthScope
    GNSS station/time-series was verified.
- Rejected r4 as successful-acquisition demo, accepted as runtime blocker
  evidence:
  - `trace-sophia-depth-topology-bay-area-mutation-r4.jsonl`
  - `report-sophia-depth-topology-bay-area-mutation-r4.md`
  - Runtime bubbling is fixed: the root answer now preserves the downstream
    acquisition blocker instead of stale geospatial prose. The live run did not
    reach profile/plot because NDP search timed out after 30s and subsequent
    NDP MCP calls returned `ClosedResourceError`.
- Rejected r5/r6 after hidden or misleading evidence:
  - `trace-sophia-depth-topology-bay-area-mutation-r5.jsonl`
  - `report-sophia-depth-topology-bay-area-mutation-r5-rerendered.md`
  - `trace-sophia-depth-topology-bay-area-mutation-r6.jsonl`
  - `report-sophia-depth-topology-bay-area-mutation-r6-rerendered.md`
  - These runs helped harden two report gates: final answers may not rewrite
    verified workspace artifact paths, and final answers may not hide failed
    NDP/tool calls behind generic no-data language.
- Rejected r7 after manual trace review despite an initial green report:
  - `trace-sophia-depth-topology-bay-area-mutation-r7.jsonl`
  - original report `report-sophia-depth-topology-bay-area-mutation-r7.md`
  - corrected report `report-sophia-depth-topology-bay-area-mutation-r7-rerendered.md`
  - The run staged/profiled/plotted `WWMT.CI.LY_.40.csv`, but the requested
    region was centered at `37.77 N, 122.42 W` with a `75 km` radius. The
    staged station catalog shows `WWMT` is about `670.5 km` from that center,
    while nearby Bay Area stations such as `UCSF`, `SBRU`, `MHDL`, and `EBMD`
    existed in the filtered metadata. The harness now rejects analysis-ready
    EarthScope GNSS station CSVs whose station ID is absent from the staged
    catalog or outside the requested radius.
- Accepted r8 only as grounded acquisition-blocker evidence:
  - `trace-sophia-depth-topology-bay-area-mutation-r8.jsonl`
  - `report-sophia-depth-topology-bay-area-mutation-r8.md`
  - The run resolved/filter-staged the Bay Area EarthScope station metadata
    and then attempted targeted station-resource searches including `UCSF`.
    Those NDP calls failed with `TimeoutError` and `ClosedResourceError`; the
    final answer surfaced the NDP search-tool blocker and did not invent a
    station CSV or PNG. This is a valid blocker-path trace, not a demo-ready
    successful analysis/visualization trace.
- Runtime hardening after r8: bounded retry behavior is no longer only prompt
  guidance. `SyncMCPToolExecutor` now tracks repeated consecutive transient
  failures per tool and raises `RepeatedToolFailureError` before opening more
  MCP calls after the configured failure limit. This is generic tool substrate,
  not EarthScope-specific routing. Focused tests cover transient timeouts,
  non-transient validation errors, and reset after success. Still missing:
  a fresh live provider run proving the strict-depth agent turns that runtime
  blocker into clean `resource_discovery.status=tool_failed`/final-answer
  evidence under real NDP/MCP failure.
- Rejected r9 after manual trace review despite an initial green report:
  - `trace-sophia-depth-topology-bay-area-mutation-r9.jsonl`
  - original report `report-sophia-depth-topology-bay-area-mutation-r9.md`
  - corrected report `report-sophia-depth-topology-bay-area-mutation-r9-rerendered.md`
  - The live run reached the full depth chain through profile, visualization,
    and synthesis with no failed tools, but it staged `WWMT.CI.LY_.40.csv` and
    then called `ndp_filter_earthscope_station_catalog` on that same station
    time-series CSV. That is not valid geographic grounding; the filter tool is
    for the EarthScope station metadata catalog (`earthscope_converted_data.csv`).
    The harness now rejects this exact misuse, and the resolver prompt now
    states that station time-series CSVs must never be passed to the station
    catalog filter.
- Accepted r10 only as grounded acquisition-blocker evidence:
  - `trace-sophia-depth-topology-bay-area-mutation-r10.jsonl`
  - `report-sophia-depth-topology-bay-area-mutation-r10.md`
  - The run resolved/filter-staged Bay Area station metadata and searched
    nearby stations such as `UCSF`, `SBRB`, `SBRU`, `MHDL`, and `EBMD`. It did
    not stage a station time-series CSV, profile, or plot. The final answer
    surfaced the grounded blocker instead of inventing an off-region artifact.
    This is valid negative evidence for mutable geography handling, not a
    demo-ready successful Bay Area analysis trace.

Mutation evidence:

- `trace-sophia-los-angeles-mutation-state-merge.jsonl`
- `report-sophia-los-angeles-mutation-state-merge.md`
- Marketplace commit: `0c6f742859826970e257a05be4aacd68ed5745bd`
- Later prompt/state hardening commit: `f56f641`
- Selected live station/resource: `WWMT.CI` / `WWMT.CI.LY_.40.csv`
- Verified artifacts:
  - `/home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/WWMT.CI.LY_.40.csv`
  - `/home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/WWMT.CI.LY_.40_timeseries.png`

Coordinate mutation evidence:

- Failed trace that exposed missing-path analysis routing:
  - `trace-sophia-coordinate-mutation-workflow-state-output.jsonl`
  - `report-sophia-coordinate-mutation-workflow-state-output.md`
- Passing trace after staged-path typed-state guard:
  - `trace-sophia-coordinate-mutation-missing-path-guard.jsonl`
  - `report-sophia-coordinate-mutation-missing-path-guard-rerendered.md`
- Passing trace after removing fabricated station CSV URL hints:
  - `trace-sophia-coordinate-mutation-5790f15.jsonl`
  - `report-sophia-coordinate-mutation-5790f15.md`
- Prompt target: center `34.05 N, 118.25 W`, radius `75 km`.
- Selected live station/resource: `MTA1` / `MTA1.CI.LY_.30.csv`.
- Verified artifacts:
  - `/home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/earthscope_converted_data.csv`
  - `/home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30.csv`
  - `/home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/MTA1_CI_LY_30_timeseries.png`

Width-topology coordinate mutation evidence:

- Passing trace:
  - `trace-sophia-width-topology-2e48e55.jsonl`
  - `report-sophia-width-topology-2e48e55.md`
- Prompt target: center `34.05 N, 118.25 W`, radius `75 km`.
- Selected live station/resource: `MTA1` / `MTA1.CI.LY_.30.csv`.
- Verified artifacts:
  - `/home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/earthscope_converted_data.csv`
  - `/home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30.csv`
  - `/home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30_timeseries.png`
- Reviewed route: `main` delegated to geospatial, NDP dataset discovery,
  EarthScope station catalog, NDP resource resolver, GNSS timeseries analysis,
  station network analysis, visualization, and synthesis. Tools included
  `ndp_search_datasets`, `ndp_get_dataset_details`, `ndp_stage_resource`,
  `ndp_filter_earthscope_station_catalog`, `ndp_profile_csv_resource`, and
  `ndp_plot_csv_timeseries`.
- Caveat: root semantic events captured the handoffs and tools, but child
  session logs were still empty.

Depth-topology rejected attempt:

- Empty trace placeholder:
  - `trace-sophia-depth-topology-2e48e55.jsonl`
- No accepted report was produced because the run was terminated manually.
- Observed live path: `main -> geospatial -> ndp_dataset_discovery ->
  earthscope_station_catalog -> ndp_resource_resolver`, followed by repeated
  search/stage/filter acquisition cycles instead of convergence into
  analysis/visualization.
- Rejected trace after runtime repeat guards:
  - `trace-sophia-depth-topology-turn-guard-2e48e55.jsonl`
  - `report-sophia-depth-topology-turn-guard-2e48e55.md`
- Observed live path after the guard: `main -> geospatial ->
  ndp_dataset_discovery -> earthscope_station_catalog`, with NDP metadata
  staging/filtering and no second completed station-catalog handoff. The run
  still failed because it stopped at `acquisition.status=metadata_only` for
  `earthscope_converted_data.csv` and never reached station-specific CSV
  staging, GNSS profiling, PNG visualization, or synthesis.
- Interpretation: strict depth remains useful as a design experiment, but it
  needs stronger typed completion state before it can be a demo or benchmark
  candidate. In particular, metadata-only station catalog evidence with
  suggested station search terms should route to resolver continuation, not to
  finalization.
- Rejected trace after station/resource-discovery hardening:
  - `trace-sophia-depth-topology-resource-discovery-2e48e55-r2.jsonl`
  - `report-sophia-depth-topology-resource-discovery-2e48e55-r2.md`
- Observed live path: station metadata was staged and filtered, resolver/
  discovery searched current station candidates, and the run found live station
  CSV candidates such as `MTA1.CI.LY_.30.csv` and `PKRD.CI.LY_.20.csv`.
  However, no station CSV was staged, no CSV profile ran, no PNG was generated,
  and no synthesis expert ran.
- Interpretation: this is a forward-moving rejected trace. The remaining
  depth-chain blocker is candidate-to-acquisition semantics: resource candidate
  URLs must flow through `ndp_stage_resource` before any expert may set
  `acquisition.status=staged` or `analysis_ready=true`. Candidate URLs alone
  are not analysis-ready evidence.
- Rejected trace after repeated-child runtime hardening:
  - `trace-sophia-depth-topology-resource-discovery-2e48e55-r5.jsonl`
  - `report-sophia-depth-topology-resource-discovery-2e48e55-r5.md`
- Observed live path: `main -> geospatial -> ndp_dataset_discovery ->
  earthscope_station_catalog -> ndp_resource_resolver`, with live NDP metadata
  staging/filtering, one recovered NDP search timeout, dataset details for
  `MTA1.CI.LY_.30`, and a concrete station CSV URL from current tool evidence.
  No station CSV was staged, no profile ran, no PNG was generated, and no
  synthesis expert ran.
- Interpretation: the repeated-child exception from r4 is fixed, but the depth
  pack still lets a concrete station CSV URL be treated as a non-staged blocker
  rather than forcing a resolver `ndp_stage_resource` call. The next accepted
  trace must show a second `ndp_stage_resource` call for the station CSV, then
  profile, visualization, and synthesis from that exact local path.

Depth-topology r6-r9 evidence after typed-state and resolver prompt hardening:

- Rejected trace after candidate URL normalization:
  - `trace-sophia-depth-topology-resource-discovery-2e48e55-r6.jsonl`
  - `report-sophia-depth-topology-resource-discovery-2e48e55-r6.md`
- Observed live path: the model found a concrete remote station CSV candidate
  (`MTA1.CI.LY_.30.csv`) but still did not stage it. Runtime merged state
  correctly overrode the model's claimed `analysis_ready=true` with
  `analysis_ready=false` because no local staged CSV path existed.
- Rejected trace after metadata-filter capability was added to the resolver:
  - `trace-sophia-depth-topology-resource-discovery-2e48e55-r8.jsonl`
  - `report-sophia-depth-topology-resource-discovery-2e48e55-r8.md`
- Observed live path: the run staged
  `/home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/earthscope_converted_data.csv`
  and called `ndp_filter_earthscope_station_catalog` for the requested
  coordinate/radius region. It found nearby stations such as `MTA1`, `PKRD`,
  `USC2`, `ELSC`, and `SILK`, but did not convert that filtered station
  evidence into station-specific CSV staging.
- Latest rejected trace:
  - `trace-sophia-depth-topology-resource-discovery-2e48e55-r9.jsonl`
  - `report-sophia-depth-topology-resource-discovery-2e48e55-r9.md`
- Observed live path: `main -> geospatial -> ndp_dataset_discovery ->
  earthscope_station_catalog -> ndp_resource_resolver`, with live NDP search,
  metadata staging, station-catalog filtering, and a search over the filtered
  station list. The run still stopped with
  `acquisition.status=metadata_only`; it did not stage a station time-series
  CSV, profile data, plot a PNG, or call synthesis.
- Current interpretation: tests now guard the typed-state invariants, but the
  benchmark is still rejected by trace review. The strict depth topology needs a
  stronger structured station-resource selection handoff between filtered
  station metadata and resolver staging. A passing test is insufficient here;
  acceptance requires a reviewed live trace with station-specific CSV staging,
  profile, visualization, and synthesis.

Depth-topology r13-r16 evidence after resolver repeat, artifact-state, and
provenance hardening:

- Manually interrupted trace:
  - `trace-sophia-depth-topology-resource-discovery-2e48e55-r13.jsonl`
- Observed live path: `main -> geospatial -> ndp_dataset_discovery ->
  earthscope_station_catalog -> ndp_resource_resolver ->
  seismic_event_catalog -> gnss_timeseries_analysis ->
  station_network_analysis -> visualization`. The run staged real NDP/EarthScope
  files, profiled the station CSV, and generated a plot, but then
  `earthscope_station_catalog` repeated resolver/analysis/visualization after
  completed child evidence.
- Interpretation: rejected. The root cause was pack-level repeat semantics on
  the station-catalog continuation. The fix removed `allow_repeat` from those
  resolver continuations so a completed resolver child is not re-run solely
  because its output still satisfies downstream state.
- Rejected trace after resolver repeat fix:
  - `trace-sophia-depth-topology-resource-discovery-2e48e55-r14.jsonl`
  - `report-sophia-depth-topology-resource-discovery-2e48e55-r14.md`
- Observed live path: no acquisition loop; the run staged
  `earthscope_converted_data.csv`, filtered nearby stations, staged
  `MTA1.CI.LY_.30.csv`, profiled the station CSV, and generated a PNG. It still
  failed because visualization returned a prose/string completion state rather
  than typed artifact state, so the chain did not route through synthesis and
  the final answer collapsed to earlier geospatial evidence.
- Interpretation: rejected. The fix added typed artifact inference from
  `ndp_plot_csv_timeseries` tool evidence so an actual generated PNG can produce
  `artifact.status=ready` for downstream synthesis routing without matching
  benchmark strings.
- Accepted full-chain trace with caveat:
  - `trace-sophia-depth-topology-resource-discovery-2e48e55-r15.jsonl`
  - `report-sophia-depth-topology-resource-discovery-2e48e55-r15.md`
- Observed live path: full strict-depth chain through `synthesis` with live NDP
  search, metadata staging, station filtering, station CSV staging, CSV profile,
  PNG visualization, and final synthesis. Verified artifacts:
  - `/home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/earthscope_converted_data.csv`
  - `/home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30.csv`
  - `/home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/MTA1_time_series_plot.png`
- Caveat: synthesis reported the staged CSV `source_url` as unset even though
  upstream tool evidence contained the NDP resource provenance. The follow-up
  fix made workflow-state merges preserve non-empty tool provenance instead of
  allowing later empty model fields to overwrite it.
- Failed live retry after provenance merge:
  - `trace-sophia-depth-topology-resource-discovery-2e48e55-r16.jsonl`
  - `report-sophia-depth-topology-resource-discovery-2e48e55-r16.md`
- Observed live path: the run reached NDP/EarthScope discovery but did not
  acquire station-specific data. After one NDP timeout, subsequent NDP tool
  calls returned `anyio.ClosedResourceError`; no station CSV, profile, PNG, or
  synthesis artifact was produced. The final answer surfaced the metadata-only
  blocker instead of inventing success.
- Current interpretation: r15 is the first reviewed full-chain strict-depth
  pass, but this topology is not demo-final until a healthy rerun proves the
  provenance merge and the runtime/tool layer short-circuits repeated NDP calls
  after a service-collapse error. Tests added around these failures are
  regression guardrails only; acceptance remains manual/agent review of the
  live JSONL trace, report, route graph, tool evidence, and artifacts on disk.

Bay Area mutation evidence after typed-state contract hardening:

- Rejected trace:
  - `trace-sophia-depth-topology-bay-area-mutation-r5.jsonl`
  - `report-sophia-depth-topology-bay-area-mutation-r5.md`
  - `report-sophia-depth-topology-bay-area-mutation-r5-rerendered.md`
- Observed live path: `main -> geospatial -> ndp_dataset_discovery ->
  earthscope_station_catalog -> ndp_resource_resolver`, with live NDP search,
  station metadata staging, spatial filtering around `37.77, -122.42` within
  `75 km`, and station-resource searches for Bay Area station IDs such as
  `UCSF` and `SBRB`.
- Runtime progress: the previous `contract-allowed child: <none>` deadlock is
  fixed. A declared child can now produce the missing typed state instead of
  being rejected before the state exists.
- Rejection reason: no analysis-ready station CSV was staged and no PNG was
  produced. The final answer also cited
  `/home/jcernuda/.clio/artifacts/ndp-staging/earthscope_converted_data.csv`,
  while the verified staged metadata path was
  `/home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/earthscope_converted_data.csv`.
  The benchmark verifier now fails visible answers that rewrite verified
  workspace artifact paths.

- Rejected trace after path-discipline prompt hardening:
  - `trace-sophia-depth-topology-bay-area-mutation-r6.jsonl`
  - `report-sophia-depth-topology-bay-area-mutation-r6.md`
  - `report-sophia-depth-topology-bay-area-mutation-r6-rerendered.md`
- Observed live path: metadata staging and Bay Area station filtering still
  worked, but later station-resource searches repeatedly failed. The original
  harness classification was `PASS` under the blocker-path criteria, but manual
  trace review rejected it because the final answer collapsed repeated failed
  NDP search calls into a generic "no concrete station-specific CSV" blocker.
- Follow-up hardening: the verifier now fails cases where failed tool calls are
  hidden from the visible answer, and the resolver prompt now requires
  `resource_discovery.status=tool_failed` plus failed tool names/arguments after
  repeated station-resource search failures. This is still not an accepted
  Bay Area mutation pass until a fresh trace shows that failure state honestly
  surfaced, or a healthy NDP run stages a current Bay Area station CSV.

Trace-review rule:

- Unit tests and verifier gates are regression guards for known invariants:
  typed state normalization, local path existence, station metadata provenance,
  visible artifact citation, and honest surfaced errors.
- They are not the benchmark result. A case is accepted only after reviewing the
  JSONL trace, route graph, tool arguments, local staged files, generated
  artifacts, and final assistant text. In particular, a harness `PASS` can still
  be rejected if the trace shows wrong geography, stale files, hidden tool
  failures, collapsed provenance, or a final answer that contradicts tool
  evidence.
