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
- EarthScope/USGS discovery consumes the resolved region instead of resolving
  place names internally.
- The first evidence layer is proper CSV/tabular event, station, channel, or
  metadata evidence where available.
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
  EarthScope/USGS event, station, or channel metadata. It should return
  CSV/tabular evidence when available.
- `seismic_analysis`: analyzes event distribution, magnitudes, depths,
  station/channel suitability, time coverage, and uncertainty.
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
