# Earth and ocean ledger

The working record for the oceanography and weather plan (two marketplace
plugins, two clio-kit servers, a tile service and four general A2UI
components). It holds the open-item resolutions with their evidence, the code
licence audit (plan Rule 5), pinned versions, qualification fixtures with
their reference values, and the Q1–Q28 results.

Branch in every repository: `claude/earth-ocean-weather`.

| Repository | Base | Base commit |
| --- | --- | --- |
| iowarp/clio-agent | `develop` | `ba8c47f` |
| iowarp/clio-kit | `main` | `4b5e6e3` |
| iowarp/gact-tui | `develop` | `def3ae0` |
| iowarp/clio-schemas | `develop` | `e138128` |
| iowarp/clio-agent-marketplace | `develop` | `f860020` |

clio-kit is based on `main` because clio-agent pins clio-kit at `88874fb`
(`pyproject.toml`, `[tool.uv.sources]`), which `main` contains and `develop`
does not; `main` is 146 commits ahead of `develop` (checked 2026-11-17).

## Machine

Cloud container: 4 CPUs, 15 GB RAM, no GPU (`nvidia-smi` absent), 26 GB free
disk. System packages installed with apt for this work: `libnetcdf-dev`,
`libnetcdff-dev`, `gfortran`, `libopenmpi-dev`, `openmpi-bin`, `nco`, `cdo`,
`netcdf-bin`. Rows that need a GPU (Q14's MOM6 test run is CPU, but Q19, Q20,
Q25 run AI models) are recorded against this hardware.

Reachable without credentials (HTTP probe, 2026-11-17): Ifremer and IOOS
glider ERDDAP, NOAA NCEI, CoastWatch ERDDAP, PSMSL, UHSLC, CCHDO, BODC, ECMWF
open data (`data.ecmwf.int`), Google Cloud Storage (ARCO ERA5), Hugging Face,
GEBCO download, CDS web. Copernicus Marine and CDS downloads need accounts.

## Code licence audit (Rule 5)

Read from each package's own licence file in the pinned sdist or repository,
not from index metadata alone (several packages publish no licence field).

| Package | Pinned | Licence (source) | Used by | How it is used |
| --- | --- | --- | --- | --- |
| gsw | 3.6.23 | BSD-3-Clause (PyPI) | ocean | imported |
| xgcm | 0.10.1 | MIT (PyPI) | ocean | imported |
| utide | 0.4.0 | MIT (`LICENSE.txt`) | ocean | imported |
| argopy | 1.5.0 | EUPL-1.2 (`LICENSE`) | ocean | imported, unmodified |
| erddapy | 3.3.2 | BSD-3-Clause (PyPI licence text) | ocean | imported |
| parcels | 3.1.4 | MIT (`LICENSE.md`) | ocean | imported |
| earthaccess | 0.19.0 | MIT (PyPI) | ocean | imported |
| metpy | 1.7.1 | BSD-3-Clause (PyPI) | weather | imported |
| regional-mom6 | 1.0.4 | MIT (`LICENSE`) | ocean | imported (needs ESMF, see open items) |
| **marEx** | 5.0.0 | **AGPL-3.0-or-later** (`LICENSE`) | ocean | **separate worker process**, never imported by the server |
| **py-eddy-tracker** (`pyEddyTracker`) | 3.6.1 | **GPL-3.0** (`COPYING`) | ocean | **separate worker process**, never imported by the server |
| TempestExtremes | git `cafb070` | BSD-2-Clause (`LICENSE`) | weather | C++ binaries built from source, run as subprocesses |
| earth2studio | 0.20.0 | Apache-2.0 (PyPI) | weather | imported in an optional extra |
| copernicus-mcp | 0.7.2 | BSD-3-Clause (`LICENSE`); its deps EUPL-1.2 (copernicusmarine), Apache-2.0 (cdsapi) | plugins | run as published |
| ecmwf-weather-skills | git `62e6334` | Apache-2.0 (`LICENSE`) | weather plugin | used as published (skills + `https://mcp.ecmwf.int`) |
| nasa/earthdata-mcp | 1.1.0, git `330ba9a` | **no licence file in the repository** | ocean plugin | hosted endpoint only (`https://cmr.earthdata.nasa.gov/mcp/v2`); not redistributed |
| QgisRemoteMCP | git `1709216` | MIT (`LICENSE`) | QGIS case | candidate (needs Docker) |
| qgis_mcp (jjsantos01) | git `e37bf6f` | **no licence file** | — | not usable: no licence grants redistribution |

