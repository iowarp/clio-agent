# Case 10: Terrain/Lidar Suitability

Status: not passed.

## Prompt Intent

Ask CLIO to assess terrain or lidar suitability for a field/science decision
and produce a visual or structured artifact when data supports it.

## Semantics To Prove

- Catalog/staging and terrain/lidar derivation are separate stages.
- Gridding or slope/terrain metrics are grounded in data.
- Suitability criteria are explicit and tied to the user's decision.
- Visualization or structured artifact is verified.

## Required Expert Decomposition

- `main`: owns the suitability decision and merges evidence.
- `geospatial`: resolves the area of interest and coordinate assumptions.
- `catalog`: finds DEM/lidar/terrain candidates and checks access/size.
- `terrain_derivation`: computes slope, roughness, elevation, or other metrics.
- `suitability`: applies explicit decision criteria to derived metrics.
- `visualization`: creates a map, raster summary, or structured artifact.

The case must not be a generic terrain summary. It needs a decision target and
data-derived suitability evidence.

## Current Core Problem

The previous terrain pack is scaffold-like. It must become a realistic spatial
analysis workflow or be replaced.
