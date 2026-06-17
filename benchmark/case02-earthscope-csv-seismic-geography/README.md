# Case 02: EarthScope CSV Seismic Geography

Status: active pre-1.0 case. San Diego, Los Angeles, coordinate/radius, width
topology, and strict depth topology have each passed at least once with live
ALCF Sophia/NDP evidence. The case is not benchmark-final. The latest accepted
strict-depth run is a reviewed Los Angeles coordinate/radius trace, and Bay
Area mutation runs have exposed final-answer, provenance, and live NDP/MCP
reliability blockers that must be resolved before demo/benchmark claims.

Current strict-depth status from live r48-r51 review:

- `r48` passed the harness with a real ALCF/NDP EarthScope route, staged the
  Los Angeles-area `MTA1.CI.LY_.30.csv` station CSV, profiled it, plotted a PNG,
  and synthesized a final answer. Manual trace review rejected the run as
  benchmark-final because completed child handoff rows flattened most
  `output_summary` values into the final synthesis text. That made expert-local
  evidence hard to audit even though the overall workflow looked plausible.
- `r49` kept the live acquisition/analysis/visualization path working, but
  showed the first local-evidence patch was still capturing local summaries
  after nested settlement. The trace still inherited too much downstream final
  text in upstream child rows.
- `r50` failed the harness and is accepted as a useful semantic failure. The
  trace reached plotting, but manual artifact review found the workflow had
  staged off-region `YUHG.CI.LY_.20.csv` from a broad EarthScope/GNSS/CSV
  coordinate search. That station is outside the requested Los Angeles
  coordinate/radius target, proving why benchmark acceptance cannot be based on
  final-answer plausibility or a simple route PASS.
- `r51` is the latest reviewed live strict-depth pass. It used ALCF Sophia and
  live NDP/EarthScope tooling, traversed
  `main -> geospatial -> ndp_dataset_discovery -> earthscope_station_catalog ->
  ndp_resource_resolver -> seismic_event_catalog -> gnss_timeseries_analysis ->
  station_network_analysis -> visualization -> synthesis`, staged
  `earthscope_converted_data.csv` and `MTA1.CI.LY_.30.csv`, profiled the station
  CSV, generated `.clio/artifacts/plots/MTA1_CI_LY_30_timeseries.png`, and
  produced a non-empty PNG. Manual provenance inspection confirmed `MTA1` is
  `0.713 km` from the requested `34.05 N, -118.25 W` center. The trace now
  preserves distinct `local_output_summary` / `local_workflow_state` for each
  expert and parent-facing `return_output_summary` / `workflow_state` for
  continuation evidence.

Current blocker: r51 proves the current depth topology can execute the full
live data-acquisition, analysis, visualization, and synthesis path with
auditable local expert evidence. It is still not benchmark-final. Remaining
work is to replay mutable geography/resource cases, compare depth/width/domain
topologies under the same trace-review bar, and decide whether downstream
workflow-state rows must preserve full station provenance rather than relying
on upstream rows. The acceptance standard is reviewed trace plus artifact and
provenance inspection; tests only encode regressions discovered by that review.

Current-code positive-path replay r57:
`trace-sophia-depth-topology-current-r57.jsonl` and
`report-sophia-depth-topology-current-r57.md` were run live against ALCF Sophia
and NDP after terminal workflow-state hardening. The run still reaches the full
strict-depth acquisition path: station metadata catalog staging, spatial
filtering around `34.05 N, -118.25 W` with a `75 km` radius, station-specific
resource discovery, staging `MTA1.CI.LY_.30.csv`, profiling the CSV, and
plotting `/home/jcernuda/clio-agent/.clio/artifacts/ndp-plot/MTA1_time_series.png`.
Manual artifact/provenance inspection confirmed the staged station CSV has
`time`, `east`, `north`, `up`, `sigEE`, `sigNN`, `sigUU`, and `qChannel`
columns, the PNG is a non-empty `1400 x 672` image, and the staged
`earthscope_converted_data.csv` places active station `MTA1` at
`34.05522077, -118.24550778`, `0.713 km` from the requested center.

r57 is rejected as a demo-quality benchmark pass because the final visible
synthesis collapsed into an artifact-only status update. It cited the staged
CSV and PNG, but omitted the NDP source URL, station-region provenance, profile
or uncertainty evidence, and event/data-coverage limitations. The report was
re-rendered with a stricter final-brief verifier as
`report-sophia-depth-topology-current-r57-rerendered.md`, and the same trace now
fails for exactly that reason. This is the desired benchmark behavior: the
pipeline can work while the final collaborator-facing answer is still not good
enough.

Verifier correction from r48 review: the original r48 report failed with
`no staged EarthScope station metadata catalog was available to verify region`
even though the live trace staged `earthscope_converted_data.csv` and filtered
it before selecting `MTA1.CI.LY_.30.csv`. Manual inspection confirmed `MTA1`
appears in that catalog at `34.05522077, -118.24550778`, `0.713 km` from the
requested `34.05 N, -118.25 W` center. The benchmark verifier now checks both
artifact evidence and data/input files for the staged metadata catalog, because
EarthScope station metadata is input provenance, not a produced artifact. The
re-rendered report
`report-sophia-depth-topology-resource-discovery-2e48e55-r48-rerendered.md`
therefore passes the lane gate, but the acceptance standard remains trace,
data, artifact, and provenance review rather than the gate alone.

Bay Area mutable-geography replay r52:
`trace-sophia-depth-topology-bay-area-mutation-r52.jsonl` and
`report-sophia-depth-topology-bay-area-mutation-r52.md` are the latest reviewed
Bay Area replay after the resolver-frontier and verifier fixes. It used ALCF
Sophia and live NDP/EarthScope tooling, staged
`earthscope_converted_data.csv`, filtered the explicit `37.77 N, -122.42 W`
and `75 km` region, and found `67` nearby station candidates. Manual catalog
inspection confirmed the ranked candidates are geographically valid Bay Area
stations: `UCSF` at `3.444 km`, `SBRB` and `SBRU` at `9.325 km`, `MHDL` at
`10.360 km`, and `EBMD` at `12.971 km` from the requested center. The resolver
searched that ranked station set for station-specific GNSS time-series CSV
resources and found none. It did not stage, profile, or plot an off-region CSV;
the final answer correctly reported `metadata_only`/no analysis-ready station
CSV and withheld visualization.

r52 is accepted as a credible mutable-geography blocker trace, not as an
analysis/visualization success. The remaining efficiency issue is that the
model still requested repeated `SBRU` and `SBRB` searches after the bounded
station set had been covered; the typed runtime state skipped those duplicate
calls, so no extra NDP work occurred, but the blueprint prompts should stop
requesting them.

Earlier strict-depth status from live r37-r47 review:

- `r37` reached live NDP/EarthScope acquisition, staged
  `MTA1.CI.LY_.30.csv`, profiled it, and plotted a PNG, but manual trace review
  found a structured plot/file-policy error hidden as successful telemetry.
  That led to the structured-tool-error telemetry fix.
- `r38` correctly surfaced the structured staging error and failed cleanly. It
  exposed that the resolver could set an arbitrary `max_bytes` limit and then
  drift back to broad search instead of retrying the selected station CSV.
- `r39` proved cached exact resource reuse and no size-limit staging failure,
  but still called the station metadata filter on a station time-series CSV and
  stopped before analysis/visualization.
- `r40` is the best current semantic evidence run: it traversed the full depth
  chain through `main -> geospatial -> ndp_dataset_discovery ->
  earthscope_station_catalog -> ndp_resource_resolver ->
  seismic_event_catalog -> gnss_timeseries_analysis ->
  station_network_analysis -> visualization -> synthesis`, staged live
  `PKRD.CI.LY_.20.csv` (50875566 bytes), profiled 250000 rows with `time`,
  `east`, `north`, `up`, `sigEE`, `sigNN`, `sigUU`, and `qChannel`, and
  generated `PKRD.CI.LY_.20_timeseries.png` (138088 bytes). The PNG was
  visually inspected and is a non-empty east/north/up GNSS time-series plot.
  The harness still failed because the first plot call used a nonexistent
  output directory and the final visible answer omitted that recovered error.
- `r41` proved the plot tool can create missing allowed parent directories, but
  again called the station metadata filter on a station time-series CSV before
  recovering into profile/plot/synthesis.
- `r42` proved the station filter now returns typed `not_applicable` evidence
  for time-series CSVs instead of a failed tool row. It still stopped at
  `seismic_event_catalog` and did not reach GNSS profiling or visualization.
- `r43` failed at the DSPy adapter boundary: the model emitted a malformed
  final JSON object that was actually a tool intent for `ndp_stage_resource`.
  The runtime now recovers declared ReAct tool intents at that boundary and
  records the tool result as typed workflow state.
