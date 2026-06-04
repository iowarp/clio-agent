# Case 02: EarthScope CSV Seismic Geography

Status: not passed.

## Prompt Intent

Ask CLIO to investigate recent seismic activity around a user-provided U.S.
place or region. The prompt should not mention SAC or internal waveform tools.

Example:

```text
Explore recent seismic activity around the San Diego area. Resolve the requested
geography, find public EarthScope or earthquake catalog evidence, analyze the
events and station context, and produce an artifact suitable for discussion.
```

## Semantics To Prove

- Generic geospatial resolver emits center, bbox or radius, confidence, and
  source provenance.
- EarthScope/USGS/NDP discovery consumes the resolved region instead of
  resolving place names internally.
- The first evidence layer is proper CSV/tabular event, station, channel, GNSS,
  or metadata evidence where available.
- Waveform/SAC handling is optional and only follows correct discovery and
  staging. SAC is not the benchmark target.
- Analysis explains event distribution, station/channel suitability, data
  limitations, and provenance.

## Required Expert Decomposition

- `main`: owns the collaborator question and merges geography, seismic catalog,
  station/channel, analysis, and artifact evidence.
- `geospatial`: converts the input region into coordinates or coordinate range.
  Input: natural place, state, county, bbox, or explicit lat/lon. Output:
  region object with center, bbox or radius, source, confidence, and warnings.
- `earthscope_catalog`: consumes only the resolved region object and queries
  EarthScope/USGS/NDP event, station, channel, or GNSS metadata. It should
  return CSV/tabular evidence when available.
- `seismic_analysis`: analyzes event distribution, magnitudes, depths,
  station/channel or GNSS station suitability, time coverage, displacement
  ranges, and uncertainty.
- `waveform_format`: optional. It may inspect SAC or another waveform format
  only after the catalog/staging path has produced valid waveform input.
- `visualization`: produces event/station maps or waveform plots when the data
  supports them.

The geospatial expert is mandatory. The EarthScope expert must not parse city
names or use built-in location hints. SAC-specific tooling is a leaf-stage
format handler, not the discovery or geography layer.

## Current Core Problem

The previous seismic demo used a SAC/EarthScope tool with built-in location
hints. That is a semantic shortcut. This case fails until geography is a generic
capability and EarthScope discovery is driven by the resolved region object.

The case is also not complex enough if it stops at "download one waveform and
plot it." The public benchmark must show region resolution, event/station
metadata selection, analysis, provenance, and only then optional waveform work.

## Hierarchy Semantics To Compare

This case should be used to compare hierarchy semantics, not just final
answers. The same scientific workflow can be expressed as depth, width, or
domain grouping. Those choices test different CLIO capabilities.

### Depth Semantics

```text
main
-> geospatial
-> ndp_dataset_discovery
-> earthscope_station_catalog
-> ndp_resource_resolver
-> seismic_event_catalog
-> gnss_timeseries_analysis
-> station_network_analysis
-> visualization
-> synthesis
```

This layout tests whether CLIO can preserve typed evidence through a long
dependency chain. It is easy to inspect and friendly to smaller models, but it
is brittle: one bad upstream choice contaminates every downstream step, and
independent evidence streams are forced into sequence.

### Width Semantics

```text
main
├─ geospatial
├─ ndp_dataset_discovery
├─ earthscope_station_catalog
├─ ndp_resource_resolver
├─ seismic_event_catalog
├─ gnss_timeseries_analysis
├─ station_network_analysis
├─ visualization
└─ synthesis
```

This layout tests fanout, parallel evidence gathering, and merge quality. It
should expose whether CLIO can coordinate independent expert branches and catch
inconsistencies. It also increases orchestration burden: without strict
contracts, branches may duplicate work or use inconsistent assumptions.

### Domain Semantics

```text
main
├─ data
│  ├─ geospatial
│  ├─ ndp_dataset_discovery
│  ├─ earthscope_station_catalog
│  └─ ndp_resource_resolver
├─ analysis
│  ├─ seismic_event_analysis
│  ├─ gnss_timeseries_analysis
│  └─ station_network_analysis
├─ visualization
│  ├─ regional_map
│  └─ gnss_timeseries_plot
└─ synthesis
```

This is the preferred production target because it matches reusable CLIO
capability domains: data search/acquisition, scientific analysis,
visualization, and final synthesis. It should make tool permissions and
blueprint reuse cleaner across benchmark cases.

The risk is that a broad `data` expert can hide the actual semantics. For this
case, geospatial may live under `data`, but it must remain a distinct mandatory
boundary. The benchmark must fail any trace where NDP or EarthScope discovery
quietly parses `San Diego` internally and starts querying resources without an
explicit region object.

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
