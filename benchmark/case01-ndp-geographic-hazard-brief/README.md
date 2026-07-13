# Case 01: NDP Geographic Hazard Brief

Status: not passed.

## Prompt Intent

Ask CLIO for a live hazard brief for a city, county, or state that can change at
demo time. The prompt should use natural geography, not coordinates or internal
tool names.

Example:

```text
Build a current environmental hazard brief for the San Diego area. Find relevant
NDP datasets, resolve the geography, query bounded live features where possible,
and produce a compact artifact I can inspect.
```

## Semantics To Prove

- Generic place/state resolution to a provenance-bearing region object.
- NDP catalog discovery and candidate selection.
- Bounded feature query or staged resource with source URL and limits.
- Geospatial/hazard analysis separate from catalog search.
- Parent synthesis with caveats and an inspectable JSON or map artifact.

## Required Expert Decomposition

- `main`: owns the user question, asks for the needed branches, and merges the
  evidence.
- `geospatial`: turns "San Diego area", "California", or another requested
  place into a region object with center, bbox or radius, source, confidence,
  and limitations.
- `catalog`: searches NDP and ranks datasets against the resolved region and
  hazard intent.
- `hazard_features`: queries or stages bounded feature/resource data and
  validates timestamps, geometry, and key fields.
- `visualization` or `artifact`: writes compact JSON/GeoJSON or a map artifact
  with provenance.

The `catalog` expert must not do geocoding. The `hazard_features` expert must
not silently choose geography. The parent answer must show how the region,
dataset, feature query, and artifact connect.

## Current Core Problem

Existing NDP hazard demos are too shallow. They prove a feature query can run,
but not that CLIO owns geography, candidate selection, analysis, and provenance
as a coherent hierarchy.

The current benchmark must be considered invalid if it only proves
`ndp_search_datasets -> ndp_query_arcgis_features`. That is a tiny use case, not
a realistic collaborator workflow.