- `r44` reached the full depth chain and produced a PNG, but manual review found
  contradictory retained state: the final answer said acquisition was
  analysis-ready while the retained state still carried a stale
  `candidate_found` blocker. Tool-row JSON-string staging results are now parsed
  before workflow-state reconciliation.
- `r45` proved the staging-state fix, but stopped at `seismic_event_catalog`.
  Station-catalog evidence was present only as a compact preview payload, so the
  resolver did not preserve `resource_candidate.geographically_grounded=true`
  for the downstream GNSS-profile contract. Station-filter preview payloads are
  now decoded for typed state.
- `r46` improved acquisition and reached `seismic_event_catalog`, but did not
  run GNSS profiling, station-network analysis, visualization, or synthesis.
  The generated child-expert tool path was only recording compact natural
  language in active completions, so continuation contracts still evaluated
  lossy text instead of durable typed state.
- `r47` is the latest reviewed live strict-depth pass. It used ALCF Sophia and
  live NDP/EarthScope tooling, traversed
  `main -> geospatial -> ndp_dataset_discovery -> earthscope_station_catalog ->
  ndp_resource_resolver -> seismic_event_catalog -> gnss_timeseries_analysis ->
  station_network_analysis -> visualization -> synthesis`, staged
  `earthscope_converted_data.csv` and `MTA1.CI.LY_.30.csv`, profiled the station
  CSV, generated
  `.clio/artifacts/plots/MTA1.CI.LY_.30_timeseries.png` (89147 bytes), and
  produced a final answer citing the staged CSV and PNG. The report shows 22
  successful scientific tool rows, 0 failed tool rows, 22/22 successful rows
  with result evidence, and observed proofs for marketplace pack activation,
  root delegation, nested tier-3 delegation, and sync parent return.

Current blocker: r47 proves the full live data-acquisition, analysis,
visualization, and synthesis path can work through typed Agent Blueprint state,
but the case is not benchmark-final. Manual trace review still shows redundant
station-catalog/resource-resolution work and duplicate station-network
delegation before the chain settles. The generated PNG is valid and non-empty,
but uses raw epoch-like x-axis labels rather than a polished datetime axis.
Before demo/benchmark-final claims, reduce over-search/repeated delegation,
verify one mutable-geography replay, and keep the acceptance bar as reviewed
trace plus artifact/provenance inspection, not harness PASS alone.

Latest Bay Area mutable-geography audit:
`trace-sophia-depth-topology-bay-area-mutation-r27.jsonl` and
`report-sophia-depth-topology-bay-area-mutation-r27.md` are the latest reviewed
Bay Area blocker evidence after typed station-resource search coverage and
report artifact-classification fixes. The route ran live against ALCF Sophia
and NDP, resolved the explicit Bay Area coordinate/radius request, staged and
filtered the live `earthscope_converted_data.csv` station metadata, found
67 stations inside 75 km, and searched the ranked nearby stations `UCSF`,
`SBRB`, `SBRU`, `MHDL`, and `EBMD` for concrete station CSV resources. It did
not find or stage a station-specific GNSS time-series CSV for those candidates,
so it correctly withheld GNSS profiling and PNG generation and returned:

```text
Staged CSV path: none (no station-specific CSV found)
Source URL (metadata): https://nationaldataplatform.org/catalog/dataset/811f0bcc-99e5-455c-bcf6-7c63c2634f41/resource/a420cc30-2262-423a-8c63-3ad8d91f2a8f/download/earthscope_converted_data.csv
Size: N/A
Blocker: All candidate stations (UCSF, SBRB, SBRU, MHDL, EBMD) have been
exhaustively searched; only generic catalog metadata is available, not a
station-specific GNSS time-series CSV.
```

r27 improved the r26 over-search pattern from 17 station-resource attempts to
7 station-resource attempts, and the station-code coverage rows now carry
structured `station_code` evidence for actual station-resource searches. Manual
review still rejects r27 as benchmark-final because `SBRU` and `MHDL` were
searched twice with equivalent calls before the blocker settled. The report was
re-rendered after fixing artifact extraction so `earthscope_converted_data.csv`
is a data/input file and URL fragments such as `/download/...` are not counted
as missing local artifacts.

`trace-sophia-depth-topology-bay-area-mutation-r28.jsonl` and
`report-sophia-depth-topology-bay-area-mutation-r28.md` are a diagnostic replay,
not an accepted improvement. The harness passed and the final answer correctly
reported `acquisition.status=metadata_only`, `analysis_ready=false`, and no
PNG/artifact. Manual trace review found the run expanded to 22 scientific tool
rows, repeated broad metadata staging/details calls, and searched equivalent
station-resource coverage across `UCSF`, `SBRB`, `SBRU`, `MHDL`, and `EBMD`
after the blocker should already have been settled. It also exposed that
generic discovery vocabulary such as `PBO` and `LIST` can be mistyped as station
codes if station-code detection is too permissive. Those false-positive terms
are now excluded, but the larger blocker remains: resolver/orchestrator stop
logic must be driven by accumulated structured workflow state and tool-result
coverage, not by repeated model attempts or free-text search contracts.

The Bay Area case therefore proves the runtime no longer promotes
metadata-only acquisition into a false `analysis_ready=true` path, but it also
proves why benchmark/demo readiness cannot be accepted from the harness `PASS`
label alone. The remaining work is to make the resource resolver stop after a
bounded, typed station-resource coverage set and to keep using reviewed trace,
artifact/data classification, and provenance inspection as the acceptance bar.

Latest reviewed coordinate/radius evidence:
`trace-sophia-coordinate-mutation-5790f15.jsonl` and
`report-sophia-coordinate-mutation-5790f15.md`, produced with marketplace commit
`5790f15 Avoid fabricated EarthScope station CSV URLs`. That run used live NDP
station-resource search, staged `MTA1.CI.LY_.30.csv`, profiled the CSV,
generated `MTA1_CI_LY_30_timeseries.png`, and synthesized from the completed
evidence. It is one accepted trace, not a benchmark-final guarantee.

Latest reviewed width-topology evidence:
`trace-sophia-width-topology-2e48e55.jsonl` and
`report-sophia-width-topology-2e48e55.md`, produced with marketplace commit
`2e48e55 Add EarthScope topology variants`. That run used explicit
coordinate/radius geography centered at `34.05 N, 118.25 W`, resolved live
NDP/EarthScope data, selected and staged `MTA1.CI.LY_.30.csv`, profiled the
CSV, generated `MTA1.CI.LY_.30_timeseries.png`, and synthesized from the
completed evidence. The strict depth topology was also attempted against the
same backend and prompt family, but it repeatedly cycled through acquisition
search/stage/filter branches for about fourteen minutes and was stopped before
analysis or visualization. Treat that as a rejected topology finding, not as an
accepted trace.

Latest reviewed depth-topology evidence:
`trace-sophia-depth-topology-resource-discovery-2e48e55-r33.jsonl` and
`report-sophia-depth-topology-resource-discovery-2e48e55-r33.md`. This is the
latest accepted strict-depth trace after manual review. It used live ALCF
Sophia and live NDP/EarthScope tools, resolved the explicit coordinate/radius request,
searched broad EarthScope/GNSS/GPS CSV catalog evidence, fetched details for the
EarthScope Stations Dataset, staged `earthscope_converted_data.csv`, filtered
nearby stations around `34.05 N, -118.25 W`, selected/staged
`MTA1.CI.LY_.30.csv`, profiled the station CSV, generated
`MTA1_time_series.png`, and synthesized from completed evidence through
the full depth path:

```text
main -> geospatial -> ndp_dataset_discovery -> earthscope_station_catalog
-> ndp_resource_resolver -> seismic_event_catalog -> gnss_timeseries_analysis
-> station_network_analysis -> visualization -> synthesis
```

r33 is accepted as live evidence for the data-acquisition, analysis,
visualization, and typed-state depth-chain semantics. Manual trace review found
the final assistant text part was user-facing clean and cited the staged CSV and
PNG. Local artifact inspection confirmed `MTA1` is present in the staged
EarthScope station metadata at latitude `34.05522077`, longitude
`-118.24550778`, `0.713 km` from the requested center, and that the
profiled/plotted station CSV contains `time`, `east`, `north`, and `up`
columns. The PNG was visually inspected and is a non-empty time-series plot for
the three displacement components. Verified local artifacts from the run:

- `.clio/artifacts/ndp-staging/earthscope_converted_data.csv` (`Site`,
  `Latitude`, `Longitude`, station metadata; 153082 bytes).
- `.clio/artifacts/ndp-staging/MTA1.CI.LY_.30.csv` (`time`, `east`, `north`,
  `up`, and quality columns; 50424246 bytes).
- `.clio/artifacts/MTA1_time_series.png` (PNG, 87581 bytes).

Caveats from r33 trace review:

