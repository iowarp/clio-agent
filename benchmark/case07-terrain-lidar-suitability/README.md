# Case 07: Terrain/Lidar Suitability And Artifact Generation

Status: not yet passed.

Issue checklist entry: `iowarp/clio-agent#628`.

Related reopened issue: `iowarp/clio-agent#617`.

## Benchmark Prompt Intent

Ask CLIO to assess whether terrain or lidar data supports a site-suitability
decision and produce an artifact or clear surfaced limitation.

## Expected Agent Blueprint

Primary pack: `terrain-suitability`, or a later researched replacement pack.

## Semantics To Prove

- Catalog/collection, terrain derivation, gridding, suitability, and visual
  summary as separate expert stages.
- Tool-grounded DEM/lidar/geospatial evidence.
- Artifact generation when data is available.
- Surfaced limitations when a source is unavailable, too large, or unsupported.

## Required Folder Evidence

Add the live run evidence required by `../CASE_EVIDENCE_CONTRACT.md` before
checking this case off.
