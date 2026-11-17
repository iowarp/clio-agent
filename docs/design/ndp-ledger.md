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
| 3. Core geospatial operations (clio-kit) | Done on clio-kit `claude/ndp-data-access`: new `geoprocess` server (`a78f824`) |
| 4. Discovery skill and shared `resolve-region` (marketplace) | Done on marketplace `claude/ndp-data-access` (`fce1ba6`): `geospatial` plugin (moved `resolve-region`, `get-geospatial-data`, `harmonize-layers`), ndp `find-ndp-data-for-place`, tile tool claimed |
| 5. Harmonization and recorded merge | Skill written (`harmonize-layers`); provenance sidecar format awaiting the earth-ocean session's answer; not exercised live |
| 6. Map with time for rasters and vectors | GeoTIFF/COG/VRT grid layers on `clio.layered-map.v1` built on clio-agent branch `claude/ndp-data-access-map` (`0ea9e1d`, based on the earth-ocean branch, agreed with that session); vectors already worked (`shapes`). Live render verified with a tool-directed prompt (R8). The agent-facing catalog text in clio-schemas still says grids are NetCDF or Zarr only (Needs user) |
| 7. Domain skills (marketplace) | Done (`70088e8`): `wildfire` (fire-perimeters, fire-risk-and-fuels), `landscape` (terrain-from-lidar with a D8 flow-routing script, planning-layers); the ndp agent carries both |
| Qualification runs | In progress: Tahoe habitat run 1 (R3) done, partial; snow-depth case grounded (R4) |

## Needs user

- ~~Repository access~~ resolved 2026-11-17: the user allowed attaching clio-kit and
  clio-agent-marketplace; both are on `claude/ndp-data-access`.
- **clio-schemas access** (refused 2026-11-17, `add_repo` "Unauthorized Access";
  only clio-kit and the marketplace were authorized). One change is needed there
  for gap 6: `a2ui/catalog_fields.py` describes `gridUri` as "Registered NetCDF
  or zipped Zarr grid with longitude and latitude (grid layers)." and
  `a2ui/instructions.py` repeats it, so an agent converts GeoTIFFs to NetCDF
  before drawing (R8a). Proposed text: "Registered NetCDF, zipped Zarr or
  GeoTIFF/COG/VRT grid (grid layers); for a GeoTIFF, variable names a band by
  number, bandN or description." The validator and renderer already accept it.
- **Map branch placement.** The GeoTIFF layer change sits on
  `claude/ndp-data-access-map` (based on `claude/earth-ocean-weather`) because it
  extends that unmerged branch; the earth-ocean session left the merge to the
  owner. This is the one place the "one branch name per repo" rule could not hold.
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

- **D7. NDP verifies TLS with the OS trust store** (`6cc03b6`). NDP clients set
  `trust_env=False`, so httpx trusted only certifi and failed behind any
  TLS-inspecting network whose CA the OS trusts. This container is one; so are
  many lab and university networks. The built-in catalog tools failed in R3 and
  the agent fell back to the shell. One owner helper (`ndp_tls.system_tls()`,
  a truststore context) now serves every NDP client. Direct check: certifi fails
  and truststore succeeds against `ndp-test.sdsc.edu` from this container.
- **D8. Tool servers are enabled the way a person enables them.** A fresh agent
  shows an "Enable all" card for servers its plugins declare (geo, geoservices,
  geoprocess, terrain, ndp); nothing is installed without it. In R3 nobody
  clicked it, so the agent had no geoservices tools. Qualification runs now click
  "Enable all" before typing the request (`scripts/cloud/checks/ndp_study.mjs`),
  as the October runs pre-saved the NDP account. This is environment setup, not
  model-facing instruction.