- The report and raw trace prove the full route and tool sequence, but exported
  `tools_called` rows are still live-observer lifecycle rows without returned
  tool-result evidence. The benchmark report now surfaces this as
  `Tool rows with result evidence: 0/20`, so a pass cannot be accepted without
  separate artifact/session review.
- Compact handoff summaries still drop some dataset/source provenance fields:
  the final typed state preserves `resource_name=MTA1.CI.LY_.30.csv` and the
  staged local path, but `dataset_id` and `source_url` are empty in the compact
  state despite being present in tool-call arguments. This remains a runtime
  evidence-propagation blocker.

The benchmark acceptance bar is therefore not "the harness says PASS"; it is
reviewed trace plus artifact/provenance inspection.

Latest rejected strict-depth telemetry/acquisition evidence after r33:

- `trace-sophia-depth-topology-resource-discovery-2e48e55-r36.jsonl` and
  `report-sophia-depth-topology-resource-discovery-2e48e55-r36.md`: rejected as
  a complete benchmark/demo pass. It used live ALCF Sophia and live NDP tools
  with the runtime telemetry fix that records bounded successful tool results.
  The report shows `24` reviewable scientific tool rows, `13` successful,
  `11` failed, and `13/13` successful rows carrying result evidence.
- r36 did prove the first half of the intended pipeline with auditable
  evidence: `ndp_search_datasets` returned the EarthScope Stations Dataset,
  `ndp_stage_resource` staged
  `.clio/artifacts/ndp-staging/earthscope_converted_data.csv`, and
  `ndp_filter_earthscope_station_catalog` found `113` stations within the
  requested `75 km` radius around `34.05 N, -118.25 W`. The nearest station was
  `MTA1` at `34.05522077, -118.24550778`, `0.713 km` from the requested center.
- r36 failed because the resolver did not converge on a station-specific
  time-series CSV. It repeatedly searched station/resource variants, hit
  several `TimeoutError`, `RepeatedToolFailureError`, and `ClosedResourceError`
  failures, delegated into `seismic_event_catalog` before acquisition was
  settled, and ended with no staged analysis-ready station CSV, no profile, no
  station-network analysis, no visualization, and no synthesis from completed
  analysis.
- The immediate blocker exposed by r36 is not initial catalog discovery; it is
  acquisition-state control after station filtering. The resolver needs to
  either stage a concrete resource returned by live NDP search, or return a
  typed `resource_discovery.status=blocked` / `acquisition.status=metadata_only`
  state that stops analysis/visualization routing cleanly without repeated
  broad searches or regional no-data claims.

Bay Area mutation evidence after r30:

- `trace-sophia-depth-topology-bay-area-mutation-r1.jsonl` and
  `report-sophia-depth-topology-bay-area-mutation-r1.md`: rejected. The run
  reached live Bay Area discovery but treated a station-specific time-series
  CSV as station metadata and attempted to spatially filter it. This exposed a
  missing typed-state route from selected resource candidate to resolver
  staging when no `acquisition` state was present.
- `trace-sophia-depth-topology-bay-area-mutation-r2.jsonl` and
  `report-sophia-depth-topology-bay-area-mutation-r2.md`: rejected. The route
  improved to `earthscope_station_catalog -> ndp_resource_resolver`, staged and
  filtered `earthscope_converted_data.csv`, and searched nearby station IDs, but
  did not stage a station time-series CSV and returned stale geospatial prose.
- `trace-sophia-depth-topology-bay-area-mutation-r3.jsonl` produced real live
  evidence for `37.77 N, -122.42 W` with station/resource
  `WWMT.CI.LY_.40.csv`, profiled the CSV, generated
  `WWMT.CI.LY_.40_timeseries.png`, and traversed the full depth chain through
  synthesis. It is rejected after manual trace review because the final visible
  answer still said no EarthScope GNSS station/time-series was verified. The
  re-rendered report
  `report-sophia-depth-topology-bay-area-mutation-r3-rerendered.md` uses the
  tightened harness and correctly marks this as a failure despite the internal
  artifact evidence.
- `trace-sophia-depth-topology-bay-area-mutation-r4.jsonl` and
  `report-sophia-depth-topology-bay-area-mutation-r4.md`: rejected as a
  successful-acquisition demo, but useful runtime evidence. The patched
  strict-depth bubbling semantics made the root answer preserve the downstream
  resolver blocker instead of stale geospatial prose. Acquisition failed because
  a 30s NDP search timeout was followed by `ClosedResourceError` failures in
  search/details/staging, so the run ended at a metadata/tool-availability
  blocker rather than analysis and visualization.

Remaining caveats for benchmark-final status:

- The latest strict-depth acceptance is a coordinate/radius run centered near
  Los Angeles. Bay Area mutation attempts changed geography and resource
  selection, but no Bay Area run is accepted yet: r3 had the data/artifact but
  wrong final answer, and r4 had the right blocker bubbling but no successful
  acquisition due live NDP/MCP failures.
- The run proves staged-file acquisition, profiling, visualization, and
  synthesis, but it does not yet perform deeper quantitative GNSS science such
  as velocity fitting, anomaly detection, or event correlation.
- The raw JSONL intentionally retains internal typed-state audit payloads in
  expert handoff metadata. Reports and final assistant text must stay clean.

Immediate rejected predecessor evidence:

- `r18` stopped after two narrow NDP keyword searches and incorrectly concluded
  no EarthScope/GNSS station data existed. That failure motivated typed
  `search_coverage` evidence from `ndp_search_datasets` and a
  `catalog.status=search_incomplete` retry path.
- `r19` repeated broad discovery instead of stopping early, but still used an
  over-constrained search strategy (`station/catalog` plus an assumed
  `owner_org=earthscope`) and returned no candidates. Direct NDP tool review
  showed the real broad query must allow `EarthScope`, `GNSS` or `GPS`, and
  `CSV`/`raw_csv`, because the EarthScope Stations Dataset is discoverable that
  way.
- Earlier `r15` was the first full-chain depth pass but lost the staged CSV
  `source_url` in synthesis. `r16` then hit NDP/MCP service failure before the
  provenance fix could be proven.

Earlier depth-topology retries remain useful failure evidence:

- `trace-sophia-depth-topology-turn-guard-2e48e55.jsonl` and
  `report-sophia-depth-topology-turn-guard-2e48e55.md`: runtime repeat guards
  prevented a long acquisition loop, but the chain stopped at metadata-only
  station catalog evidence and never reached station-specific CSV acquisition,
  analysis, visualization, or synthesis.
- `trace-sophia-depth-topology-resource-discovery-2e48e55-r9.jsonl` and
  `report-sophia-depth-topology-resource-discovery-2e48e55-r9.md`: the run
  staged and spatially filtered live EarthScope station metadata and searched
  nearby station IDs, but stopped at `acquisition.status=metadata_only`.
- `trace-sophia-depth-topology-resource-discovery-2e48e55-r13.jsonl`: the live
  path reached visualization with real staged files, but repeated
  resolver/analysis/visualization after child completion. This led to removing
  repeat semantics from station-catalog resolver continuations.
- `trace-sophia-depth-topology-resource-discovery-2e48e55-r14.jsonl` and
  `report-sophia-depth-topology-resource-discovery-2e48e55-r14.md`: no loop, and
  real station CSV/profile/PNG evidence existed, but visualization did not
  expose typed artifact state so the chain did not route into synthesis.

Earlier rejected resource-discovery traces remain useful evidence:

- `r2` found live station CSV candidates but incorrectly reported
  `analysis_ready=true` with no staged local CSV path. Runtime state
  normalization now downgrades that condition to a candidate/blocker state.
- `r3` failed honestly at metadata-only acquisition and did not route far
  enough into resource resolution.
- `r4` reached the resource resolver, but a repeated ReAct child-tool call
  raised `child already completed`; runtime now returns prior compact child
  evidence for repeated child calls instead of throwing inside the trajectory.
- `r6` exposed a typed-state normalization gap: the model reported a remote CSV
  URL as `analysis_ready=true`/`data_available` without a local staged path.
  Runtime normalization now downgrades non-staged remote URLs to
  `candidate_found`.
- `r7` staged the metadata CSV but skipped the required spatial filter before
  claiming no regional station resources. The resolver now has access to
  `ndp_filter_earthscope_station_catalog` and documents that keyword search is
  not spatial evidence.
- `r8` proved metadata staging plus spatial filtering and found live nearby
  stations, but still stopped at metadata-only acquisition.
- `r9` repeated the metadata staging/filtering path and searched the filtered
  station list, but still did not stage a station-specific time-series CSV. This
  is the current rejected trace and should be treated as the blocker for the
  strict depth topology.

## Prompt Intent

Ask CLIO to investigate recent seismic/geodetic activity around a
user-provided U.S. place or region using public NDP/EarthScope GNSS station
metadata and station time-series CSV evidence first. The prompt should not
mention SAC or internal waveform tools.

