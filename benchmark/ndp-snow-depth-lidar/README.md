# Case: NDP Snow-Depth from Differenced Lidar

Status: **grounded by hand 2026-11-17 (see Grounding); not yet passed by an agent.** Independent of the wildfire
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
   the former wildfire case's `manual-solution/DATASETS.md` (removed 2026-10;
   in git history under `benchmark/ndp-wildfire-smoke-impact/`).

Only after that: write the full spec, expected hierarchy, and pass criteria,
then build the CLIO agent.


## Grounding (2026-11-17, by hand, with clio-kit geoservices and geoprocess)

Done against the live NDP test catalog and OpenTopography's public raster store,
with the tools an agent has (`describe_geo_service`, `read_raster_window`,
`raster_difference`), not with a separate script.

1. **Matched pairs exist.** Organization `opentopography` lists snow-on and
   snow-off bare-earth models for the Southern Sierra Nevada CZO (snow-on
   2010-03-14 to 03-24, `SSCZO_Mar10_be.vrt`; snow-off 2010-08-05 to 08-15,
   `SSCZO_Aug10_be.vrt`), the Jemez River Basin (snow-on 2010-03-27 to 04-03,
   snow-off 2010-06-29 to 07-08) and Boulder Creek (three snow-on dates in May
   2010, snow-off August 2010). Each dataset appears twice in the catalog under
   different ids. Both SSCZO mosaics are EPSG:26911 at 1 m, with no vertical
   datum declared and no units in the files.
2. **Bounded reads work.** Each mosaic is read only inside a region, through
   its VRT; no tile is downloaded whole. A 200,000-cell window took 52 to 94 s
   per survey (the VRTs' `.img` tiles have no overviews, so a decimated window
   still reads the full-resolution blocks).
3. **Alignment and differencing.** Both windows were read onto the same grid
   (snow-off resampled onto snow-on's grid by `raster_difference`); the tool
   flagged the undeclared vertical datum.
4. **Result** (snow-on minus snow-off, cells ~4.8 to 5.6 m after decimation):

| Site (region, degrees) | Elevation | Median depth | 5th to 95th percentile | Negative cells | Net volume |
| --- | --- | --- | --- | --- | --- |
| Wolverton, -118.74, 36.585, -118.71, 36.60 | ~2,150 to 2,660 m | 2.02 m | 1.26 to 2.74 m | 0.0% | 9.3 million m³ |
| Providence Creek, -119.21, 37.05, -119.18, 37.07 | ~1,640 to 2,030 m | 0.90 m | 0.27 to 1.89 m | 0.2% | 5.8 million m³ |

Depth rising with elevation is the expected pattern. Plowed roads and a
parking area at Wolverton read near zero (`manual-solution/snow-depth-ssczo-2010.png`).

**Pass criteria for an agent run** (plain request, e.g. the draft question
above or "How deep was the snow in the Southern Sierra Critical Zone
Observatory in March 2010? Show me a snow-depth map."):

- finds a matched snow-on/snow-off pair on NDP and says which surveys and dates;
- reads only the region (no whole-dataset download), at a stated resolution;
- differences snow-on minus snow-off bare earth on one grid and states the
  vertical-datum assumption;
- reports depth statistics within 0.2 m of the medians above for the same
  region and resolution, the share of negative cells, and the dates;
- shows a snow-depth map and states the limits (vegetation, steep slopes,
  decimation, no density so no SWE).