- **D9. The clio-agent branch pins the sibling branches.** `pyproject.toml` pins
  clio-kit at the `claude/ndp-data-access` commit, and the marketplace submodule
  and `DEFAULT_MARKETPLACE_COMMIT` point at the marketplace branch (`fa913d3`,
  `35b9c1b`), as develop pins merged sibling work. A release replaces both.

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
| Do national layers expose their extent through their services? | NIFC FeatureServer: declared extent malformed, but a `returnExtentOnly` query gives the real data extent (-176.65, 0.0, 144.94, 69.89) and a bbox count works (21 perimeters in the Tahoe box). sparcal WCS: `DescribeCoverage` gives a correct EPSG:3310 envelope. LANDFIRE records link HTML pages, not a service | **Answered:** yes for sparcal WCS; for NIFC through a data query rather than the declared extent (geoservices does both); unknown for LANDFIRE, whose records have no service link |
| Can CLIO query Affinities? | EarthScope records carry `affinity_dataset_uuid`, `affinity_service_uuids` and `affinity_endpoint_uuids` extras. The ep-api documents Affinities as an Endpoint feature (`AFFINITIES_ENABLED`). No public Endpoint exposing it was found | Open: which Endpoint hosts the Affinities registry, and is it readable without sign-in? |
| Is ndp-test the catalog for the idp-test login? | Not determinable from public services | Open (for NDP) |
| Longer EarthScope GNSS series? | Both catalogs hold only the 9-day window | Open (for NDP); nothing longer is in either catalog |
| Can blank `format` fields be filled? | Blank on all sampled WFSI resources, 574 of 3,562 sampled OpenTopography resources (the `.adf` files) and some LANDFIRE/NIFC entries | Open (for NDP) |
| Which organizations would join a trial? | — | Open (for NDP) |
| New: why are OpenTopography datasets listed twice under one title? | Every Tahoe OpenTopography result appears twice | Open (for NDP) |
| New: stale 3DEP Entwine links | `usgs-lidar-public/TX_BurnetCo_2006` no longer exists | Open (for NDP): can the harvester drop or refresh removed projects? |
| New: stale service links in harvested records | TRPA "Wetlands" points at layer 4 and other records at layer 2 of `Datadownloader_SoilsandHydro`, both "404 Layer not found" (R3, R5); 28 of 50 TRPA Hub GeoJSON links fail; Cal OES records point at hosted services that no longer exist (`Fire_Station`: "400 Invalid URL", checked by hand) and at FRAP Fire Threat (404); several Cal OES layers need an ArcGIS token | Open (for NDP): can harvesting check each resource URL and mark dead ones? |
| New: production TRPA host | Production records use `arcprod.trpa.org`; through this container's egress proxy its certificate fails hostname verification, while a direct handshake shows a certificate naming the host | Undetermined from here; worth checking from a normal network |
| New: NIFC history stops at 2019 near Tahoe | The InterAgencyFirePerimeterHistory "All Years" layer returned no 2020 or later fires within 20 km of the lake, although its description says it runs through 2021 (NIFC run) | Open (for NDP or NIFC): is the archive layer behind, or are recent years only in the current-year WFIGS layers? |
| New: same dataset id in both catalogs | OpenTopography datasets carry the same CKAN id on production and test (the tile tool had to ask which catalog) | Answered for CLIO (the tool names both catalogs); open for NDP whether the two are meant to mirror |
| New: BCO-DMO records hold no data | Every sampled `bco_weather` record has one ISO XML resource; the data is at bco-dmo.org | Answered: metadata only on NDP; data acquisition needs BCO-DMO directly |

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

### R3 (2026-11-17): Tahoe habitat, October request unchanged (plain request; partial)

- Stack: isolated (`CLIO_CLOUD_ISOLATE=1`), provider `claude_code`, model
  `claude-sonnet-5-5`, agent `clio/ndp` 1.1.0. Session `sess_41e09de7589c`,
  workspace `ws_94bfda34fc6b`.
- Command: `STUDY_AGENT=ndp STUDY_TEXT=… node scripts/cloud/ui_check.mjs --steps
  scripts/cloud/checks/science_study.mjs --label ndp-tahoe-habitat --width 1440
  --height 900`. The request was typed into the real composer. The harness allowed
  each permission for the session.