Example:

```text
Explore recent seismic/geodetic activity around the San Diego area. Resolve the
requested geography, find public EarthScope/NDP GNSS station or station
time-series evidence for that region, stage a concrete CSV resource when
available, analyze the station time series and uncertainty columns, produce a
PNG artifact from the staged CSV when analysis-ready data exists, and explain
data freshness, coverage, and provenance limitations.
```

## Semantics To Prove

- Generic geospatial resolver emits center, bbox or radius, confidence, and
  source provenance.
- EarthScope/NDP discovery consumes the resolved region instead of
  resolving place names internally.
- The first evidence layer is proper CSV/tabular station, GNSS, or metadata
  evidence where available.
- Waveform/SAC handling is optional and only follows correct discovery and
  staging. SAC is not the benchmark target.
- Event-catalog handling is optional and only follows an explicit user request
  for earthquakes/events, magnitudes, depths, epicenters, or prior tool
  evidence that makes event context necessary. A general regional
  "seismic activity" prompt does not force that branch.
- Analysis explains station suitability, time-series/profile evidence,
  uncertainty columns, data limitations, and provenance.

## Required Expert Decomposition

- `main`: owns the collaborator question and merges geography, NDP/GNSS
  station-resource acquisition, analysis, visualization, and synthesis
  evidence.
- `geospatial`: converts the input region into coordinates or coordinate range.
  Input: natural place, state, county, bbox, or explicit lat/lon. Output:
  region object with center, bbox or radius, source, confidence, and warnings.
- `ndp_dataset_discovery`: consumes only the resolved region object and queries
  NDP for EarthScope GNSS metadata and station-resource candidates.
- `earthscope_station_catalog`: ranks nearby GNSS stations from staged station
  metadata and preserves regional provenance.
- `ndp_resource_resolver`: stages a concrete station time-series CSV only when
  current NDP tool evidence identifies one for the resolved region.
- `gnss_timeseries_analysis`: profiles the staged CSV, required columns,
  displacement ranges, uncertainty ranges, and scan/full-file caveats.
- `station_network_analysis`: assesses station suitability, proximity,
  redundancy, and whether one station is enough for the user's question.
- `seismic_event_catalog`: optional. It may report event-context evidence or a
  capability gap only when the parent asks for event catalog evidence.
- `waveform_format`: optional. It may inspect SAC or another waveform format
  only after the catalog/staging path has produced valid waveform input.
- `visualization`: produces station/time-series plots when the staged data
  supports them.

Spatial resolution is a required semantic evidence boundary. It may be executed
by a dedicated `geospatial` expert or by a data-domain expert that delegates to
geospatial, but the trace must expose typed region state before domain catalog
querying. The EarthScope expert must not parse city names or use built-in
location hints. SAC-specific tooling is a leaf-stage format handler, not the
discovery or geography layer.

## Current Core Problem

The previous seismic demo used a SAC/EarthScope tool with built-in location
hints. That is a semantic shortcut. This case fails until geography is a generic
capability and EarthScope discovery is driven by the resolved region object.

The case is also not complex enough if it stops at "download one file and plot
it." The public benchmark must show region resolution, NDP station metadata
selection, region-grounded station CSV acquisition, analysis, provenance,
visualization, and only then optional event or waveform work when requested.

## Hierarchy Semantics To Compare

This case should be used to compare hierarchy semantics, not just final
answers. The same scientific workflow can be expressed as depth, width, or
domain grouping. Those choices test different CLIO capabilities.

The marketplace now contains three concrete Agent Blueprint variants for this
comparison:

- `earthscope-gnss-region`: domain-grouped production candidate.
- `earthscope-gnss-region-width`: root-owned width topology.
- `earthscope-gnss-region-depth`: strict depth-chain topology.

All three variants validate structurally and avoid free-text continuation
routing. `earthscope-gnss-region` has accepted real ALCF Sophia/NDP traces for
the domain-grouped path. `earthscope-gnss-region-width` has one accepted
coordinate/radius trace after review. `earthscope-gnss-region-depth` now has one
accepted full-chain trace after review, but it remains under active hardening
because the accepted pass had a provenance caveat and the immediate retry
exposed live NDP/MCP service instability.

### Depth Semantics

```text
main
-> geospatial
-> ndp_dataset_discovery
-> earthscope_station_catalog
-> ndp_resource_resolver
-> gnss_timeseries_analysis
-> station_network_analysis
-> visualization
-> synthesis
```

This layout tests whether CLIO can preserve typed evidence through a long
dependency chain. It is easy to inspect and friendly to smaller models, but it
is brittle: one bad upstream choice contaminates every downstream step, and
independent evidence streams are forced into sequence.

Registered pack: `earthscope-gnss-region-depth`.

Current evidence: accepted in reviewed r30. Earlier live Sophia/NDP runs started
correctly (`main -> geospatial -> ndp_dataset_discovery ->
earthscope_station_catalog -> ndp_resource_resolver`) but either looped in
acquisition, stopped at metadata-only acquisition, found candidate station CSV
URLs without staging them, or over-constrained the NDP catalog search. r30
proved the full depth path with real station metadata, station CSV staging,
profile analysis, PNG visualization, and synthesis. The topology still needs
mutation evidence across changed geographies/resources and deeper quantitative
science before it is benchmark-final.

### Width Semantics

```text
main
├─ geospatial
├─ ndp_dataset_discovery
├─ earthscope_station_catalog
├─ ndp_resource_resolver
├─ seismic_event_catalog (optional, only when event context is requested)
├─ gnss_timeseries_analysis
├─ station_network_analysis
├─ visualization
└─ synthesis
```

This layout tests fanout, parallel evidence gathering, and merge quality. It
should expose whether CLIO can coordinate independent expert branches and catch
inconsistencies. It also increases orchestration burden: without strict
contracts, branches may duplicate work or use inconsistent assumptions.

Registered pack: `earthscope-gnss-region-width`.

Current evidence: accepted once after trace review. The run used live
ALCF Sophia/NDP, explicit coordinates instead of a benchmark city string, live
NDP search and staging, CSV profiling, station-network review, PNG generation,
and final synthesis. Caveats: child session logs are still not captured even
though semantic handoff/tool events are present, and the run is one coordinate
mutation, not a final benchmark guarantee.

### Domain Semantics

```text
main
├─ data
│  ├─ geospatial
│  ├─ ndp_dataset_discovery
│  ├─ earthscope_station_catalog
│  └─ ndp_resource_resolver
├─ analysis
│  ├─ gnss_timeseries_analysis
│  └─ station_network_analysis
│  └─ seismic_event_catalog (optional, only when event context is requested)
├─ visualization
│  ├─ regional_map
│  └─ gnss_timeseries_plot
└─ synthesis
```

This is the preferred production target because it matches reusable CLIO
capability domains: data search/acquisition, scientific analysis,
visualization, and final synthesis. It should make tool permissions and
blueprint reuse cleaner across benchmark cases.

Registered pack: `earthscope-gnss-region`.

The risk is that a broad `data` expert can hide the actual semantics. For this
case, geospatial may live under `data`, but it must remain a distinct
inspectable boundary. The benchmark must fail any trace where NDP or EarthScope
discovery quietly parses `San Diego` internally and starts querying resources
without an explicit region object.

Non-negotiable rule:

```text
if the prompt contains spatial intent,
the trace must include explicit geospatial resolution
before any domain catalog query consumes the location
```

The orchestrator may call `geospatial` directly or the `data` expert may call
it as a child. Either is acceptable. What is not acceptable is collapsing
geography into a domain-specific search tool, especially a SAC or EarthScope
helper with built-in city hints.

Typed workflow state is the reliability mechanism, not free-text substring
matching. For example, analysis may follow acquisition when structured evidence
contains `acquisition.status=staged`, `acquisition.analysis_ready=true`, and a
local staged path that exists on disk. That branch is valid because it routes
over semantic state returned by experts/tools. It is not valid to route because
the final prose happens to contain a station name, a city, or a filename seen in
a previous run.

Station metadata is not station time-series acquisition. A catalog helper may
return nearby stations, provenance, and search terms. It must not manufacture
raw CSV URLs from station IDs or suffix guesses. A station CSV is
analysis-ready only after a current NDP search/resource result identifies the
resource and `ndp_stage_resource` writes a local file that exists.

Tests should encode invariants discovered during trace review, but tests alone
do not certify this benchmark case. Unit tests cover state-space rules such as
"candidate URLs are not analysis-ready acquisition" and "analysis may only run
after a staged local CSV path exists." Integration tests cover tool wiring,
workspace defaults, and report invariants. Neither class of test proves that the
scientific workflow made sense. A passing test suite can prevent known failures
from reappearing; it cannot replace trace review or artifact inspection.

