# Case: NDP Snow-Depth from Differenced Lidar

Status: **candidate — grounding NOT yet done.** Independent of the wildfire
case (different domain: terrain/hydrology, not fire). Do the manual solve
before writing a confident spec — per our method, no case is real until the
question has been solved by hand against live data.

## The question (draft)

> How much snow is sitting on a given mountain basin this season, and where is
> it deepest? Show me a snow-depth map.

## Why it looks promising

- NDP org `opentopography` carries paired **snow-on** and **snow-off** lidar
  surveys of the same area (seen live: Southern Sierra Nevada Critical Zone
  Observatory snow-on/snow-off; Jemez River Basin snow-on/snow-off). Each pack
  has dozens of resources (GeoTIFF/TIFF).
- Differencing snow-on minus snow-off elevation → a snow-depth raster. That is
  a genuinely hard, fully grounded computation with a strong 3D/terrain visual.

## Grounding TODO (must do first)

1. Confirm matched snow-on/snow-off pairs for one basin (same footprint/grid).
2. Confirm the GeoTIFF resources are stageable at bounded size (opentopography
   tiles can be large — the wildfire case showed staging can hang; need a
   size/timeout strategy).
3. Verify alignment/resampling works and the differenced raster is sane.
4. Produce a real snow-depth map by hand; capture datasets/URLs/fields like
   `../ndp-wildfire-smoke-impact/manual-solution/DATASETS.md`.

Only after that: write the full spec, expected hierarchy, and pass criteria,
then build the CLIO agent.