- Request: "What can NDP tell us about how much of the Lake Tahoe basin is wetland
  or riparian habitat, and how that relates spatially to the lake? Use the
  available scientific data to make a small map and quantify what you can, with
  clear caveats."
- What the agent chose (evidence of judgement, since nothing was named): it
  loaded `find-ndp-data-for-place`, then `planning-layers`.
- What failed: every built-in NDP search failed TLS (fixed after this run, D7);
  the agent queried the public CKAN API through the shell with verification on.
  The geoservices tools were never available (D8). It found that the NDP
  "Wetlands" record points at TRPA layer 4, which no longer exists on the service
  or the hub, listed the service's current layers, and used "Stream Environment
  Zone - Remotely Sensed" instead. It also caught a GeoJSON labelled EPSG:4326
  that held UTM metres.
- Science: 10,206 ha of dissolved wetland/riparian habitat (six SEZ classes,
  overlaps counted once), 12.3% of 83,165 ha of basin land (basin 132,979 ha,
  lake 49,814 ha). The wetland share is 10 to 13% in every distance band from the
  shore, from 0-100 m to 5 km and beyond, so there is no shoreline
  concentration; the large patches are at the south shore (Upper Truckee and
  Taylor Creek marshes). Cross-checks: an independent meadow layer gives 2,008 ha
  against 2,669 ha of SEZ meadow; 9% of the wetland lies in the 100-year flood
  zone. Caveats stated: undated remotely sensed layer, "forested" SEZ as
  wetland is a judgement, no sliver filter.
- Verdict: **partial.** The science answers the October question, including the
  overlap-corrected footprint the October run lacked. Not a pass of the NDP path:
  the built-in tools failed TLS, and the data came through hand-written REST
  queries rather than the access layer. Rerun as R5 after D7 and D8.
- Screenshots: `ndp-ledger-shots/r3-tahoe-1440.png` (1440x900),
  `ndp-ledger-shots/r3-tahoe-390.png` (390x844, showing the unclicked enable
  card), the agent's map `ndp-ledger-shots/r3-tahoe-agent-map.png`.

### R4 (2026-11-17): snow-depth case grounded by hand

See `benchmark/ndp-snow-depth-lidar/README.md` (`36b97a5`): Southern Sierra CZO
March and August 2010 bare-earth models, differenced with geoservices and
geoprocess. Median depth 2.02 m at Wolverton, 0.90 m at Providence Creek.

## Qualification set 2026-11-17 (R5 onward)

Common setup for every run below: isolated stack (`CLIO_CLOUD_ISOLATE=1`), provider
`claude_code`, model `claude-sonnet-5-5`, agent `clio/ndp` 1.1.0 (marketplace
`70088e8`), clio-kit `986cb22`. Two catalogs configured as an operator would
(`CLIO_STORAGE_NDP_ENDPOINTS`): production `ndp` and test `ndp-test`, because the
Wildfire Task Force and bco_weather exist only on test. Tool servers were enabled once
through the agent-needs card (D8). Each request is typed into the real composer by
`bash scripts/cloud/ndp_batch.sh <key>` from `scripts/cloud/checks/ndp_requests.json`.
The harness allows each permission for the session. Desktop (1440x900) and phone
(390x844) captures are in `ndp-ledger-shots/`. Runs on this stack predate `ec55d30`
(coverage in search results).

### Per-organization acquisition checks