A run is accepted only after reviewing the JSONL trace, the report, the route
graph, the tool evidence, and the artifact files on disk. The review must answer
at least these questions:

- Did the trace resolve geography before domain data discovery?
- Did every station, dataset, URL, local path, profile, and PNG come from the
  current run's live tool evidence?
- Did candidate metadata remain distinct from staged analysis-ready data?
- Did analysis consume the staged file that acquisition produced?
- Did visualization create a real artifact on disk from analyzed data?
- Did synthesis preserve blockers and limitations rather than filling gaps with
  plausible prose?
- Would the same semantics still work if the prompt used a different supported
  city, coordinate, or radius?

The review must use human/agent judgment over the trace. Automated pass/fail
checks are regression guardrails: they can prove that a known invariant still
holds, but they cannot prove that the scientific path is meaningful, that the
data choice is appropriate, or that the synthesis is demo-worthy.

## Corrected NDP/EarthScope GNSS Pipeline

The reviewed San Diego path should be treated as a complex data-search,
analysis, and visualization workflow:

```text
natural language geography
-> geospatial region object
-> NDP EarthScope station catalog discovery
-> nearest GNSS station/resource selection
-> NDP-listed station CSV acquisition
-> CSV validation and time-series profiling
-> displacement/uncertainty analysis
-> map or time-series artifact
-> final scientific brief with provenance
```

The target data layer is NDP-exposed EarthScope GNSS station/time-series data
unless the user explicitly asks for waveform data. A SAC file is a waveform
format artifact, not the default answer to "EarthScope around San Diego."

Manual NDP catalog exploration found this viable path for San Diego:

- Resolve San Diego, CA to approximately `lat=32.7157`, `lon=-117.1611`.
- Use the NDP EarthScope station metadata CSV as the first catalog evidence
  layer.
- Rank nearby EarthScope GNSS stations by distance from the resolved center.
- Select an NDP-listed station package/resource, then download the station CSV
  through the NDP resource URL.
- Analyze `time`, `east`, `north`, `up`, `sigEE`, `sigNN`, and `sigUU`.
- Produce a time-series visualization and a short evidence-backed discussion of
  station coverage, displacement ranges, uncertainty, and limitations.

Nearest station evidence observed during manual exploration:

| Station | Network | Status | Latitude | Longitude | Distance |
| --- | --- | --- | ---: | ---: | ---: |
| `P475` | `NOTA` | `ACTIVE` | 32.66640 | -117.24394 | 9.5 km |
| `SIO5` | `CRTN` | `ACTIVE` | 32.84074 | -117.24970 | 16.2 km |
| `P473` | `NOTA` | `ACTIVE` | 32.73378 | -116.94952 | 19.9 km |
| `P472` | `NOTA` | `ACTIVE` | 32.88921 | -117.10470 | 20.0 km |
| `JAS1` | `CRTN` | `ACTIVE` | 32.82792 | -116.98809 | 20.4 km |
| `NSSS` | `CRTN` | `ACTIVE` | 32.57932 | -116.97269 | 23.3 km |

Example NDP station resources found for `P475`:

- Package `p475-ci-ly-20`, title `P475.CI.LY_.20`, tag `gnss`.
- CSV resource:
  `https://ds2.datacollaboratory.org/Earthscope_api_dec2024/raw_csv/P475.CI.LY_.20.csv`
- PNG resource:
  `https://ds2.datacollaboratory.org/Earthscope_api_dec2024/generated_png/P475.CI.LY_.20.png`
- Dashboard resource:
  `https://di.ndp.utah.edu/datasets/339c7577-adb1-4f8b-a1c2-a45ef2db142e`

The downloaded `P475.CI.LY_.20.csv` was about 49 MB and contained 858,619
rows from `2024-12-03T00:00:00+00:00` through
`2024-12-12T23:59:46+00:00`. Observed numeric ranges:

| Column | Minimum | Maximum |
| --- | ---: | ---: |
| `east` | -3.377 | 2.593 |
| `north` | -6.002 | 3.793 |
| `up` | -2.774 | 11.877 |
| `sigEE` | 0.028 | 68.987 |
| `sigNN` | 0.032 | 23.798 |
| `sigUU` | 0.064 | 99.685 |

These observations are not a benchmark pass by themselves. They define the
workflow CLIO must execute through marketplace Agent Blueprints and live tool
provenance.

## Pass Criteria

A passing run must produce inspectable evidence for every layer:

- Region object: center, radius or bbox, confidence, and provenance.
- NDP catalog evidence: station metadata source and query/resource provenance.
- Resource selection: ranked nearby stations and justification for chosen CSVs.
- Geographic resource provenance: any station-specific CSV used for analysis
  must match a station ID from the current filtered station metadata or
  equivalent coordinate evidence inside the requested radius.
- Acquisition evidence: downloaded CSV path, source URL, size, and row count.
- Validation evidence: required columns, time range, missing-value or quality
  notes, and no stale local-file reuse.
- Analysis evidence: displacement ranges, uncertainty ranges, station coverage,
  and limitations.
- Visualization artifact: non-empty map or time-series plot generated during
  the run.
- Final answer: cites selected stations, source URLs, artifact paths, and data
  limitations.

The case fails if the run:

- starts by downloading or plotting a SAC file without first proving catalog
  and station/resource semantics;
- uses a San Diego-specific built-in location hint in an EarthScope tool rather
  than a generic geospatial expert;
- answers with only prose or a PNG path;
- cannot tolerate a changed U.S. city, state, bbox, or explicit coordinate
  input;
- hides data search, acquisition, analysis, or visualization inside one opaque
  expert response.
- reaches a pass only because the benchmark city, station ID, or resource name
  matched a previously seen string.
- stages, profiles, plots, or marks `analysis_ready=true` for an off-region
  station CSV. For example, a Bay Area request must not pass with a Southern
  California station resource merely because the CSV has valid GNSS columns.
- uses `ndp_filter_earthscope_station_catalog` on a station time-series CSV
  instead of on the EarthScope station metadata catalog. Filtering
  `WWMT.CI.LY_.40.csv` does not prove `WWMT` is inside a requested Bay Area
  radius.
- rewrites verified local artifact paths, for example by changing a workspace
  path under `/home/jcernuda/clio-agent/.clio/...` into
  `/home/jcernuda/.clio/...`;
- hides failed tool calls in the final answer. If NDP search or staging fails,
  the final synthesis must say that the service/tool failed and preserve enough
  failed-call evidence to distinguish "no matching station CSV exists" from
  "station-resource search could not complete."
- loops through repeated equivalent NDP search failures instead of converting
  the failed acquisition path into structured blocker evidence. The tool
  substrate now has a generic repeated-transient-failure guard; the benchmark
  still needs fresh live-provider evidence that the Agent Blueprint turns that
  guard into clean acquisition-blocker synthesis.

## 2026-06-05 ALCF Bay Area Mutation Trace Review

The Bay Area coordinate/radius mutation was rerun repeatedly against the live
ALCF Sophia provider and NDP/EarthScope tools:

- `trace-sophia-depth-topology-bay-area-mutation-r30.jsonl`
- `trace-sophia-depth-topology-bay-area-mutation-r31.jsonl`
- `trace-sophia-depth-topology-bay-area-mutation-r32.jsonl`
- `trace-sophia-depth-topology-bay-area-mutation-r33.jsonl`
- `trace-sophia-depth-topology-bay-area-mutation-r34.jsonl`
- `trace-sophia-depth-topology-bay-area-mutation-r35.jsonl`
- `trace-sophia-depth-topology-bay-area-mutation-r52.jsonl`
- `trace-sophia-depth-topology-bay-area-mutation-r53.jsonl`
- `trace-sophia-depth-topology-bay-area-mutation-r54.jsonl`
- `trace-sophia-depth-topology-bay-area-mutation-r55.jsonl`
- `trace-sophia-depth-topology-bay-area-mutation-r56.jsonl`

All six runs were harness `PASS` results, but none is accepted as a benchmark
pass. The harness pass only means the recorded session returned a grounded
answer without crashing; it does not prove scientific workflow quality.

Observed progress:

- The run no longer falls back to stale SAC waveform semantics.
- The active marketplace depth topology resolves the requested coordinates,
  discovers the EarthScope station metadata CSV, stages
  `earthscope_converted_data.csv`, and filters station candidates around
  `37.77, -122.42` with a 75 km radius.
- `r33` proves executor-level typed workflow interception is visible in the
  live JSONL: duplicate and exhausted station-resource searches are returned
  with `_meta.status=skipped`, `clio_runtime.workflow_state`, and reasons such
  as `duplicate_station_resource_search` and
  `resource_discovery_search_exhausted`.
