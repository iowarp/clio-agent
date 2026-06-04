# NDP Meeting Demo Playbook

This playbook is for live CLIO demos with NDP/EarthScope collaborators. The
goal is to run prompts in front of attendees and show the live execution trace,
not to replay static screenshots.

## Backend

Start CLIO:

```sh
uv run clio-agent-gact --host 127.0.0.1 --port 17831
```

Configure ALCF Sophia:

```sh
curl -sS -X PUT http://127.0.0.1:17831/v1/providers/lm \
  -H 'Content-Type: application/json' \
  -d '{"provider":"argonne","api_base":"https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1","model":"openai/gpt-oss-120b","temperature":0.2,"max_tokens":32000}'
```

Install marketplace packs from the checked-out submodule path when using the
benchmark harness:

```sh
--marketplace-source external/clio-agent-marketplace
```

## Proven Rehearsal Evidence

Combined four-case report:

- Report: `tmp/ndp-meeting-live-agent/ndp_demo_four_cases.md`
- Evidence JSONL: `tmp/ndp-meeting-live-agent/ndp_demo_four_cases.jsonl`
- Result: 4/4 per-case passes with ALCF/Sophia.
- Live semantic events: 109/109 live-observed across the four cases.

Render the combined report from existing rows:

```sh
uv run python scripts/run_demo_benchmark.py \
  --render-existing-jsonl tmp/ndp-meeting-seismic/marketplace_seismic_live.jsonl \
  --render-existing-jsonl tmp/ndp-meeting-live-agent/marketplace_ndp_wildfire_fresh.jsonl \
  --render-existing-jsonl tmp/ndp-meeting-live-agent/marketplace_ndp_warnings_iso.jsonl \
  --render-existing-jsonl tmp/ndp-meeting-live-agent/marketplace_ndp_cimis_resource_summary.jsonl \
  --output-jsonl tmp/ndp-meeting-live-agent/ndp_demo_four_cases.jsonl \
  --report tmp/ndp-meeting-live-agent/ndp_demo_four_cases.md \
  --lane marketplace_agents
```

## Demo 1: EarthScope Seismic Post-Analysis

### Rehearsal Prompt That Ran

This is the prompt that previously produced a passing rehearsal artifact:

```text
Using the active seismic waveform review agent, explore seismic activity over the last 7 days around the San Diego, California area. Start with NDP catalog discovery for relevant seismic waveform data; if NDP staging is blocked, recover with regional EarthScope discovery from the requested geography, inspect the staged SAC waveform, compute trace statistics, and produce a PNG plot artifact without using stale local files.
```

### Post-Analysis

This run should not be counted as a clean public benchmark pass. It proved that
CLIO could execute a marketplace blueprint, call NDP/EarthScope/SAC tools, stage
data, compute trace statistics, and generate a PNG artifact. It did not prove
the intended geographic EarthScope semantics.

Problems found after review:

- The prompt was over-specified. It named the active agent, NDP, EarthScope,
  SAC, recovery behavior, statistics, PNG output, and stale-file constraint.
  That makes it a scripted workflow check, not a natural collaborator request.
- Geography was handled in the wrong layer. The SAC/EarthScope tool resolved
  `San Diego` through built-in location hints instead of receiving a region
  object from a generic geospatial expert.
- SAC was treated as part of the discovery semantics. SAC is a waveform file
  format; it should be an optional format-analysis stage after region, event,
  station, and channel discovery are already correct.
- The run did not prove a `geospatial -> earthscope_catalog ->
  seismic_analysis -> visualization` hierarchy. It mostly proved the older
  `main -> data/ndp_catalog -> analysis/sac_format -> visualization` path.
- The successful artifact was not enough. A PNG only demonstrates that a
  waveform was plotted; it does not prove the region, dataset, event/station
  selection, or provenance semantics are correct.
- The case was too narrow. A real benchmark should tolerate changing the city,
  state, or explicit coordinates without relying on a San Diego-specific path.

### Corrected Benchmark Prompt

The prompt we should use after the review is:

```text
Explore recent seismic activity around the San Diego area. Resolve the requested geography, find public EarthScope or earthquake catalog evidence, analyze the events and station context, and produce an artifact suitable for discussion.
```

Flexible variants should be valid without changing the prompt structure:

- `Explore recent seismic activity around Los Angeles. Resolve the requested geography...`
- `Explore recent seismic activity around Anchorage. Resolve the requested geography...`
- `Explore recent seismic activity around 32.7157, -117.1611. Resolve the requested geography...`

### Corrected Benchmark Semantics

The corrected agent should prove this shape:

- `main`: owns the collaborator request and merges evidence.
- `geospatial`: input is a natural region, state, city, bbox, or explicit
  lat/lon; output is a provenance-bearing region object with center, bbox or
  radius, source, confidence, and warnings.
- `earthscope_catalog`: consumes the resolved region object and queries
  EarthScope/USGS event, station, or channel metadata. It should return
  CSV/tabular evidence where available.
- `seismic_analysis`: analyzes event distribution, magnitudes, depths,
  station/channel suitability, time coverage, uncertainty, and data limits.
- `visualization`: produces event/station maps or other discussion artifacts.
- `waveform_format`: optional leaf-stage. It may inspect SAC or another
  waveform format only after discovery/staging semantics are correct.

The run should fail the benchmark if:

- a domain-specific SAC/EarthScope tool resolves place names internally;
- the answer jumps straight from city name to one waveform plot;
- the final answer cannot cite region, event/station/channel, and artifact
  provenance from the trace;
- the city or region is changed and the workflow only works for the rehearsed
  San Diego path;
- the trace lacks distinct geospatial, catalog, analysis, and visualization
  evidence.