| Organization | Request (abridged) | Session | Path the agent took | Result | Verdict |
| --- | --- | --- | --- | --- | --- |
| CA Wildfire and Forest Resilience Task Force | Surface fuel around South Lake Tahoe | `sess_991a854b2f8d` | Search (test catalog) found "Total Fuel (Dead and Down) Load"; connect and download returned HTTP 500 (`connected_data_open`); `describe_geo_service` then `read_raster_window` with the dataset's WCS | Mean 15.5 to 16 Mg/ha within 3 to 10 km, 90th percentile about 21, heaviest about 4 to 7 km south-southeast of town; under 0.1% of cells above the dataset's 40 Mg/ha high-severity level. Units taken from the description because the raster has none (said so). No map rendered (said so) | **Pass** (data in hand through the access layer after the file link failed) |
| Cal OES | Hazard or emergency layers for Sonoma County, with features | `sess_01738ac6a708` | Search, then `describe_geo_service` and `fetch_features` (count and fetch) on each record's Esri REST layer; `clip_features` to the county polygon | Features in hand: 7 counties intersecting the county box, and 6 public-use airports inside the county (9 in the box, 3 removed by the clip). Every hazard layer it tried was unreachable, each with a typed reason: FRAP Fire Threat 404 (service gone), several hosted services "400 Invalid URL" (checked independently: `Fire_Station` no longer exists on that ArcGIS host), CAL FIRE units and shelters "499 Token Required", fire points with 0 records. It said it looked at 50 of 260 results | **Pass for acquisition, partial for the question** (no hazard layer was retrievable; the reasons are real and stated) |
| WFSI | Fuel moisture measurements | `sess_6ecf46cb8325` | Search, `connected_data_connect` of the data table and its dictionary, linked read | Fort Stewart 2024 campaign, 150 rows, values with units and formula from the dictionary; live pine needles 155 to 212%, dead fine fuels 9 to 33%; flagged one replicate (102.6% against 20.5 and 27.4%) and a dictionary that calls a decimal column integer | **Pass** |
| EarthScope | GNSS series for a station near Lake Tahoe, and its length | `sess_cfe3fe531e72` | Loaded `work-with-ndp`; 25 searches; connect, download, plot | Station SLID (Slide Mountain, NV), 2024-12-03 to 12-12 at 1 Hz, 851,176 of 863,987 epochs, 581 gaps; units assumed metres and reference frame undocumented, both stated. It chose SLID from knowledge, not from coordinates, because results then carried none (fixed after, `ec55d30`) | **Pass**, with the station choice made without data (now fixable) |
| bco_weather | California Current cruise measurements from BCO-DMO | `sess_0ef84be8e932` | Search (test catalog) found five BCO-DMO records; connect failed for all five | No data: every record holds one ISO metadata XML, data lives at bco-dmo.org (stated). **Bug found:** attaching failed because the source label became the dataset title, over the 120-character limit, for any long title; fixed in `209c357` | **Typed, honest no-data result**, plus a product bug fixed |

### R5: Tahoe habitat, October request unchanged, rerun after D7 and D8

Session `sess_c083b1b7a2e7` (73 calls, 21 failed). The agent loaded no skill this
time; it went straight to the geoservices tools (`fetch_features` ×26,
`describe_geo_service` ×17) and rendered with `geo_render_feature_map`.

- Failures, all handled and reported: production records point at
  `arcprod.trpa.org`, whose certificate fails hostname verification through this
  container's egress proxy (a direct handshake presents a certificate naming the
  host, so this is undetermined rather than TRPA's fault); TRPA layers 2 and 4 no
  longer exist ("404 Layer not found"); one SEZ page exceeded the 64 MiB response
  bound and was narrowed; it called the optional ndp server's
  `get_dataset_details` with wrong argument names and guessed ids (the reason for
  `ec55d30`).
- Science: union of SEZ (non-lake classes), SEZ assessment units, UC Davis/USFS
  meadows and USFS fens, dissolved: **11,093 ha, 13.3% of 83,152 ha of basin
  land.** Non-lake SEZ alone: **10,206 ha**, the same figure R3 reached in a
  separate session with hand-written queries. The wetland share is 13 to 15%
  within 1 km of the shore against 13.3% overall, so there is no shoreline
  concentration; the large patches are the Upper Truckee corridor and marsh and
  Camp Richardson/Tallac. Per watershed: Upper Truckee about 2,940 ha, Camp
  Richardson the densest at about 37%. Caveats: SEZ is a regulatory category
  with a large forested riparian class; the layers are undated and differ in
  method, so the union is an upper bound; lake-level sensitivity of the
  shoreline bands; no field check.