- `r34` proves the terminal typed-state finalization guard is active. The final
  answer no longer proposes more same-run station searches after
  `resource_discovery.status=search_exhausted`; it reports the metadata CSV,
  67 nearby station candidates, the searched ranked stations
  `UCSF, SBRB, SBRU, MHDL, EBMD`, and explicitly says no GNSS profiling or
  visualization ran because acquisition remained metadata-only.
- `r35` proves the resolver-frontier contract is improved for this mutation.
  `ndp_resource_resolver` was invoked only after the prompt carried
  `station_catalog.status=ranked_metadata_only`, the filtered station list,
  and remaining station-resource search state. The tool sequence dropped from
  22 rows in `r34` to 14 rows in `r35`, and the resolver did not replay
  `ndp_stage_resource` or `ndp_filter_earthscope_station_catalog`.
- `r52` is accepted only as a grounded metadata-only acquisition blocker. It
  resolved the Bay Area coordinate/radius request, staged the EarthScope
  station metadata CSV from NDP, filtered 67 nearby stations, searched the
  ranked station set, and did not invent a station time-series CSV, analysis,
  or PNG.
- `r53` is rejected as a prompt-only terminal-state hardening result. It still
  issued repeated terminal `resource_discovery_search_exhausted` NDP search
  attempts after the ranked station set had already been covered.
- `r54` and `r55` show partial runtime improvement but are not clean terminal
  behavior. They preserve a grounded metadata-only answer, but still contain
  more than one terminal exhausted skip in the tool trace.
- `r56` is the current accepted live trace for the metadata-only acquisition
  branch. It used the live ALCF Sophia provider and NDP tools, filtered 67
  Bay Area station candidates around `37.77, -122.42` within 75 km, covered
  the ranked station candidates `UCSF, SBRB, SBRU, MHDL, EBMD`, returned no
  terminal `resource_discovery_search_exhausted` tool-loop skip, and produced
  a final answer that explicitly says no profiling or visualization ran because
  acquisition remained metadata-only.

Remaining blockers:

- The final synthesis stale-state bug observed in `r33`, resolver replay bug
  observed in `r34`, and repeated terminal exhausted-search behavior observed
  through `r55` are improved in `r56`. These are necessary
  runtime/blueprint semantics fixes, not sufficient full benchmark acceptance.
- `r56` still contains one non-terminal duplicate station-resource request for
  `SBRU`. The runtime gives typed feedback and the workflow recovers, but the
  pack should learn to avoid redundant non-terminal station permutations from
  `remaining_station_ids`.
- No accepted run has produced the full data acquisition -> analysis ->
  visualization -> synthesis workflow for this Bay Area mutation. The current
  best live evidence is a metadata-only acquisition blocker, not a scientific
  analysis artifact.
- Unit tests cover the state guards, but acceptance requires manual trace
  audit: tool arguments, selected resource provenance, staged file type, station
  geography, state transitions, final synthesis, and generated artifacts.

## 2026-06-05 ALCF Positive Depth-Topology Trace Review

The current positive-path coordinate/radius run was exercised repeatedly against
ALCF Sophia and live NDP/EarthScope tools:

- `trace-sophia-depth-topology-current-r57.jsonl`
- `trace-sophia-depth-topology-current-r58.jsonl`
- `trace-sophia-depth-topology-current-r59.jsonl`
- `trace-sophia-depth-topology-current-r60.jsonl`
- `trace-sophia-depth-topology-current-r61.jsonl`

The accepted live trace is `r61`; the earlier traces are retained as failure
evidence:

- `r57` reached real acquisition, profile, and plotting, but the final answer
  was an artifact-only status update. The rerendered stricter report rejects it
  for missing source URL, station-region provenance, profile/uncertainty
  evidence, and data/event limitations.
- `r58` kept the same acquisition path but proved prompt-only synthesis
  hardening was insufficient; typed source/provenance state still failed to
  reach the visible final answer.
- `r59` proved accumulated typed session state reached synthesis input, but the
  model still emitted a thin artifact-only answer. This shifted the fix from
  routing/prompting to final settlement over typed tool evidence.
- `r60` showed the new positive typed-state fallback working partially. It
  preserved station ID, CSV path, source URL, and limitation, but still failed
  because runtime extraction dropped profile, plot, and station-distance state
  from live observer tool rows.
- `r61` is the current accepted positive live trace. It ran the depth topology
  through `main -> geospatial -> ndp_dataset_discovery ->
  earthscope_station_catalog -> ndp_resource_resolver ->
  seismic_event_catalog -> gnss_timeseries_analysis ->
  station_network_analysis -> visualization -> synthesis`, with 13 successful
  NDP tool rows and observed semantic proofs for marketplace pack loading,
  root delegation, nested tier-3 routing, and synchronous parent returns.

Manual r61 evidence checks:

- Request: region centered at `34.05, -118.25` with a `75.0 km` radius.
- Station catalog: `earthscope_converted_data.csv` was staged from NDP and
  filtered to 113 candidates; selected station `MTA1` is `0.713 km` from the
  requested center, network `SCGN`, status `ACTIVE`.
- Source resource:
  `https://ds2.datacollaboratory.org/Earthscope_api_dec2024/raw_csv/MTA1.CI.LY_.30.csv`.
- Staged station CSV:
  `/home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30.csv`
  (`50,424,246` bytes). Header and sample rows contain
  `time,east,north,up,sigEE,sigNN,sigUU,qChannel`.
- Profile evidence: `250000` rows scanned; numeric/uncertainty-capable columns
  include `east`, `north`, `up`, `sigEE`, `sigNN`, and `sigUU`.
- Visualization artifact:
  `/home/jcernuda/clio-agent/.clio/artifacts/ndp-visualizations/MTA1_time_series.png`
  (`87,581` bytes), PNG `1400 x 672`, nonblank channel variance.
- Final answer includes the requested region, station provenance, source URL,
  staged CSV, profile evidence, PNG path, and the limitation that no independent
  live earthquake/event catalog evidence was included in the completed
  workflow.

Remaining caveat: r61 is one accepted positive coordinate/radius path, not full
benchmark closure. The Bay Area mutation remains a separate accepted
metadata-only blocker path, and future benchmark work still needs mutable
geography/resource runs plus broader scientific scenarios.

## 2026-06-05 Bay Area Mutable-Geography Recheck

After the r61 positive Los Angeles-coordinate path, the Bay Area mutation was
rerun to check the opposite branch: a changed coordinate/radius target where
NDP exposes nearby station metadata but not a concrete station time-series CSV
for the ranked candidates.

Evidence files:

- `trace-sophia-depth-topology-bay-area-mutation-r62.jsonl`
- `report-sophia-depth-topology-bay-area-mutation-r62.md`
- `trace-sophia-depth-topology-bay-area-mutation-r63.jsonl`
- `report-sophia-depth-topology-bay-area-mutation-r63.md`

Manual r63 evidence checks:

- Provider/runtime: ALCF Sophia, `argonne` provider,
  `openai/gpt-oss-120b`, marketplace blueprint
  `earthscope-gnss-region-depth`.
- Request: region centered at `37.77, -122.42` with a `75.0 km` radius.
- Route: `main -> geospatial -> ndp_dataset_discovery ->
  earthscope_station_catalog -> ndp_resource_resolver -> ... -> main`, with
  live semantic proofs for marketplace pack loading, root delegation, nested
  tier-3 routing, and synchronous parent return.
- Metadata acquisition: staged
  `/home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/earthscope_converted_data.csv`
  from
  `https://nationaldataplatform.org/catalog/dataset/811f0bcc-99e5-455c-bcf6-7c63c2634f41/resource/a420cc30-2262-423a-8c63-3ad8d91f2a8f/download/earthscope_converted_data.csv`.
- Station filtering: `ndp_filter_earthscope_station_catalog` found `67`
  nearby candidates within the requested radius. The nearest candidates
  included `UCSF` (`3.444 km`, `BARD`, `ACTIVE`), `SBRB` (`9.325 km`,
  `BARD`, `RETIRED`), `SBRU`, `MHDL`, and `EBMD`.
- Resource resolution: live NDP station-resource searches covered the ranked
  nearby station set (`UCSF`, `SBRU`, `SBRB`, `MHDL`, `EBMD`) and returned no
  concrete station CSV resource to stage.
- Final answer: correctly reported a metadata-only acquisition blocker, cited
  the metadata CSV/source URL, the requested center/radius, candidate count,
  searched station IDs, and explicitly stated that no profiling or
  visualization ran because acquisition was not analysis-ready.

Important correction from trace review: the apparent repeated search cycle in
the nested handoff evidence is evidence propagation through parent resumes, not
repeated live NDP tool calls. The live `tools_called` rows and semantic
`tool.call.*` events show one broad metadata discovery path plus one ranked
station-resource search pass. Tests were added only to guard the true failure
mode, where the same station-resource search is actually repeated in the live
tool rows.

