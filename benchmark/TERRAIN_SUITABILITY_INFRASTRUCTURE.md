# Terrain Site Suitability Infrastructure

Issue: iowarp/clio-agent#602

Benchmark source: Case 10, terrain site suitability from lidar/DEM through NDP.

## What Changed

CLIO now has reusable terrain tools for the benchmark's two delivery forms:

- `terrain_dem_terrain` analyzes a ready DEM grid for elevation, slope,
  aspect, and slope/elevation suitability masks.
- `terrain_pointcloud_read` reads x/y/z point-cloud data, grids it into a
  DEM-like surface, and can write that DEM as CSV for downstream analysis.
- Both tools validate file access through CLIO file policy and bound input size.
- Both tools support dependency-light CSV/NPY/NPZ fixtures for tests and local
  demos. GeoTIFF and LAS/LAZ inputs return structured optional-dependency
  guidance when `rasterio` or `laspy` are not installed.

This gives marketplace terrain experts a real substrate for conditional
delegation: use the direct DEM path when a ready raster/grid is available, or
use the point-cloud gridding path before deriving terrain when only raw x/y/z
points are delivered.

## Evidence Added

Focused tests cover:

- direct DEM suitability analysis with elevation and slope constraints;
- point-cloud CSV ingestion, gridding, and DEM output writing;
- running the generated DEM through downstream terrain analysis;
- gateway exposure through `terrain_dem_terrain` and
  `terrain_pointcloud_read`;
- structured optional-dependency responses for GeoTIFF/LAS paths when
  `rasterio` or `laspy` are unavailable;
- tool-catalog ownership and visibility for terrain derivation.

## Not Claimed Yet

This is not the final real-provider end-to-end terrain benchmark result. The
manual demo-readiness run still needs to instantiate a Terrain Suitability
marketplace pack, search/select bounded NDP/OpenTopography data, exercise the
DEM-only and point-cloud-to-gridding branches when available, and inspect live
semantic logs/artifacts for the expected hierarchy.