- Independent check (by hand, outside the agent): TRPA layer 32 fetched with
  geoservices (all 28,022 polygons in 32 pages after clio-kit `1ebcf09` halved
  oversized pages), the six non-lacustrine classes dissolved and clipped to basin
  land with geopandas: **10,206 ha, 12.3% of 83,152 ha**, basin 132,966 ha, lake
  49,814 ha. Identical to both agent runs.
- Verdict: **pass.** This is the result the October run could not produce:
  NDP-discovered data acquired through the access layer, an overlap-corrected
  footprint, its spatial relation to the lake, a map the agent inspected, and
  stated limits. Mechanism note: no map-with-time view, a static PNG.
- Captures: `ndp-ledger-shots/r5-tahoe-1440.png` (1440x900),
  `ndp-ledger-shots/r5-tahoe-390.png` (390x844), and the agent's map
  `ndp-ledger-shots/r5-tahoe-agent-map.png`.
| TRPA | Deer fawning habitat share of the basin | `sess_92b8e7a7adb5` | Loaded `work-with-ndp`; downloaded the habitat GeoJSON; the TRPA boundary download failed (HTTP 400) so it fetched the boundary from the Esri REST layer; `overlay_features` dissolve | 1,585 polygons, no overlaps, **13,048 ha, 9.8% of the TRPA boundary**; said the boundary includes the lake (about half its area), so the land share is about twice that, not computed. **Bug found:** `overlay_features` failed on date attributes; fixed in clio-kit `c9db7e0` | **Pass** |
| NIFC | Wildfires within 20 km of Lake Tahoe, and years | `sess_408083696c98` | Loaded `fire-perimeters`; `fetch_features` on InterAgencyFirePerimeterHistory (388 perimeters in a box), buffered the lake outline, merged agency duplicates | **158 fires in 73 years, 1910 to 2019** (255 polygons before merging duplicates; 166 with 8 prescribed burns); nearest: Angora 2007, Emerald 2016, Cascade 1994, Kiva 2002. Stated that the archive has no 2020 or later fires here (so the 2021 Caldor fire is missing), early-century undercount, approximate merge | **Pass** (acquisition and method), with a stated gap: it did not add the current-year WFIGS layer for 2020 onward |
| OpenTopography | Bare-earth model around Wolverton and its steepness | `sess_f3fdebef57f1` | `connected_data_ndp_tiles_covering` (first call refused: the dataset id is in both catalogs, so it needs `endpoint_id`; it retried with one), then `read_raster_window` | SSCZO 2010 snow-off DTM, 2029 x 1825 at 1 m, 1,959 to 2,469 m; mean slope 18.1°, half the area 15 to 30°. The terrain tool refused 3.7 M cells (2 M limit), so it computed slope itself (said so). Vertical datum not checked (said so) | **Pass** |
| USGS 3DEP | Ground points for a square kilometre at Tahoe City | `sess_226c428604dd` | `read_point_cloud` on the USGS EPT bucket, four quadrants to reach full density | **2,427,115 ground points at 0.30 m spacing, 1,896.9 to 1,967.9 m, 2.4 pts/m²** (0.7 over the lake-heavy quadrant). Stated that NDP's `CA-NV_LakeTahoe_2010` record holds only a footprint and no EPT exists for it, so it used the overlapping `CA_PlacerCo_2012` survey. **Bug found:** `describe_geo_service` crashed on a streamed HTTP error (`ResponseNotRead`); fixed in clio-kit `8a7639e` | **Pass**, with the points from a different survey than the NDP record named (stated) |

### R6: cross-organization Tahoe correlation, attempt 1 (did not finish)

Session `sess_943e05311dc4`. Request: "For the Lake Tahoe basin, how do wildlife
habitat areas relate to recent wildfire, vegetation and fuels, and to the terrain?
Bring together whatever NDP has from the different organizations, tell me what
overlaps and what doesn't, and show it on a map."