Remaining caveat: r63 is an accepted mutable-geography blocker branch, not a
full analysis/visualization branch. The accepted positive branch remains r61.
The case now has two complementary pieces of live evidence: successful
acquisition/profile/plot/synthesis for a coordinate path with an available
station CSV, and truthful metadata-only blocker synthesis for a coordinate path
where NDP does not expose an analysis-ready station CSV for the ranked nearby
stations.

## 2026-06-05 Width-Topology Positive Recheck

The current dirty marketplace width topology was rerun after the latest
runtime/prompt hardening to make sure the older width evidence still holds
against the current pack:

- `trace-sophia-width-topology-current-r64.jsonl`
- `report-sophia-width-topology-current-r64.md`

Manual r64 evidence checks:

- Provider/runtime: ALCF Sophia, `argonne` provider,
  `openai/gpt-oss-120b`, marketplace blueprint
  `earthscope-gnss-region-width`.
- Request: region centered at `34.05, -118.25` with a `75.0 km` radius.
- Route shape: root-owned width topology, with `main` delegating directly to
  `geospatial`, `ndp_dataset_discovery`, `earthscope_station_catalog`,
  `ndp_resource_resolver`, `gnss_timeseries_analysis`,
  `station_network_analysis`, `visualization`, and `synthesis`.
- Station catalog: `earthscope_converted_data.csv` was staged from NDP and
  filtered to `113` candidates within the requested radius. Selected station
  `MTA1` is `0.713 km` from the requested center, network `SCGN`, status
  `ACTIVE`.
- Source resource:
  `https://ds2.datacollaboratory.org/Earthscope_api_dec2024/raw_csv/MTA1.CI.LY_.30.csv`.
- Staged station CSV:
  `/home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30.csv`
  (`50,424,246` bytes).
- Profile evidence: `ndp_profile_csv_resource` completed successfully over the
  staged CSV; final synthesis reported `250000` scanned rows and columns
  `time`, `east`, `north`, `up`, `sigEE`, `sigNN`, `sigUU`, and `qChannel`.
- Visualization artifact:
  `/home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30_timeseries.png`
  (`89,696` bytes). Manual image inspection shows nonblank east/north/up
  displacement traces.
- Final answer includes region, station provenance, staged CSV, source URL,
  profile evidence, PNG path, and the limitation that no independent live
  earthquake/event catalog evidence was included.

Caveat: the width run staged `MTA1.CI.LY_.30.csv` twice through cached
`ndp_stage_resource` calls: once in `earthscope_station_catalog` and once in
`ndp_resource_resolver`. This did not corrupt the result, but it is a remaining
prompt/runtime efficiency issue for width semantics. The root orchestrator
should prefer typed `acquisition.status=staged` evidence over asking another
child to restage the same resource.

## 2026-06-05 Domain-Grouped Topology Recheck

The domain-grouped topology was rerun to evaluate a third organization pattern:
`main` delegates spatial resolution, a `data` parent owns NDP/station/resource
children, an `analysis` parent owns profile/network children, and `main`
settles visualization plus synthesis.

Evidence files:

- `trace-sophia-domain-coordinate-current-r65.jsonl`
- `report-sophia-domain-coordinate-current-r65.md`
- `trace-sophia-domain-coordinate-post-reuse-r66.jsonl`
- `report-sophia-domain-coordinate-post-reuse-r66.md`
- `trace-sophia-domain-coordinate-stage-guard-r67.jsonl`
- `report-sophia-domain-coordinate-stage-guard-r67.md`
- `trace-sophia-domain-coordinate-stage-guard-r68.jsonl`
- `report-sophia-domain-coordinate-stage-guard-r68.md`

Manual evidence checks:

- Provider/runtime: ALCF Sophia, `argonne` provider,
  `openai/gpt-oss-120b`, marketplace blueprint `earthscope-gnss-region`.
- Request: explicit coordinate target centered at `34.05, -118.25` with a
  `75.0 km` radius.
- Route shape: `main -> geospatial -> main`, `main -> data -> ... -> main`,
  `main -> analysis -> ... -> main`, then `main -> visualization -> main` and
  `main -> synthesis -> main`. This preserves an explicit geospatial boundary
  before NDP acquisition and groups acquisition/analysis by domain.
- Station catalog: NDP station metadata was staged as
  `earthscope_converted_data.csv` and filtered to `113` nearby candidates.
  Selected station `MTA1` is `0.713 km` from the requested center, network
  `SCGN`, status `ACTIVE`.
- Source station resource:
  `https://ds2.datacollaboratory.org/Earthscope_api_dec2024/raw_csv/MTA1.CI.LY_.30.csv`.
- Staged station CSV:
  `/home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30.csv`
  (`50,424,246` bytes).
- Profile evidence: `250000` rows scanned, with required GNSS columns
  `time`, `east`, `north`, and `up`, plus uncertainty columns `sigEE`,
  `sigNN`, `sigUU`, and `qChannel`.
- Visualization artifact in the accepted r68 run:
  `/home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30_plot.png`
  (`85,697` bytes). Manual image inspection shows nonblank east/north/up
  displacement traces.
- Final answer includes region, station provenance, staged CSV, source URL,
  profile evidence, PNG path, and the limitation that no independent live
  earthquake/event catalog evidence was included.

Duplicate staging finding and fix:

- r65, r66, and r67 completed the end-to-end workflow but showed that the
  resolver attempted to stage the same `MTA1.CI.LY_.30.csv` after the
  station-catalog child had already staged it. Those duplicate calls were
  cache hits, so they did not corrupt artifacts, but they represented a real
  semantic weakness: a child was reacquiring data instead of respecting typed
  `acquisition.status=staged` state.
- Prompt-only resolver hardening was insufficient because the resolver's local
  ReAct row context did not reliably include the sibling child stage row.
- r68 is the accepted post-guard run. It still records a third
  `ndp_stage_resource` tool event, but that event is now typed as
  `_meta.status=skipped` with
  `_meta.reason=duplicate_station_resource_stage` and includes
  `clio_runtime.workflow_state.acquisition.status=staged`,
  `analysis_ready=true`, the exact local CSV path, and the source URL. This is
  no longer a second acquisition; it is a runtime idempotency observation over
  structured workflow state.

Remaining caveat: the model still chose to call the resolver staging tool after
the data parent already had an analysis-ready CSV. The runtime now prevents
duplicate acquisition, but future prompt/blueprint work should reduce the
unnecessary call itself by making child experts more explicit about when their
typed final output is meant for a parent.

## 2026-06-05 Domain Topology Trace-Review Hardening

Additional real-provider reruns were made because the benchmark PASS gate hid
two semantic failures that were only visible through trace review:

- `trace-sophia-domain-coordinate-resolver-boundary-r69.jsonl`
- `report-sophia-domain-coordinate-resolver-boundary-r69.md`
- `trace-sophia-domain-coordinate-tool-boundary-r70.jsonl`
- `report-sophia-domain-coordinate-tool-boundary-r70.md`
- `trace-sophia-domain-coordinate-state-authority-r71.jsonl`
- `report-sophia-domain-coordinate-state-authority-r71.md`

Trace review findings:

- r69 completed with real ALCF/NDP data and a valid PNG, but
  `earthscope_station_catalog` still searched and staged the station CSV before
  `ndp_resource_resolver`. That was scientifically usable but semantically
  wrong: station catalog should rank metadata, while resolver should own
  station-specific acquisition.
- The marketplace tool scopes were tightened after r69. Dataset discovery now
  owns broad catalog discovery and station metadata staging;
  `earthscope_station_catalog` now only has
  `ndp_filter_earthscope_station_catalog`; resolver owns station-specific
  search and station CSV staging.
- r70 exposed a different semantic failure: the geospatial expert authored a
  fake `catalog` workflow state with station IDs (`P041`, `CIV2`, `P056`) even
  though it has no data tools. Data then followed those invented station IDs.
  The run produced a real CSV and PNG, but it skipped the required NDP metadata
  catalog/filter evidence and was therefore rejected despite PASS.
- Runtime typed-state authority filtering was added so geospatial child returns
  can only contribute geographic state (`geospatial`, `region`, `center`,
  `bbox`) and cannot smuggle catalog, resource, or acquisition state into the
  parent.
- r71 is the accepted route after both fixes. Live and saved trace evidence
  show the intended sequence:
  `ndp_search_datasets` / `ndp_get_dataset_details` for the EarthScope station
  metadata dataset, `ndp_stage_resource` for
  `earthscope_converted_data.csv`, `ndp_filter_earthscope_station_catalog`
  yielding `113` regional candidates, resolver search for `MTA1`, resolver
  staging of `MTA1.CI.LY_.30.csv`, profile, plot, and synthesis.

r71 accepted evidence:

