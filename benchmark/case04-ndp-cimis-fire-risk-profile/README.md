# Case 04: NDP CIMIS Fire-Risk Profile

Status: not passed.

## Prompt Intent

Ask CLIO to use NDP-hosted CIMIS weather data to profile conditions relevant to
fire spread or field operations for a requested California region.

## Semantics To Prove

- Catalog search finds CIMIS resources and selects a station/resource justified
  by geography or stated scope.
- CSV staging records exact source URL, selected resource name, row count, and
  bounds.
- Weather analysis profiles temperature, humidity, wind, missingness, and time
  range.
- Visualization produces a verified artifact.
- Parent answer ties the profile to the requested operational question without
  overclaiming fire-spread modeling.

## Required Expert Decomposition

- `main`: owns the operational question and merges station choice, weather
  profile, visualization, and caveats.
- `geospatial`: resolves the requested California region and provides station
  selection constraints.
- `catalog`: searches NDP for CIMIS datasets and enumerates candidate station
  resources.
- `station_selection`: chooses the best station/resource from location,
  metadata, availability, and row bounds. It must explain rejected candidates.
- `csv_profile`: stages and profiles the selected CSV, including row count,
  time span, missingness, units, and numeric ranges.
- `weather_analysis`: interprets humidity, wind, and temperature for the stated
  operational/fire-risk question.
- `visualization`: plots the selected variables and records the artifact path.

The benchmark is not valid if the agent happens to pick Fresno Station 80
without showing why that resource matches the requested region or question.

## Current Core Problem

The existing CIMIS run is closer to usable than the other NDP cases, but it must
prove station selection semantics and a meaningful analysis question, not just
CSV profile plus plot.