- What the agent chose (plain request, nothing named): it loaded
  `find-ndp-data-for-place`, then `harmonize-layers`. It ran 23 catalog
  searches, fetched TRPA habitat layers with `fetch_features`, and checked the
  Wildfire Task Force's WCS for burn probability, CWHR and goshawk habitat
  coverages with `describe_geo_service` (`match` filtering 200 coverages).
- It then asked `read_raster_window` for the whole basin from the 0.5 m
  OpenTopography mosaic. That source has no overviews, so GDAL had to fetch
  about 30 GB of native blocks; the call hung for over 25 minutes and the
  harness gave up. **Fixed** in clio-kit `e8f0d6d`: such a read is refused in
  about a second with the size and the area that fits. A separate defect seen
  here (an HTTP error crashing `describe_geo_service`) is fixed in clio-kit
  `8a7639e`.
- It also called the optional ndp server's `get_dataset_details` 22 times to
  read record extents, with argument and id errors; search results now carry
  each record's location and time (clio-agent `ec55d30`).
- Verdict: **failed (tool fault, fixed); rerun as R6b.**

### R7: snow depth (benchmark `ndp-snow-depth-lidar`), plain request

Session `sess_e436ee637f6c` (24 calls, none failed). Request: "How deep was the
snow in the Southern Sierra Critical Zone Observatory in March 2010? Use NDP data
and show me a snow-depth map." The agent loaded `work-with-ndp`, found the
snow-on and snow-off datasets, connected and downloaded them (about 2.4 GB), and
differenced bare earth for each survey area, sampling every fourth 1 m cell.

| Area | Mean | Median | 5th to 95th percentile |
| --- | --- | --- | --- |
| Bull | 1.81 m | 1.82 m | 0.84 to 2.65 m |
| Courtwright Rd | 2.22 m | 2.25 m | 1.49 to 2.85 m |
| Providence | 0.91 m | 0.83 m | 0.06 to 2.05 m |
| Soaproot Saddle (about 1,000 to 1,400 m) | 0.02 m | 0.01 m | -0.16 to 0.17 m |
| Wolverton | 2.41 m | 2.37 m | 0.54 to 4.51 m |

It explained the missing San Joaquin area (its snow-on file is an 8-bit
shaded-relief image, not elevation), inspected its map, and stated canopy,
sampling, coverage and no-ground-truth limits.

Against the grounded pass criteria (`benchmark/ndp-snow-depth-lidar/README.md`):

- finds a matched pair and names surveys and dates: **met**;
- reads only the region: **not met** (downloaded the surveys);
- differences on one grid and states the vertical-datum assumption: differenced,
  but **no datum statement**;
- statistics within 0.2 m of the grounded medians for the same region: the
  agent measured whole survey areas, not the grounding boxes. Providence agrees
  (0.83 m against 0.90 m); Wolverton's whole area (2.37 m) is not comparable
  with the grounding sub-box (2.02 m);
- map and limits: **met**.

Verdict: **partial.** Sound science and an honest answer; two criteria missed.
Next: the pass criteria name bounded reads, which `get-geospatial-data` covers
but the agent did not load.

### R8: GeoTIFF layers on the layered map (gap 6), live in the browser

A second isolated stack from the map worktree (`claude/ndp-data-access-map`,
agent port 8797, UI 5183), workspace files fetched with geoservices: the Sierra
Nevada fuel-load WCS subset (EPSG:3310, 1668 x 1301), an OpenTopography DEM window
at Tahoe City (EPSG:26910) and TRPA waterfowl habitat polygons, each with its
provenance sidecar.

- **R8a** (`map-geotiff.out`), request naming the files: "Show tahoe_fuel_load.tif,
  tahoe_city_dem.tif and waterfowl_habitat.geojson from this workspace together
  on one interactive map, so I can toggle the layers." The agent converted both
  rasters to NetCDF before drawing, because the catalog text says grid layers
  take NetCDF or Zarr (Needs user: clio-schemas).