- Provider/runtime: ALCF Sophia, `argonne` provider,
  `openai/gpt-oss-120b`, marketplace blueprint `earthscope-gnss-region`.
- Request: explicit coordinate target centered at `34.05, -118.25` with a
  `75.0 km` radius.
- Station metadata path:
  `/home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/earthscope_converted_data.csv`.
- Filtered station candidates: `113`; selected station `MTA1`, network
  `SCGN`, status `ACTIVE`, `0.713 km` from the requested center.
- Station CSV source:
  `https://ds2.datacollaboratory.org/Earthscope_api_dec2024/raw_csv/MTA1.CI.LY_.30.csv`.
- Staged station CSV:
  `/home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30.csv`
  (`50,424,246` bytes).
- Profile evidence: required columns `time`, `east`, `north`, `up` plus
  `sigEE`, `sigNN`, `sigUU`, and `qChannel`; `250000` rows scanned with
  scan-limited caveat.
- Visualization artifact:
  `/home/jcernuda/clio-agent/.clio/artifacts/plot_MTA1_time_series.png`
  (`88,007` bytes). Manual image inspection shows nonblank east/north/up
  displacement traces.

Remaining caveat after r71: the final answer included an unsupported sampling
rate estimate derived from scan-limited profile rows. The analysis and
synthesis prompts were updated to forbid exact cadence claims unless a child
explicitly verifies cadence from adjacent time values or full-file evidence.

### 2026-06-05 r73 Accepted Trace After Cadence Guard

Accepted evidence files:

- `trace-sophia-domain-coordinate-cadence-guard-r73.jsonl`
- `report-sophia-domain-coordinate-cadence-guard-r73.md`

r73 is the current accepted EarthScope GNSS coordinate-mutation pipeline trace
for this case. It was run against the real ALCF Sophia provider with
`argonne/openai/gpt-oss-120b` and the workspace marketplace blueprint
`earthscope-gnss-region`.

Semantic route:

- `main -> geospatial`: resolved the explicit coordinate request to center
  `34.05, -118.25` with `75 km` radius. Runtime state-authority filtering kept
  geospatial output limited to geography state; it did not contribute catalog,
  resource, or acquisition state.
- `main -> data -> ndp_dataset_discovery`: searched NDP for EarthScope GNSS
  station metadata, fetched dataset details for
  `811f0bcc-99e5-455c-bcf6-7c63c2634f41`, and staged
  `earthscope_converted_data.csv`.
- `data -> earthscope_station_catalog`: used only
  `ndp_filter_earthscope_station_catalog` on the staged metadata CSV. It did
  not search or stage station time-series resources.
- `data -> ndp_resource_resolver`: searched for the selected station resource
  and staged `MTA1.CI.LY_.30.csv` from dataset
  `1b0c1b93-f164-4025-bd7b-000252b5ca18`.
- `main -> analysis -> gnss_timeseries_analysis`: profiled the staged station
  CSV and reported scan-limited evidence without converting `rows_scanned` into
  a sampling-rate or duration claim.
- `analysis -> station_network_analysis`: assessed MTA1 proximity and nearby
  backup stations from typed station-catalog evidence.
- `main -> visualization`: generated a PNG time-series artifact from the
  staged CSV.
- `main -> synthesis`: summarized source, station provenance, profile columns,
  artifact path, and the missing independent event-catalog limitation without
  unsupported cadence claims.

r73 accepted evidence:

- Station metadata path:
  `/home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/earthscope_converted_data.csv`.
- Filtered station candidates: `113`; selected station `MTA1`, network
  `SCGN`, status `ACTIVE`, `0.713 km` from the requested center.
- Station CSV source:
  `https://ds2.datacollaboratory.org/Earthscope_api_dec2024/raw_csv/MTA1.CI.LY_.30.csv`.
- Staged station CSV:
  `/home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30.csv`
  (`50,424,246` bytes).
- Profile evidence: columns `time`, `east`, `north`, `up`, `sigEE`, `sigNN`,
  `sigUU`, and `qChannel`; numeric summary over the first `5,000` rows; scan
  coverage reported as `250,000` rows with scan-limited caveat.
- Visualization artifact:
  `/home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30_plot.png`
  (`89,786` bytes). Manual image inspection shows nonblank east/north/up
  displacement traces.
- Final answer contained no model-derived `Hz`, sampling-rate, cadence, or
  duration claim. The only `1 Hz` evidence found in the trace was in the NDP
  dataset notes, not in the final answer or child analysis inference.

Remaining quality gap: the visualization uses raw epoch-like numeric values on
the x-axis. This is acceptable evidence that acquisition, analysis, and
artifact generation worked end to end, but it is not yet a polished public demo
plot. The next improvement should convert or label time axes in generated GNSS
plots while preserving exact source-column provenance.

### Current Agent Version: Domain-Grouped EarthScope GNSS Region Agent

Current blueprint: `earthscope-gnss-region`.

Observed/expected expert tree:

```text
Level 0
main
  tools: none
  role: orchestrates typed state, child evidence, and final route control

Level 1
main -> geospatial
  tools: none
  role: resolve requested geography into typed region/center/bbox state only

main -> data
  tools: none directly
  role: coordinate acquisition branches

main -> analysis
  tools: none directly
  role: coordinate profile and station-network interpretation

main -> visualization
  tools:
    - ndp_plot_csv_timeseries
  role: render plot artifacts from staged/analyzed CSV resources

main -> synthesis
  tools: none
  role: produce grounded final answer from typed state and child evidence

Level 2 under data
data -> ndp_dataset_discovery
  tools:
    - ndp_search_datasets
    - ndp_get_dataset_details
    - ndp_stage_resource
  role: find EarthScope metadata dataset and stage station metadata CSV

data -> earthscope_station_catalog
  tools:
    - ndp_filter_earthscope_station_catalog
  role: rank nearby stations from staged metadata; no search/stage authority

data -> ndp_resource_resolver
  tools:
    - ndp_search_datasets
    - ndp_stage_resource
  role: find and stage concrete station time-series CSV

Level 2 under analysis
analysis -> gnss_timeseries_analysis
  tools:
    - ndp_profile_csv_resource
  role: profile staged station CSV, columns, numeric ranges, scan caveats

analysis -> station_network_analysis
  tools: none
  role: assess station proximity, redundancy, and suitability from typed evidence

analysis -> seismic_event_catalog (optional)
  tools: none currently
  role: report event-catalog evidence or a capability gap only when the parent
        explicitly requests earthquake/event catalog context
```

Recent iteration history:

- r69: proved real ALCF/NDP data and PNG generation, but rejected because
  `earthscope_station_catalog` searched/staged station CSVs instead of only
  ranking metadata.
- r70: rejected because `geospatial` produced fake catalog/station workflow
  state even though it had no data tools.
- r71: accepted typed-state authority route after geospatial output was limited
  to geography state and station acquisition moved to resolver ownership.
- r72/r73: hardened analysis and synthesis prompts so scan-limited
  `rows_scanned` evidence is not converted into unsupported cadence/duration
  claims.
- r74: hardened the generic NDP plotting tool so epoch millisecond/second and
  datetime x-columns are rendered as readable time axes, with explicit
  `x_axis` metadata in tool results.

### 2026-06-05 r74 Accepted Trace After Time-Axis Tool Hardening

Accepted evidence files:

- `trace-sophia-domain-coordinate-timeaxis-r74.jsonl`
- `report-sophia-domain-coordinate-timeaxis-r74.md`

r74 keeps the r73 acquisition/analysis semantics and adds artifact-quality
evidence for visualization. The real provider run completed through the full
domain-grouped blueprint route and the `ndp_plot_csv_timeseries` tool returned:

```json
{
  "x_axis": {
    "kind": "epoch_milliseconds",
    "label": "time (UTC)",
    "parse_success_ratio": 1.0
  }
}
```

r74 evidence:

- Provider/runtime: ALCF Sophia, `argonne/openai/gpt-oss-120b`.
- Route: `main -> geospatial -> data -> ndp_dataset_discovery ->
  earthscope_station_catalog -> ndp_resource_resolver -> analysis ->
  gnss_timeseries_analysis -> station_network_analysis -> visualization ->
  synthesis`.
- Tool sequence: `ndp_search_datasets`, `ndp_get_dataset_details`,
  `ndp_stage_resource`, `ndp_filter_earthscope_station_catalog`,
  station-specific `ndp_search_datasets`, station CSV `ndp_stage_resource`,
  `ndp_profile_csv_resource`, and `ndp_plot_csv_timeseries`.
- Station CSV:
  `/home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30.csv`.
- Visualization artifact:
  `/home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30_plot.png`
  (`77,188` bytes). Manual image inspection shows a nonblank east/north/up
  displacement plot with UTC time ticks and `2024-Dec-03` date context.
- Final answer contained no model-derived `Hz`, sampling-rate, cadence, or
  duration claim.