### Copyleft decision (marEx, py-eddy-tracker)

The ocean server's own code is BSD-3-Clause like the rest of clio-kit. It does
not import marEx or py-eddy-tracker. Each runs in its own locked environment
(`mcp-servers/ocean/workers/<name>/`), started by the server as a subprocess,
exchanging NetCDF files and a JSON result on disk. The worker scripts that do
import them carry the upstream's licence (AGPL-3.0-or-later for the marEx
worker, GPL-3.0-or-later for the eddy worker), and the packages are installed
unmodified from PyPI. py-eddy-tracker 3.6.1 pins `numpy<1.23`, so its worker
runs on Python 3.10 (with `setuptools<81` for numba's `pkg_resources`); the
two could not share an environment anyway (marEx needs `dask>=2025.9`, hence
`numpy>=1.24`; resolver output 2026-11-17).

This is recorded under "Needs user" because a licence posture is the
maintainers' decision: shipping AGPL and GPL workers inside the clio-kit wheel
is a distribution of those works.

## Pins and API names confirmed

Confirmed by importing the pinned version (2026-11-17, Python 3.13 unless
stated):

- gsw 3.6.23: `SA_from_SP`, `CT_from_t`, `sigma0`, `Nsquared`, `p_from_z`, `z_from_p`.
- utide 0.4.0: `solve`, `reconstruct`.
- xgcm 0.10.1: `Grid`, `Grid.transform` (density-space overturning).
- MetPy 1.7.1: `cape_cin`, `surface_based_cape_cin`, `most_unstable_cape_cin`,
  `mixed_layer_cape_cin`, `lifted_index`, `parcel_profile`.
- argopy 1.5.0: `DataFetcher().region()`, `.float()`, `.profile()`.
- Parcels 3.1.4: `FieldSet.from_xarray_dataset`, `ParticleSet`, `AdvectionRK4`,
  `JITParticle`, `ScipyParticle`.
- marEx 5.0.0: `preprocess_data(da, method_anomaly=..., method_extreme=...,
  threshold_percentile=..., window_years=..., ...)`, `tracker(data_bin, mask,
  R_fill, area_filter_quartile, ...).run()`, plus `regional_tracker`.
- py-eddy-tracker 3.6.1 (Python 3.10): `RegularGridDataset.bessel_high_filter`,
  `.add_uv`, `.eddy_identification`; `Correspondances`; `TrackEddiesObservations`.
- copernicus-mcp 0.7.2: every `marine_*` and `cds_*` tool the plan lists exists
  (read from `src/`). Credentials: `copernicusmarine login` file or
  `COPERNICUSMARINE_SERVICE_USERNAME`/`_PASSWORD`; `CDSAPI_KEY`.

## Open-item resolutions

| Item | Resolution | Evidence |
| --- | --- | --- |
| Parcels v3 or v4 | **3.1.4.** v4 is now at 4.0.3 (2026-11-13) and its PyPI README no longer marks v4 as in development, so the plan's reason is gone; but v3's API is the one confirmed here, and v3 holds the server at `zarr<3`. Migration to v4 is follow-up work, not done silently. | PyPI release dates; import check above |
| NASA Earthdata MCP repository, version, licence | `github.com/nasa/earthdata-mcp`, version 1.1.0 (`pyproject.toml`), **no licence file**. NASA runs it at `https://cmr.earthdata.nasa.gov/mcp/v2` (Streamable HTTP). The plugin uses that hosted endpoint for CMR search and does not redistribute the code. SWOT downloads go through `earthaccess` in the ocean server, as planned. | repository clone `330ba9a` |
| Which QGIS server | QgisRemoteMCP (MIT). qgis_mcp has no licence file, so it cannot be redistributed or recommended as a dependency. | repository clones |
| xgcm | **Not used.** `overturning` integrates on the model's own grid directly (a per-cell transport such as MOM6 `vmo`/`vh`, or v with cell widths), and density space bins transport into classes; neither needs xgcm's metrics. xgcm was dropped from the ocean server's dependencies. | `ocean_mcp/implementation/transport.py`, `tests/test_science.py::test_overturning_depth_space` |
| xarray for Parcels 3 | Pinned `xarray<2026`: xarray 2026.x no longer writes zarr v2, which Parcels 3.1.4's `ParticleFile` uses (`open_group() got an unexpected keyword argument 'zarr_format'`). 2025.12.0 works. | ocean `pyproject.toml`; `test_particles_follow_uniform_flow` (0.1 m/s for 5 days moves particles 42.8 km, expected 43.2) |
| ESMF for regional-mom6 | ESMF 8.8.1 builds from source here (gfortran, OpenMPI, NetCDF-Fortran from apt; `make` 0, `make install` 0). esmpy is not on PyPI, so regional-mom6 can only run against a local ESMF build. The `mom6_*` set-up tools are not built in this session: besides ESMF they need TPXO (registration), ERA5 through CDS and GLORYS through Copernicus Marine, none of which have credentials here. `mom6_preflight` is built and tested. | build log in the session scratchpad; `tests/test_mom6.py` |
| TempestExtremes | Built from source (git `cafb070`, CMake + NetCDF, no MPI); DetectBlobs, StitchBlobs, BlobStats, DetectNodes, StitchNodes and the blocking tools installed. The weather server finds them through `TEMPESTEXTREMES_BIN`; flags checked against the binaries' own help. | `tests/test_weather.py::test_detect_blobs_tracks_a_moving_blob`, `::test_tc_track_follows_a_planted_low` |
| Earth2Studio cyclone trackers | `TCTrackerWuDuan`/`TCTrackerVitart` import cupy, and the `cyclone` extra requires `cucim-cu13` and `cupy-cuda13x`: CUDA only. `tc_track` therefore has an explicit `backend`: `tempestextremes` (CPU; DetectNodes on MSLP minima with a closed-contour criterion, StitchNodes) or `earth2studio`, which refuses with `backend_unavailable` without CUDA. No automatic fallback. | earth2studio 0.20.0 source `models/dx/tc_tracking*.py`; PyPI requires_dist |
| ECMWF services used as shipped | ECMWF runs an MCP router at `https://mcp.ecmwf.int` (charts, meteograms, DestinE catalogue); the skills' scripts cover ERA5 point series through ECMWF's EDR service without a key (`polytope-edr.lumi.apps.dte.destination-earth.eu`, reachable here) and deterministic open-data forecasts. Neither computes ensemble exceedance, and the skills send ensemble point requests to Polytope (key). So `ensemble_fetch` (ECMWF ENS open data, keyless) and `exceedance` live in the weather server; ERA5 point series stay with the skills. | listing of `mcp.ecmwf.int` tools; `ecmwf-cds-ads/scripts/cds.py`; `ecmwf-open-data/scripts/odpoint.py` |
| Keyless ERA5 fields | ARCO-ERA5 on Google Cloud (`gs://gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3`, 273 variables, anonymous) for W4, W5, W6, W10, W11 fields; `era5_fetch` refuses subsets above a stated size. | probe 2026-11-17 |
| Station observations (W9) | NOAA Integrated Surface Database global-hourly CSV (`ncei.noaa.gov/data/global-hourly/access/{year}/{station}.csv`), keyless. | `weather_mcp/implementation/fetch.py` |
| IBTrACS | NOAA NCEI IBTrACS v04r01 CSV (`.../ibtracs.{last3years,ACTIVE,ALL,basin}.list.v04r01.csv`), doi:10.25921/82ty-9e16. | same |
| Tide gauges (O7, O11) | PSMSL and UHSLC are reachable without a key; the ocean tools take the downloaded series as files. | HTTP probe |
| Tile service | **Built in GACT, not as a clio-kit server.** The map's raster layers need tiles the browser can fetch same-origin and authenticated in Desktop, local Web and remote Web. A clio-kit process on localhost is reachable only from a browser on the same machine (Desktop CSP allows `connect-src http://127.0.0.1:*`; remote Web would need a GACT proxy to it, which is an SSRF surface). GACT already reads registered artifacts next to the data (raster-query), so `GET /v1/artifacts/{id}/tiles/{z}/{x}/{y}.png`, `GET .../grid-info` and `POST .../section-query` were added there (`gact/artifacts/raster_tiles.py`, `routes/artifact_grid_views.py`). Each tile reads only its index window from a lazily opened NetCDF or zipped Zarr, strided to at most 1024 cells per axis, so a multi-gigabyte field is never loaded whole (raster-query loads the whole plane and caps sources at 256 MiB). Not TiTiler: titiler.xarray brings rasterio/GDAL and a second web app for what ~300 lines of numpy and Pillow do on regular lon-lat grids. | `tests/test_gact/test_artifact_grid_views.py` (7 tests) |
| regional-mom6 install | 1.0.4 installs from PyPI but imports `xesmf`, which needs ESMF/esmpy; ESMF has no PyPI wheel and no Ubuntu 24.04 package. Needs a source or Spack build of ESMF for O18/Q14. | `ModuleNotFoundError: No module named 'ESMF'` |
| Linked views (`/view/time`, `/view/selection`, `/view/path`) | **Mapped onto the catalog's existing mechanisms, no new `/view/*` namespace.** Time and selection are `SelectionState` at a `/selection/<key>` path (`{field, values[], source}`): the layered map writes the frame it shows to its `time` path, and a section or profile stack reading the same path follows it; a click writes the picked feature to `selection`. Drawn geometry (`draw.value`), moved points (`edits`) and excluded profiles (`excluded`) are data-model bindings the agent reads back, e.g. `/view/path`. A second selection mechanism would break skill rule 3. | clio-schemas `474575c` (`field_components.py`, `instructions.py`); gact-tui `bound-value.ts`, `field-views.test.ts` |
| Profile stack as its own component | **A component, not a chart preset.** Skill rule 2 says one charting layer, but `clio.chart.v1` caps inline rows and has no persistent per-item decision: excluding a bad profile here writes the id list to a binding the agent reads back (a QC decision, not a highlight), and a 600 x 60 stack (36k rows) is read by reference with a search over the profile list. Overlay, label and category fields come from the same table. Recorded under Needs user because it departs from rule 2. | gallery screenshots; `test_a2ui_field_references.py` |
| Section view data | `clio.section.v1` names a section artifact the ocean and weather servers write (`distance` x a vertical with `positive` set, per-column latitude/longitude, station ids). GACT resamples it to the view size (`POST /v1/artifacts/{id}/section-query`), so a long, deep section never ships whole. Contours by marching squares in the client. | `test_artifact_grid_views.py::test_section_sample_carries_stations_and_orientation` |
| Producer checks for the field components | Every grid, section, table and GeoJSON a field component names is resolved at surface creation; a missing variable is `a2ui_grid_invalid` / `a2ui_section_invalid`, a wrong column `a2ui_field_not_in_dataset` naming the layer and the real columns. In `a2ui_producer/_field_references.py`, called from the existing validator so `_data_reference.py` stays under its size cap. | `tests/test_gact/test_a2ui_field_references.py` (7 tests) |
| Base map style in the layered map | Found in the browser: passing `offlineStyle()` fresh on each render made the map re-apply its style and drop every layer added to it, so nothing but the land showed. The style is now memoized, as `scientific-map-view.tsx` already does, and grid layers sit beneath the coastline. | gallery screenshots before/after |
| Grid editor | **An MCP App view of a general clio-kit `grids` server**, not a catalog component: every edit must be a tool call recorded with the grid's hash so it can be replayed onto a regenerated grid, which an A2UI binding cannot guarantee. Built: clio-kit `grids` server (`grid_open`, `grid_edit`, `grid_replay`, app-only `grid_window`) and its `ui://grids/editor` view; edits by longitude and latitude, base and result hashes in `<output>.edits.json`. Driven in Chromium through a stand-in host at 1440x900 and 390x844: box set, cell pick, undo, enclosed-lake fill, each a `grid_edit` call. | clio-kit `be198f67`, `tests/test_grids.py` (6), `tests/browser/run_standin.py` |
| copernicus-mcp in CLIO | **A clio-kit server that runs copernicus-mcp 0.7.2 as published** (`mcp-servers/copernicus`, the earthdata wrapper pattern: pinned with its `cmems,cds` extras, locked, run as its own program). Upstream lists its `cds_*` tools only once a CDS key resolves; the tests check both ways. The accounts are needs of a hand-written `copernicus` connector (Marine username and password, CDS token), kept in CLIO's secret storage and given to the server through the variables it reads. The marketplace `copernicus` plugin follows that connector. | clio-kit `84ab3af2` (`tests/test_server.py`, 4 tests); `catalogs/connectors/copernicus.yaml` |
| NASA Earthdata | **CLIO's existing `earthdata` connector**, not NASA's hosted endpoint: it already runs datalayer's earthdata-mcp-server 0.6.0 (BSD-3-Clause) in clio-kit with an optional Earthdata Login, and covers the CMR search O9 needs. SWOT downloads stay in the ocean server's `swath_fetch`, whose Earthdata Login is an optional need of a hand-written `ocean` connector (a connector's needs are the only way CLIO gives a server a secret). | `catalogs/connectors/earthdata.yaml`, `ocean.yaml` |
| Skew-T chart | **A weather-plugin skill with a Vega-Lite spec** (`skew-t-chart/skew-t.vl.json`) passed unchanged to `clio.chart.v1` with the soundings table as `dataUri`: log-pressure axis reversed, temperature skewed with height, dewpoint and the surface parcel. A plugin catalog preset cannot carry it (presets are string values), and a core chart preset would put a domain chart in clio-agent and gact-tui. It passes the chart guard (`check_chart_spec`) and renders with Vega 6.4; isotherm guide lines are not drawn because the guard allows data only from the named source. The T-S diagram follows the same pattern in the ocean plugin, with a script that adds labelled isopycnals to the table. | marketplace `07281118` (`plugins/weather/tests`, `plugins/ocean/tests`); renders in the session scratchpad |
| ECMWF weather skills | `ecmwf-open-data`, `ecmwf-polytope` and `ecmwf-cds-ads` vendored unchanged from ecmwf/ecmwf-weather-skills `62e6334a` (Apache-2.0) into the weather plugin, with LICENSE and NOTICE and an import lock of file hashes. Their dates and figures are ECMWF's product facts, so the literal lint skips files the lock records unchanged and checks any edited one. | `plugins/weather/import-lock.json`; `tests/test_check_skill_literals.py` |
| Plugins and connectors | `ocean` follows the `ocean` connector and requires `grids`; `copernicus` follows its connector; `grids` and `weather` run their clio-kit servers from `mcp.json` (no credentials). A plugin that binds a connection makes the agent unavailable until that connection is saved, which is why those two follow their connectors (added from the connector card). | `clio-agent plugin check` on the marketplace: 72/72 |

## Needs user

- **Tile service location.** The plan puts the tile service in clio-kit; it is
  in GACT for the reason in the resolution table (same-origin, authenticated
  tiles for remote Web). Confirm, or ask for a clio-kit server plus a GACT
  proxy.
- **Copyleft workers.** Approve shipping the marEx (AGPL-3.0-or-later) and
  py-eddy-tracker (GPL-3.0) workers as separate processes inside clio-kit, or
  choose to make them a separately installed extra.
- **Credentials** for Copernicus Marine, CDS, ECMWF Polytope, NASA Earthdata
  and TPXO come through CLIO's connector card; none are present in this
  session.
- **Profile stack departs from skill rule 2** (one charting layer). It is a
  component because exclusion is a persistent decision the agent reads back
  and a stack is read by reference far past the chart's inline row cap.
  Confirm, or ask for it as a `clio.chart.v1` preset plus a separate
  exclusion list.
- **Unmerged pins.** clio-agent on this branch pins clio-schemas `474575c` and
  clio-kit `7cfd3999`, commits on `claude/earth-ocean-weather` that are not
  merged into their develop/main; the pin comment says develop pins merged
  commits. They must be re-pinned to the merge commits before this lands.
- **Pre-existing failure, not from this work.** gact-tui
  `tests/contract/fixture-routes.test.ts` fails on develop `def3ae0` because
  `GET /v1/system/problem-report` is neither served by the e2e fixture server
  nor declared unserved. The three grid routes added here are declared.

- **Secrets for skill scripts.** ECMWF's `ecmwf-polytope` and `ecmwf-cds-ads`
  scripts run through `shell_bash` and read their keys from the environment
  or a dot-file. CLIO hands secrets only to a connector's server, so those
  scripts have no sanctioned way to get a Polytope or CDS key. The weather
  plugin's own paths are keyless (ENS open data, ARCO ERA5) and CDS goes
  through the copernicus connector; decide whether skills get a secret
  mechanism.
- **TPXO** needs a registration and its files on disk; the regional MOM6
  set-up tools that need it are not built (see the regional-mom6 rows).

## Qualification fixtures

Chosen before the work, with the reference each result is compared against.
Rows still marked "to choose" are filled in as their work starts.

| Q | Fixture | Reference values |
| --- | --- | --- |
| Q1 | GO-SHIP A16N, 2013 occupation (CCHDO expocode 33RO20130803), CTD casts | Talley et al. (2011), *Descriptive Physical Oceanography*, Atlantic chapter: AAIW as the intermediate salinity minimum (~700–1200 dbar) south of ~20°N; Mediterranean Overflow Water as the intermediate salinity maximum near 30–40°N; NADW between ~1500 and ~4000 dbar (S ≈ 34.9–35.0); AABW-influenced bottom water (θ < ~2 °C) in the south |

## Qualification results

Filled in as each row runs. A tool-directed prompt is recorded as "mechanism
verified with a tool-directed prompt; not evidence of agent judgement".