- **R8b** (`sess_1b094b228c91`), tool-directed prompt naming the component and
  `gridUri` = the `.tif`: one `create_a2ui_surface`, no failures; the producer
  accepted `variable: band1` for both rasters, `capture_a2ui_surface` waited for
  the tiles and the agent described both rasters (fuel 2.93 to 24.0 on magma,
  DEM 1,900 to 2,120 on cividis) and the habitat outlines on the south shore.
  **Mechanism verified with a tool-directed prompt; not evidence of agent
  judgement.** Capture: `ndp-ledger-shots/map-geotiff-1440.png` (the reply; the
  map is above the fold). A phone-width capture of this session was refused by
  the session's permission classifier and was not taken.

### Final batch on the restarted stack (clio-kit `e8f0d6d`, marketplace `9290d68`, clio-agent `65f9e6c`)

| Run | Session | What the agent did | Result | Verdict |
| --- | --- | --- | --- | --- |
| October Konza (request unchanged) | `sess_a0fcdee472d8` | Loaded `work-with-ndp`; found the NEON/Sage/WIFIRE burn dataset (both catalogs), connected 10 resources (anemometers, CO2 analyzers, Sage streams) | No bytes: every download failed "NDP could not be reached while reading this resource" and a linked read timed out; it stopped and said what the data could support. The files are on `wifire-data.sdsc.edu`, which answers a ranged request in 0.5 s from here; the wind file is 1.24 GB, and a direct streamed read of it from this container did not finish within 300 s. So the failure is slow or interrupted large transfers from here, not an unreachable NDP; whether the proxy cut the agent's transfers was not established. CLIO's message "NDP could not be reached" misdescribes a transfer that started | **Failed to acquire (environment, not agent judgement)**; honest report |
| Case 04 (CIMIS fire-risk profile) | `sess_2fa564e47efe` | Loaded `work-with-ndp`; geocoded Fresno; chose station 80 as the only Fresno-area station, naming the farther ones it rejected; downloaded and profiled the hourly CSV | 131,520 rows, 2010-09-28 to 2025-09-28, QC flags M/R/S/Q excluded and Y kept (3%); favourable fire-spread hours (RH ≤ 20%, wind ≥ 4.5 m/s, ≥ 25 °C) 0.13% of hours, April to September, peak June; field-operation windows about 50% of daytime hours, best March to May and October; stated that 2 m wind understates 10 m fire-weather winds. Figure inspected | **Pass on the science and the station-selection semantics.** Not met: the README's expert decomposition (single agent, no delegation) |
| Case 03 (wildfire and weather fusion, LA) | `sess_fa1ff4fa516f` | NIFC current perimeters (0 in the box) and incidents (18 small or administrative), CalOES NWS alerts and NWS forecast/observations | Brief: no significant active fire; Flood Watch for LA and Ventura through 19 Nov with 1 to 2 in of rain forecast; no Red Flag Warning in California; burn-scar debris-flow concern named but not analysed; caveats on unqueried VIIRS, CAL FIRE and RAWS | **Pass on fusion semantics** (two data branches, timestamps and scope reconciled, caveats). Not met: expert decomposition, and no map |

### R6b: cross-organization Tahoe correlation, rerun (computed, answer not delivered)

Session `sess_d2d93428c390` (97 calls, 6 failed), same request as R6. The agent
loaded `find-ndp-data-for-place` and `work-with-ndp`, then acquired, each file with
its `clio.provenance/1` sidecar:

- **TRPA**: 17 habitat layers (deer fawning, goshawk and spotted owl activity
  centres, frog critical and suitable habitat, eagle, osprey, peregrine, marten,
  bat, willow flycatcher, waterfowl, fish, biodiversity) and the basin boundary;
- **NIFC**: perimeter history and current-year WFIGS; **CAL FIRE** perimeters;
- **California Forest Observatory** 2019: canopy cover, canopy height, ladder fuel
  density and surface fuels;
