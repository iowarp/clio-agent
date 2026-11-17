# NDP readiness ledger

Working ledger for the campaign that follows the 2026-11-17 analysis "CLIO for NDP:
readiness per organization and across organizations". Branch in every repo:
`claude/ndp-data-access`. Newest entries go at the end of each section.

## Status

| Area | State |
| --- | --- |
| 1. Catalog profile (test and production) | Done. See [Catalog profile](#catalog-profile-2026-11-17) |
| 2. Access layer in clio-kit (services, bounded reads, rasterio extra) | Done on clio-kit `claude/ndp-data-access`: new `geoservices` server (`d3fe70a`), terrain 2.3.0 reads GeoTIFF and LAZ by default (`c77a9cf`). Not yet in a released clio-kit, so CLIO's pinned clio-kit does not ship it |
| 2. "Tiles covering a region" in the clio-agent attach model | Done on this branch: `connected_data_ndp_tiles_covering`, plus a warning for datasets over 1,000 resources. The marketplace `ndp` plugin must still claim the tool (blocked) |
| 3. Core geospatial operations (clio-kit) | Next |
| 4. Discovery skill and shared `resolve-region` (marketplace) | Not started (repository now attached) |
| 5. Harmonization and recorded merge | Not started |
| 6. Map with time for rasters and vectors | Not started; depends on the earth-ocean component |
| 7. Domain skills (marketplace) | Not started (repository now attached) |
| Qualification runs | Not started |

## Needs user

- ~~Repository access~~ resolved 2026-11-17: the user allowed attaching clio-kit and
  clio-agent-marketplace; both are on `claude/ndp-data-access`.
- **NDP sign-in** (device flow on the idp-test realm), for any run that touches a
  protected resource. None of the work so far needed it.

## Catalog profile (2026-11-17)

Method: CKAN `package_search` against both catalogs, faceted counts, and the 40
most recently modified datasets of each organization (all of them where the
organization has fewer). Location and time fields were counted in both top-level
keys and `extras`. The script and raw output are disposable session scratch, removed when the session
ends; every number is reproducible from the CKAN URLs below.

### Counts

| | Test (`ndp-test.sdsc.edu/catalog`) | Production (`nationaldataplatform.org/catalog`) |
| --- | --- | --- |
| Datasets | 7,194 | 6,260 |
| USGS 3DEP | 2,808 | 3,413 |
| EarthScope Consortium | 1,117 | 1,119 |
| CA Wildfire and Forest Resilience Task Force | 854 | **absent**; the same sparcal.sdsc.edu layers appear as `california-landscape-metrics` (231) |
| OpenTopography | 542 | 271 |
| bco_weather | 170 | **absent** |
| Cal OES | 129 | 130 |
| WFSI | 90 | 90 |
| NIFC | 61 | 58 |
| TRPA | 51 | 51 |
| LANDFIRE / CA Forest Observatory | 5 / 10 | 7 / 9 |
| Top formats | GeoJSON 3,981, CSV 1,381, PNG 1,123, HTML 942, WMS 836, WCS 767, GeoTiff 752 | GeoJSON 2,699, HTML 1,783, CSV 1,421, PNG 1,129, GeoTIFF 953 (+187 "GeoTiff"), ZIP 296, WMS 250, WCS 202 |

The analysis's test-catalog counts are confirmed exactly. The two catalogs are not
the same collection: production lacks the Wildfire Task Force and bco_weather
organizations and halves OpenTopography, but holds more 3DEP DEM tiles and a set of
organizations absent from test (burnpro3d, vibrant-planet, hydroframe, NAIP,
nasa-firms). The CLIO default catalog is production, so any acquisition check must
state which catalog it used.

### Per-organization access matrix

"Service or format" is what the sampled records really carry; "Location" and "Time"
name the field that holds it. "Probe" is a live request from this container on
2026-11-17.

| Organization | Service or format (host) | Location | Time | Probe | Tool needed |
| --- | --- | --- | --- | --- | --- |
| USGS 3DEP (test) | GeoJSON footprint (wifire-data.sdsc.edu), Entwine viewer link, some DEM GeoTIFF tiles (prd-tnm S3), 1 to 22 resources | `spatial` 39/40, `dataBbox` 4/40 | `temporal` 36/40 | Viewer link encodes an EPT root (`s3-us-west-2.amazonaws.com/usgs-lidar-public/<project>`). `CA_PlacerCo_2012/ept.json` returns 200 (4.96 billion points, EPSG:3857). `TX_BurnetCo_2006/ept.json` returns 404: the project is gone from the bucket, so a catalog link can be stale | EPT subset by bounds (PDAL `readers.ept`, or laspy over the EPT hierarchy); stale-link reason |
| USGS 3DEP (prod) | DEM GeoTIFF tiles on prd-tnm S3, **up to 1,048 per dataset** | `spatial` and `dataBbox` 40/40 | `issueDate` only | — | Tile selection by region |
| EarthScope | One CSV, PNG and dashboard per station (ds2.datacollaboratory.org) | `spatial` 33/40 | `start_time`/`end_time`/`temporal` | 1,111 of 1,117 test records span 2024-12-03 to 2024-12-12 (the rest end 12-09 or 12-10); production: 1,116 of 1,119 | Existing earthscope/geodesy plugins |
| Wildfire Task Force (test) | ArcGIS Feature Service (services1.arcgis.com), GeoServer WMS/WFS/WCS on sparcal.sdsc.edu, STAC, GDB, GeoTIFF | `spatial` 39/40 | Almost none (`startDateTime` 1/40); vintage sits in `data_vintage` | sparcal WCS 2.0.1: 528 coverages. `rrk_sn__TotalFuelLoad_2021_30m` subset to the Tahoe box returned a 2409×1448 30 m EPSG:3310 GeoTIFF in 2.2 s. **The GeoTIFF carries no units**; units are only in the record's `data_units` extra | WCS subset; units from catalog extras |
| OpenTopography | 20 to 2,084 tiles per dataset on opentopography.s3.sdsc.edu: `.TIF`, ArcGrid `.adf` (format left blank) and `.img`, plus `_be.vrt` / `_hh.vrt` mosaics | `spatial` 40/40 | `temporal` 40/40 | `TAHOE_be.vrt` (68,172×139,824 px, 0.5 m, EPSG:26910) read through GDAL `/vsicurl`: a 2×2 km window at Emerald Bay returned elevations of 1,928 to 2,790 m in 11.6 s with no full download. The VRT sources are `.img` tiles, so a reader that restricts `/vsicurl` extensions must allow them. Tahoe searches return each OpenTopography dataset **twice** (duplicate records) | VRT/COG windowed read; tile selection by region |
| bco_weather (test only) | One ISO XML per dataset, on bco-dmo.org | none | none | — | Metadata only; data is at BCO-DMO |
| Cal OES | ArcGIS Hub downloads (GeoJSON, CSV, KML, ZIP), Esri REST, OGC WMS/WFS (NOAA idpgis, USGS earthquake) | `spatial` 39/40 | none | — | ArcGIS query; WFS |
| WFSI | Files on wfsi-data.org, **format blank on all 1,241 sampled resources**, up to 544 per dataset | `spatial` and `dataBbox` 38/40 | `issueDate` | — | Plain HTTP files; format from extension |
| NIFC | ArcGIS Hub, GeoServices REST (services3.arcgis.com) | `spatial` on 29/40, but 11 have none and the rest are world, US or Pacific-wide boxes | `issueDate` 3/40 | WFIGS Interagency Perimeters FeatureServer layer `extent` is malformed (degrees mixed with projected metres), so it cannot be used for coverage. A bbox count query does work: **21 perimeters** intersect the Tahoe box | ArcGIS query with envelope; coverage from a count query, not the declared extent |
| TRPA | ArcGIS Hub downloads plus Esri REST on maps.trpa.org | `spatial` 40/40 | none | Of 50 Hub GeoJSON links, **28 failed** (20 HTTP 400, 4×500, 3×404, 1×502); **51 of 51** Esri REST layer URLs answer. maps.trpa.org lists 136 services, including Wildlife_Habitat, Fish_Habitat, Soils_and_Land_Capability, Streams_and_Flood_Zone | ArcGIS MapServer/FeatureServer query |
| LANDFIRE | Links to landfire.gov pages; the "GeoTIFF" resource is the web viewer and the "WCS" resource is an HTML page | none | none | — | Formats are mislabelled; needs the real LANDFIRE WCS/ImageServer address |
| CA Forest Observatory | TIFF on storage.googleapis.com | `spatial` 9/10 | `temporal` 9/10 | — | COG windowed read |

### Facts re-checked from the analysis

- Keyword `tahoe` on test: **61** datasets: TRPA 51, OpenTopography 8, USGS 3DEP 1 and
  marketplace-models 1. Confirmed.
- `ext_bbox=-120.30,38.70,-119.80,39.35` returns **7,194**, the whole catalog: the
  catalog has no working spatial search. Confirmed.
- "NIFC: no spatial extent in metadata" is half right: most NIFC records do have
  `spatial`, but it is a world or national box, which is no more use for coverage.
- EarthScope 9-day, 1 Hz window: confirmed on both catalogs.
- "TRPA downloads failed (HTTP 400 and 404)": reproduced today for 28 of 50 GeoJSON
  links, so it is not a one-off.

## Decisions

- **D1 (2026-11-17). Coverage comes from the service, never from the declared extent
  alone.** NIFC's layer extent is malformed and its records' `spatial` is world-wide,
  yet a bbox count query answers the question exactly. Coverage checks therefore ask
  the data (query with an envelope, a WCS subset, a raster header) and report
  "undetermined" when that is not possible, rather than trusting a box.
- **D2. Units come from catalog extras.** sparcal WCS GeoTIFFs carry no units; the
  Wildfire Task Force records hold them in `data_units`. Harmonization must read
  them from the record.
- **D3. Tile selection by region belongs in the clio-agent attach model**, beside
  `connected_data_search_ndp`, because only that layer holds the server-observed
  resource IDs that `connected_data_connect` accepts. Tile footprints come from the
  dataset's own VRT when it has one (one request for all tiles), otherwise from
  each tile's raster header. Tiles whose footprint cannot be read are reported as
  undetermined, never dropped.

- **D4. The 1,000-resource cap was a silent truncation.** Production 3DEP datasets
  carry up to 1,048 tiles and OpenTopography up to 2,084; the search dropped the
  rest without saying so. The cap stays (it is also the selection limit) and the
  search now warns with the real count.

- **D5. The access layer is one new clio-kit server, `geoservices`, not extensions
  to `geo` and `terrain`.** `geo` already exposes 11 tools (the curation rule is 5
  to 7) and is about drawing GeoJSON; `terrain` is local analysis with a light
  dependency set. Service access needs GDAL (rasterio), pyproj and laspy with the
  lazrs decoder, which stay isolated in their own server environment. Five tools:
  `describe_geo_service`, `fetch_features`, `read_raster_window`,
  `read_point_cloud`, `search_stac`. PDAL is not used: it has no wheels, so a
  launcher install could not include it. EPT is read directly (octree hierarchy
  plus LAZ nodes through laspy) and COPC through laspy's reader.
- **D6. Point-cloud and raster reads stop at a budget and say so.** EPT reads the
  deepest octree level that fits `max_points` and returns `complete: false` with
  the reason; raster windows are decimated to `max_cells` and return the factor.
  Neither silently returns a thinned result as if it were full density.

## Runs

### R1 (2026-11-17): tile selection against live catalogs (mechanism, not agent judgement)

Direct calls of `tiles_covering` on datasets built from live CKAN records, outside
an agent. This verifies the mechanism only.

| Dataset (catalog) | Region (W, S, E, N) | Result | Time |
| --- | --- | --- | --- |
| Lake Tahoe Basin Lidar (test), 174 resources | -120.13, 38.94, -120.11, 38.96 (Emerald Bay) | 6 of 171 tiles cover it (he/hh/int `38120H11`, `38120H22`), 165 outside, 0 undetermined, from three VRTs (58, 58 and 55 sources) | 2.9 s |
| IML CZO Sangamon River (test), 60 TIF tiles | -88.5, 40.1, -88.45, 40.15 | 10 covering, 50 outside, from two VRTs | 1.6 s |
| Niobrara River (test), 20 ArcGrid `.adf` files | outside the data | 20 outside; the VRT names `NE15_Alexander_be/*.adf`, which first left all 20 undetermined until glob sources were mapped to their directory | 1.6 s |
| 3DEP 1-m DEM CA_UpperSouthAmerican_Eldorado_2019_B19 (prod), 98 tiles, no VRT | -120.10, 38.75, -120.05, 38.80 | Two header pages (64 + 34 at the earlier per-call budget): 2 covering (`x75y430` in UTM 10, `x23y430` in UTM 11), 96 outside, 0 unreadable. The record's `dataBbox` is a 2.0° × 1.6° box, so it alone would have selected every tile | 3.1 s + 2.0 s |

Bounded-read probes from the same container: a 2 × 2 km window of `TAHOE_be.vrt`
(0.5 m) returned elevations of 1,928 to 2,790 m in 11.6 s; a WCS subset of the
Sierra Nevada total fuel load to the Tahoe box returned 2409 × 1448 cells at 30 m
in 2.2 s (mean 13.6, max 55.8, units only in the record's `data_units`).

## Questions for NDP

| Question | Evidence so far | Answer |
| --- | --- | --- |
| Do national layers expose their extent through their services? | NIFC FeatureServer: declared extent malformed, bbox query works. sparcal WCS: `DescribeCoverage` gives a correct EPSG:3310 envelope. LANDFIRE records link HTML pages, not a service | Partly answered: yes for sparcal WCS, effectively no for NIFC's declared extent (but a query works), unknown for LANDFIRE |
| Can CLIO query Affinities? | EarthScope records carry `affinity_dataset_uuid`, `affinity_service_uuids` and `affinity_endpoint_uuids` extras. The ep-api documents Affinities as an Endpoint feature (`AFFINITIES_ENABLED`). No public Endpoint exposing it was found | Open: which Endpoint hosts the Affinities registry, and is it readable without sign-in? |
| Is ndp-test the catalog for the idp-test login? | Not determinable from public services | Open (for NDP) |
| Longer EarthScope GNSS series? | Both catalogs hold only the 9-day window | Open (for NDP); nothing longer is in either catalog |
| Can blank `format` fields be filled? | Blank on all sampled WFSI resources, 574 of 3,562 sampled OpenTopography resources (the `.adf` files) and some LANDFIRE/NIFC entries | Open (for NDP) |
| Which organizations would join a trial? | — | Open (for NDP) |
| New: why are OpenTopography datasets listed twice under one title? | Every Tahoe OpenTopography result appears twice | Open (for NDP) |
| New: stale 3DEP Entwine links | `usgs-lidar-public/TX_BurnetCo_2006` no longer exists | Open (for NDP): can the harvester drop or refresh removed projects? |

### R2 (2026-11-17): geoservices against live services (mechanism, not agent judgement)

Direct MCP calls through the in-memory FastMCP client, outside an agent.

| Call | Result | Time |
| --- | --- | --- |
| `describe_geo_service` NIFC WFIGS Interagency Perimeters layer 0, Tahoe box | Declared extent flagged as invalid lon/lat; measured data extent -176.65, 0.0, 144.94, 69.89; 43,247 records; coverage `overlaps` | 3.5 s |
| `fetch_features` same layer, Tahoe box, `count_only` | 21 perimeters | 0.5 s |
| `fetch_features` TRPA `Datadownloader_Wildlife/MapServer/8` (Waterfowl Habitat, whose Hub GeoJSON link fails) | 18 polygons, bounds -120.206, 38.785, -119.902, 39.138, one page, complete | 1.7 s |
| `describe_geo_service` sparcal `rrk_sn__TotalFuelLoad_2021_30m` | EPSG:3310, grid 13,096 × 25,640, WGS84 extent -122.29, 35.06, -117.56, 42.00, coverage `overlaps` | 0.9 s |
| `read_raster_window` same coverage, Tahoe box | 2409 × 1448 at 30 m, valid on 56% of cells (the Nevada side is outside this California layer), 0 to 55.3, mean 13.6, units not declared | 3.8 s |
| `read_raster_window` OpenTopography `TAHOE_be.vrt`, 2 × 2 km at Emerald Bay, 1 M cell budget | 1123 × 890 at 2.03 m (decimated ×4.05 from 0.5 m), 1,928 to 2,791 m | 23.4 s (72 s before same-CRS reads replaced warping) |
| `read_raster_window` two 3DEP 1 m tiles in UTM 10 and UTM 11 | Mosaicked in EPSG:26910, valid 100%, 2,019 to 2,689 m. A box east of both surveys correctly returned "no valid cells"; per-tile checks confirmed both tiles are nodata there | 5.4 s |
| `describe_geo_service` Entwine viewer link for `CA_PlacerCo_2012` | Followed to `ept.json`: 4.96 billion points, EPSG:3857, extent -121.38, 38.75, -119.99, 39.32 | 0.5 s |
| `read_point_cloud` same, 0.01° box near Tahoe City, 500k budget | 375,397 points to octree depth 7 (spacing 4.7 m), `complete: false` with the reason; classes 1, 2 (62,931 ground), 7, 11 | 3.7 s |
