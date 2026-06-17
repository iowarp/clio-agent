# Case 03: NDP Wildfire And Weather Fusion

Status: not passed.

## Prompt Intent

Ask CLIO to combine current wildfire features with weather or warning context
for a region. The prompt should support swapping the city/state in front of a
collaborator.

## Semantics To Prove

- Geography resolution independent of fire/weather tools.
- NDP catalog search for wildfire and weather/warning resources.
- Parallel or sequential expert work for fire features and weather context.
- Merge step that reconciles timestamps, spatial scope, and missing fields.
- Artifact with compact feature/weather evidence and clear caveats.

## Required Expert Decomposition

- `main`: decomposes into region, fire, weather, and synthesis branches.
- `geospatial`: resolves the requested geography and emits a reusable region
  object.
- `fire_catalog`: finds current wildfire datasets and validates suitability.
- `weather_catalog`: finds warnings, station, forecast, or gridded resources
  relevant to the same region.
- `fire_features`: queries active incidents and validates geometry, timestamp,
  acres, status, and missing fields.
- `weather_analysis`: analyzes weather or warning context relevant to fire
  spread without pretending to run a fire-spread model.
- `fusion`: reconciles spatial/time coverage and produces a risk/caveat brief.

The case must use at least two data branches. A wildfire-only query is not a
pass.

## Current Core Problem

Current wildfire evidence is a single live feature query. It does not yet prove
multi-source fusion or robust region-driven behavior.