### Rehearsal Evidence

- JSONL: `tmp/ndp-meeting-seismic/marketplace_seismic_live.jsonl`
- Report: `tmp/ndp-meeting-seismic/marketplace_seismic_live.md`
- Artifact:
  `.clio-agent-artifacts/charts/sac_traces_earthscope_CI_BAR_--_BHZ_2026-05-29T021201.png`
- Direct regional evidence also produced
  `tmp/ndp-meeting-seismic/san_diego_regional_waveform.png`.

This evidence remains useful as runtime proof only. It is not a pass claim for
the corrected benchmark. The agent still needs to be rebuilt around the
geospatial -> EarthScope catalog -> seismic analysis hierarchy.

## Demo 2: Current Wildfires In California

Live prompt:

```text
Using the active NDP environmental hazards agent, build a live current-wildfire situational snapshot for California. Search the NDP catalog for USA Current Wildfires, inspect the dataset details, query the ArcGIS FeatureServer for current incident features with POOState = 'US-CA' or a California bbox, and persist compact feature evidence to .clio-agent-artifacts/ndp/current_wildfires_ca.json. Report source URLs, feature count, incident names/acres when available, and caveats.
```

What to watch:

- Blueprint: `ndp-environmental-hazards`
- Route: `main -> catalog -> geospatial`
- Tools: `ndp_search_datasets`, `ndp_get_dataset_details`,
  `ndp_query_arcgis_features`

Verified rehearsal:

- JSONL: `tmp/ndp-meeting-live-agent/marketplace_ndp_wildfire_fresh.jsonl`
- Report: `tmp/ndp-meeting-live-agent/marketplace_ndp_wildfire_fresh.md`
- Artifact: `.clio-agent-artifacts/ndp/current_wildfires_ca.json`
- Artifact size during rehearsal: 132,149 bytes.

Caveat: this is a live FeatureServer feature query. It does not run a fire
spread simulation.

## Demo 3: California NWS Watches And Warnings

Live prompt:

```text
Using the active NDP environmental hazards agent, search NDP for California NWS watches and warnings, inspect the dataset details, query its ArcGIS FeatureServer layer for up to 15 active warning/watch features intersecting California or using the dataset layer URL, and persist compact evidence to .clio-agent-artifacts/ndp/california_nws_warnings.json. Summarize event, severity, affected area, start/end timing, source URL, and data caveats.
```

What to watch:

- Blueprint: `ndp-environmental-hazards`
- Route: `main -> catalog -> geospatial`
- Tools: `ndp_search_datasets`, `ndp_get_dataset_details`,
  `ndp_query_arcgis_features`
- The persisted JSON includes normalized ArcGIS timestamp companions such as
  `Start_iso`, `End__iso`, and `Updated_iso`.

Verified rehearsal:

- JSONL: `tmp/ndp-meeting-live-agent/marketplace_ndp_warnings_iso.jsonl`
- Report: `tmp/ndp-meeting-live-agent/marketplace_ndp_warnings_iso.md`
- Artifact: `.clio-agent-artifacts/ndp/california_nws_warnings.json`
- Artifact size during rehearsal: 29,459 bytes.

Caveat: timing should be cited from the ISO fields in the artifact, not from
manual conversion of raw ArcGIS epoch milliseconds.

## Demo 4: CIMIS Fresno Fire-Weather Context

Live prompt:

```text
Using the active NDP environmental hazards agent, search NDP for CIMIS Hourly Data - Multiple Stations, inspect dataset details, stage the Fresno State station CSV rather than the combined archive, profile the CSV, and create a PNG time-series artifact at .clio-agent-artifacts/ndp/cimis_fresno_weather.png using Date on the x-axis and Air Temp (C), Rel Hum (%), and Wind Speed (m/s) as y columns. Report staged path, row count, numeric ranges, source URL, and what the plot can and cannot prove for fire-weather context.
```

What to watch:

- Blueprint: `ndp-environmental-hazards`
- Route: `main -> weather_analysis -> visualization`
- Tools: `ndp_search_datasets`, `ndp_stage_resource`,
  `ndp_profile_csv_resource`, `ndp_plot_csv_timeseries`
- The staged source URL should be the station CSV:
  `https://f3i-supercomputing.s3.us-east-2.amazonaws.com/data/stations/80-fresnostate.csv`

Verified rehearsal:

- JSONL: `tmp/ndp-meeting-live-agent/marketplace_ndp_cimis_resource_summary.jsonl`
- Report: `tmp/ndp-meeting-live-agent/marketplace_ndp_cimis_resource_summary.md`
- Staged CSV:
  `tmp/clio-ndp-staging/CIMIS_Station__80___Fresno_State_Hourly_Weather_Data__2010_2025`
- Rows profiled: 131,520.
- Numeric ranges observed:
  - Air Temp: -2.0 to 36.9 C
  - Relative Humidity: 16.0 to 97.0 percent
  - Wind Speed: 0.4 to 7.2 m/s
- Artifact: `.clio-agent-artifacts/ndp/cimis_fresno_weather.png`
- Artifact size during rehearsal: 256,838 bytes.

Caveat: the plot can support fire-weather context discussion. It cannot prove
ignition, spread, fuel state, or fire occurrence by itself.

## Evidence Standard

A live demo only counts if all of these are true:

- The active Agent Blueprint is visible in the trace.
- The child expert route matches the workflow being demonstrated.
- Live tool calls prove search/query/stage/profile/plot work happened.
- Any persisted JSON, SAC, CSV, or PNG exists on disk and is non-empty.
- The final answer cites tool-grounded source URLs and artifact paths.
- The JSONL report is saved for post-meeting trace review.