- **terrain**: when `read_raster_window` refused the whole basin from the 0.5 m
  lidar mosaic (37.9 GB, "about 112 km² fits"), it took a 30 m Copernicus DEM for
  the basin through `search_stac` and kept lidar for a window over the Angora burn.

It then computed `habitat_summary.csv`: for each habitat, its area, elevation,
slope, canopy, ladder fuel and the area burned 2007 to 2024. Examples: deer
fawning 13,058 ha, 9.5% burned; spotted owl activity centres 2,741 ha, 10.6%
burned; goshawk threshold areas 7,729 ha, 10.9% burned; frog critical habitat
81,471 ha, 6.0% burned; nests of eagles, osprey and golden eagles 0% burned. The
three-panel map (`ndp-ledger-shots/r6b-correlation-agent-map.png`) overlays
habitat, recent fire perimeters (Angora 2007, Little Valley 2016), canopy cover
and ladder fuel density with a slope contour.

No final reply: at about 21:00 the test stack's clio-core store (capped at
128 MB by `CLIO_ARC_CTE_FILE_CAPACITY` for shared cloud disks) filled, writes
failed with code 12, health went to 503, the session stopped persisting, and the
two runs that followed could not open sessions. Verdict: **cross-organization
acquisition and correlation computed; the written answer and its
overlap/no-overlap report were not delivered (test-environment failure).** The
stack is restarted with a 1 GB store for the remaining runs.

### Test-stack incident (21:00 to 21:25) and recovery

- The isolated stacks shared one clio-core daemon (port 9413), created with
  `CLIO_ARC_CTE_FILE_CAPACITY=128MB` (the cloud default for shared disks). The long
  correlation session filled it: writes failed with code 12, a boot migration
  marker could not be written, `/v1/health` flapped to 503, and the UI stuck at
  "Connecting to cloud", so three runs (Alta twice, case 01, discovery task 1)
  never opened a session. `clio-agent migrate` could not repair a full store.
- Recovery: compact traces of every cited session were committed
  (`docs/design/ndp-runs/`, `e9c392b`), both stacks and the daemon were stopped,
  their disposable state (5.7 GB) removed, and the stack restarted fresh with a
  2 GB store. Tool servers were enabled again through the card.
- Finding for CLIO, not NDP: a full clio-core store degrades health and blocks
  new sessions; the store's fill level is not reported before writes start
  failing. Worth an issue against the clio-core health integration.

### Alta (October request unchanged), on the fresh stack

Session `sess_445d6dba454f` (27 calls, 1 failed shell call). The agent loaded
`work-with-ndp`, found both Alta-Collins datasets (2000 to 2023, and 2023-24),
downloaded them, read both readmes, and compared seasonal bulk SLR, 12-hour event
SLR (LPE ≥ 0.10 in) and monthly SLR, with a chart it inspected.

- Its result: 2023-24 bulk SLR **10.8** against a 2000 to 2023 mean of **12.1**
  (SD 1.2), rank 4 of 24, z = -1.08; events lower too (median 11.1 against 12.0,
  Mann-Whitney p ≈ 0.02, with the dependence caveat); lower in every month.
  Conclusion: denser snow than average but within year-to-year variability.
  Limits stated: one season against 24, the LPE method change around 2002,
  quality-controlled history against raw 2023-24 data, the event threshold,
  incomplete seasons, 313 rain or melt periods kept.
- **Independent recomputation** (by hand, from the same downloaded files):
  with the agent's rule (all snow over all LPE) 10.8078 against 12.1395, its
  numbers exactly; with October's rule (paired positive observations only)
  635.5 / 58.24 = **10.9117** against **12.2736**, rank 5 of 25, identical to the
  October verification. The difference is the definition (the agent's includes
  rain-only LPE), which it stated.
- Verdict: **pass** (the October Luna failure, discovery that gave up, did not
  recur; the October Sol result is reproduced).
