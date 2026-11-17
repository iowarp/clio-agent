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
  clio-kit `0c5d3257`, commits on `claude/earth-ocean-weather` that are not
  merged into their develop/main; the pin comment says develop pins merged
  commits. They must be re-pinned to the merge commits before this lands.
- **Pre-existing failure, not from this work.** gact-tui
  `tests/contract/fixture-routes.test.ts` fails on develop `def3ae0` because
  `GET /v1/system/problem-report` is neither served by the e2e fixture server
  nor declared unserved. The three grid routes added here are declared.
  `src/components/clio/connected-source-picker.test.tsx` (two remote-host
  cases) also fails on this branch with and without this work's changes.

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
| Q18 | ECMWF ENS open data, latest run, Bergen (Norway), 72 h precipitation | Pass: a probability per day (or for the window) of more than 30 mm, as the fraction of members, with the run and member count stated |
| Q21 | ERA5 hourly 2 m temperature at Lisbon, 1991-2020 (ARCO ERA5, keyless) | IPMA's documented heat waves: early August 2003 and early August 2018 (Lisbon above 40 °C on 4 August 2018) must be among the events; the CDS cost estimate the plan names needs a CDS token (absent) |
| Q24 | ERA5 pressure levels, the cold front of Storm Ciara over Great Britain, 9 February 2020 | Met Office storm report: a cold front crossing England and Wales on 9 February; winter soundings with low CAPE either side, colder and drier behind the front |
| Q22 | ERA5 integrated vapour transport, US West Coast, December 2022 to March 2023 | CW3E's season summary: a family of about nine landfalling atmospheric rivers in California from late December 2022 to mid January 2023, then further events in February and March |
| Q23 | ERA5 500 hPa geopotential, Europe and the North Atlantic, winters 2009-10 and 2010-11 | Cattiaux et al. (2010, GRL): winter 2009-10 dominated by persistent high-latitude (Greenland and North Atlantic) blocking with a record negative NAO |
| Q28 | CMIP6 (Pangeo catalog), three models, SSP2-4.5, June to August near-surface temperature over the Iberian Peninsula, 2071-2100 against 1981-2010 | IPCC AR6 WGI Atlas: Mediterranean summer warming under SSP2-4.5 of roughly 2.5 to 4 °C by late century, with a model spread of a degree or more |
| Q14 | ETOPO1 over regional-mom6's Tasmania demo domain (CoastWatch ERDDAP `etopo180`, 44.5-39°S, 143.5-149.5°E), block-averaged to 0.1° (55 x 60), depth positive down. Two planted problems, not told to the agent: an isolated 35 m wet cell inland at about 42.0°S, 146.6°E, and a one-cell-wide 20 m channel of five cells at about 41.6°S, 147.6-148.0°E | Pass: both found and fixed through `grid_edit` calls (each recorded in the edit log with the base and result hashes), and the log replays onto a regenerated grid. The regional-mom6 run directory and a MOM6 test run need TPXO (registration), GLORYS (Copernicus Marine account) and ERA5 through the CDS (token): absent, so not run |
| Q2 | Argo, Sargasso Sea box 30-34°N, 68-62°W, 2005-2025 | de Boyer Montégut et al. (2004) climatology for the box: deepest mixed layers in February-March of about 100-250 m, shallowest in summer of about 15-30 m |
| Q3 | IOOS Glider DAC deployment `ru29-20200908T1623` (Rutgers, northeast Caribbean, September-November 2020) | The plan's pass: the deployment split into transects and each gridded to a section, with the samples removed by QC flags listed, and the sections shown linked to the track |
| Q12 | NOAA OISST v2.1 daily (keyless; Copernicus Marine absent), western Mediterranean, May-December 2022, Hobday et al. (2016) definition | The 2022 western Mediterranean event as reported (Guinaldo et al. 2023; Pirro et al. 2024): long-lasting heatwave conditions from May through the autumn, strong to severe in summer, with surface anomalies of several °C; Pirro et al. also find the warming reaching the subsurface in Argo |
| Q11 | BGC-Argo oxygen (DOXY), Arabian Sea 50-78°E, 5-26°N, 2016-2025; ocean colour over the Gulf of Maine for the bloom half | Arabian Sea OMZ: a thick suboxic layer (O₂ near zero) from about 100-150 m to about 1000 m, most intense in the north and northeast (e.g. Morrison et al. 1999; Acharya & Panigrahi 2016). Bloom: a spring bloom whose start varies by weeks from year to year |
| Q26 | ECMWF HRES 2 m temperature, October 2026 (open data), at Heathrow, Charles de Gaulle, Frankfurt, Madrid-Barajas and Rome-Fiumicino | ECMWF's published verification for 2 m temperature: errors of about 1-2 K in the first days, growing to about 3-4 K by day 10, with station biases mostly within ±1 K except where grid and site differ |
| Q10 | NOAA CO-OPS tide gauge 9452210, Juneau, Alaska | Relative sea level falling by about 13 mm/yr (NOAA) while the land rises by about 15 mm/yr from glacial isostatic adjustment (GPS-constrained, Hu & Freymueller 2019); their difference near the global-mean rise |
| Q6 | NOAA CO-OPS current meter cb0102 (Cape Henry LB, Chesapeake Bay mouth, ADCP) and the Sewells Point tide gauge 8638610, March-May 2025 | NOAA's published harmonic constants: cb0102 bin 4 (6.5 m) M2 major 59.1 cm/s, minor 1.7 cm/s, flood azimuth 294.7° true (nearly rectilinear); Sewells Point M2 0.350 m, N2 0.080, S2 0.064, K1 0.051 m |

## Qualification results

Filled in as each row runs. A tool-directed prompt is recorded as "mechanism
verified with a tool-directed prompt; not evidence of agent judgement".

Stack: `CLIO_CLOUD_ISOLATE=1 CLIO_UI_DIR=<gact-tui> bash scripts/cloud/stack.sh start`
(own config, data, state and cache under `.cloud-run/xdg`), agent `clio/science`
with the row's plugin added (`PATCH /v1/sessions/{sid}/agent`), provider
`claude_code`, model `claude-sonnet-5-5`, approval mode bypass. Prompts are plain
requests; a place or period the plan leaves as "here" is named. Turns that make
views run with the web UI open on the conversation (a client must advertise its
catalogs before a surface can be created; a turn posted with no client attached is
refused with a typed reason, which is how the first Q1 view turn ended).

| Q | Session | Verdict | Evidence |
| --- | --- | --- | --- |
| Q1 | `sess_fb79887aba8f` | **Pass (science); views pass on a follow-up.** From the plain prompt the agent read the 145 A16N casts (`ctd_read`), converted to TEOS-10, assigned water masses against the built-in Emery & Meincke definition, built the section and named NADW (filling the deep column, ~1500-4000 dbar), AAIW (the low-salinity layer ~600-1200 dbar south of ~10°N), Mediterranean Water (intermediate salinity maximum) and AABW-influenced bottom water (CT below 1 °C in the south), matching the reference. It flagged, unprompted, that the definition misassigns Mediterranean and South Atlantic Central Water north of ~45°N and left 23% unassigned rather than forcing them. Its first answer was a static figure; asked for something to explore, it built `clio.section.v1` beside a `clio.chart.v1` T-S diagram with σ0 contours, bound to one `profile` selection, and checked the rendered view itself. Linking by click is not yet verified (the capture's click landed outside the plot). Fixed from this run: the section and profile stack drew at their fallback size before layout (gact-tui `1b11399`). | `.cloud-run/shots/q01-views-1440-2-views.png`, `q01-views-390-2-views.png`; transcript in the session |
| Q18 | `sess_0804dba02196` | **Pass, after two server fixes found by it.** Bergen, 72 h: effectively 0% (no member above 1 mm; 51-member AIFS ENS, 00 UTC 17 Nov 2026, source and licence stated), cross-checked against another forecast. The IFS ENS fetch failed (`coordinate 'number' not present in all datasets`: the open-data index had no control member for `tp`), and `exceedance`'s fixed metre-to-mm factor gave nonsense on AIFS kg m-2 until the agent noticed and corrected it. Both fixed in clio-kit `d17fcf96` (empty member type skipped and reported; units read from the field). | session transcript; `.cloud-run/qual/q18` |
| Q21 | `sess_001cd860cdb6` | **Partial.** The agent fetched 1991-2020 hourly ERA5 at the nearest cell through ECMWF's keyless point route (the vendored `ecmwf-cds-ads` skill), checked completeness (262,992 hours), and ran `threshold_events` with a stated definition (daily max, calendar-day 90th percentile, 15-day window, 3 days). Both reference events are found: 29 Jul-2 Aug 2003 and 2-5 Aug 2018 (peak 4 Aug). Peaks are ~34.7 °C against 44 °C at Lisbon stations, because the ERA5 cell is coastal, which the agent said. Not met: the plan's "validated and costed before submission" is a CDS request estimate, and no CDS token is present, so no CDS request was made. | `.cloud-run/qual/q21/lisbon_heatwaves_events.csv` |
| Q24 | `sess_d240ca72ac2c` | **Pass, after three fixes found by it.** Plain prompt (Storm Ciara cold front, 9 Feb 2020): ERA5 pressure levels (`era5_fetch`), a section along 52.5°N from 5°W to 2°E at 12 UTC (`cross_section`), soundings in west Wales and Norfolk (`soundings`), an instability table, and a surface with `clio.section.v1`, two skew-T `clio.chart.v1` from the `skew-t-chart` skill and a data table. Behind the front: MUCAPE ~30 J/kg, LI +3.1, CIN -66; ahead: no CAPE, LI +7.9; a stable winter windstorm, as the reference expects. The agent said the front is weak in the θe section at this time and path and placed it from the 850 hPa gradient. Found and fixed: section rows drawn in file order (upside down; clio-agent `e2a8aee`), soundings failing on a box across 0° (clio-kit `b6e68f3e`), and the skew-T spec's pressure axis reversed twice (marketplace `skew-t` fix); asked in plain words to redraw, the agent found the reversed axis itself. At 390 px the agent's two skew-Ts sit in one `Row` and clip; the data table's sticky header draws over the composer (gact-tui, not changed here). | `.cloud-run/shots/q24-final-1440-3-skA.png`, `q24-final-1440-4-sec.png`, `q24-final-390-3-skA.png` |
| Q28 | `sess_719b536ca09b` | **Pass (numbers); views not made.** Three models, one per institution, r1i1p1f1, SSP2-4.5, JJA tasmax over 10°W-3.5°E, 36-44°N, 2081-2100 against 1995-2014: MPI-ESM1-2-LR +2.26 °C, IPSL-CM6A-LR +3.68 °C, CanESM5 +3.96 °C, mean +3.30 °C, spread 1.7 °C, within the AR6 reference. The agent said the box includes sea and that these are mean maxima, not extremes. It made no map or per-model series view (the plan's must-see), and its `tas` run failed (`Index must be monotonic for resampling`), fixed in clio-kit `b6e68f3e`. | `.cloud-run/qual/q28/iberia_tasmax/` |
| Q2 | `sess_4e17b48e8ef5` | **Pass (science); views not made; one fix found.** 2,802 Argo profiles in the box, 2005-2025, standard QC, TEOS-10, MLD by a stated 0.03 kg m-3 criterion from 10 dbar and N² over 20-200 dbar. Median MLD ~105-112 dbar in January-February and ~13 dbar in June-July, against the reference's ~100-250 and ~15-30 (summer slightly shallower). Trends stated with two estimators, their disagreement, the sampling imbalance and the correlation between a float's profiles; no robust change claimed. `argo_fetch` failed writing argopy's cache in the read-only home (the agent then fetched through argopy in its own code); fixed in clio-kit (argopy caches beside the server's outputs). With the UI open the agent made no profile stack or map (the plan's must-see). | `.cloud-run/qual/q02/` |
| Q22 | `sess_89644c680fd7` | **Pass (science); views not made.** IVT from ERA5 vapour fluxes, objects above 250 kg m-1 s-1 and 150,000 km² lasting at least 12 h (`detect_blobs`), landfall on a stated coastal strip, ERA5 rain per object. Found the late-December to mid-January family (23 Dec-1 Jan, 2-8, 8-13, 14-16 Jan objects; the long ones merge successive rivers, as the agent said) and the February and 9-15 March events, matching CW3E's season. It did not compare with a published catalogue itself, and said so. No map with the time axis was made. | `.cloud-run/qual/q22/ar/landfalling_ars.csv` |
| Q23 | `sess_3cd596c59682` | **Pass (science); views not made; slow.** ERA5 Z500 at 12 UTC, a stated calendar-day anomaly against 1991-2020, objects of at least +200 m and 10^6 km² lasting at least 5 days (`detect_blobs`, `blob_stats`). Found persistent high-latitude blocking dominating both winters (Greenland and North Atlantic; 27 Jan-27 Feb 2010 the longest, 32 days), as Cattiaux et al. (2010) describe for the record negative-NAO winter, and separated the four episodes over Europe proper. It said its definition is anomaly persistence, not the Tibaldi-Molteni index, and that large objects may merge highs. 42 minutes, mostly 31 `era5_fetch` calls (the tool caps a request's size, and the agent fetched per period); `blob_stats` longitude extents are wrong for objects crossing 0° (the agent noticed and used centroids). | `.cloud-run/qual/q23/blocking/out/` |
| Q28 rerun | `sess_afcaef363378` | Same plain prompt in a fresh session with the plugins' new views fragment (marketplace weather 2.1.0), which the resolved prompt carries. Numbers again within the reference (MPI-ESM1-2-LR +2.26, IPSL-CM6A-LR +3.68, MIROC6 +4.51 °C; mean +3.48 °C), and the agent flagged MIROC6's 8 K warmer baseline. It again made no view and said so. One sample, recorded as evidence that the fragment does not by itself change the agent's choice on a request that does not mention seeing anything; no stronger wording was added (no behavioural handcuffs in prompts). | session transcript |
| Q14 | `sess_8537be6a4e13` | **Pass (mask check, grid editor, edit log, replay); MOM6 set-up and test run not run.** Plain prompt: the agent loaded `regional-ocean-model` and `edit-model-grid`, opened the grid in the editor (`grid_open`; the MCP App renders in CLIO with the field, hash and edit controls), and checked the mask with 4-neighbour connectivity: both planted problems found (the 35 m isolated cell at [25,31] and the five-cell 20 m strip, "more like a flooded artefact"), plus three natural single cells and nine one-cell channels. It edited nothing and asked the person to decide each one (plan rule 2). Told to fill the isolated cells, the strip and two very shallow channels and keep the deeper channels, it applied one ordered `grid_edit` list: 7 edits, 11 cells, each with its reason, base `cec8d1e2...` to result `c1d170ab...`, base file untouched. Given a regenerated grid (depths +5%), it used `grid_replay`, reported the different base, confirmed every edit changed the same number of cells, and re-checked the result (one connected ocean, the seven kept channels). It missed that the replay wrote its own log beside the result. Not run: `mom6_preflight` against a run directory (none exists without regional-mom6's set-up), and the set-up and test run, which need TPXO, GLORYS and ERA5 accounts. Editor colouring: land (0) and very shallow cells read alike on viridis. | `.cloud-run/shots/q14-editor-1440-2-app.png`, `q14-editor-390-2-app.png`; `.cloud-run/qual/q14/*.edits.json` |
| Q3 | `sess_2deea153331a` | **Pass (science and views), after two fixes found by it.** Plain prompt. The agent fetched the deployment (`glider_fetch`: 1,060 profiles, a provenance record), split it at heading reversals (16 transects of at least 10 profiles; 720 profiles in shorter legs belong to none, which it said), gridded each (`section`, 16 calls), and built a surface: the track on `clio.layered-map.v1` with the time axis, eight sections in tabs (`clio.section.v1`, CT/SA/σ0) and a T-S `clio.chart.v1` with σ0 contours for the two longest legs, coloured by transect. QC: provider, spike and density-inversion flags remove 111,000 of 760,666 samples, listed in total but not per section. It reported, from its own capture, that the map looked empty and that it had not checked the T-S diagram or the linking. Found and fixed: the layered map opened on the whole world, so the 100 km deployment was one dot (gact-tui `e8d21a8`, it now fits its data), the section canvas held its first measured width so the T-S chart beside it got 0 px (gact-tui `9040505`, the canvas no longer sizes the row), and a workspace path given as `sectionUri`/`gridUri` was refused instead of registered like every other viewer file (clio-agent `0409168`). Its other workaround, promoting section files itself, met CLIO's per-turn cap of eight promoted outputs (existing design; reported truthfully). | `.cloud-run/shots/q03-fit3-3-map.png`, `q03-row-3-row.png`, `q03-phone-4-map.png`, `q03-phone-5-sec43.png` |
| Q12 | `sess_c7f58f188001` | **Pass (science, at one point); views not made; no subsurface check.** Plain prompt. With no data in the workspace the agent offered the Copernicus plugin (two identical offer cards in parallel); the person said "Not now", and it stopped, said why it would not answer from memory and named public sources (NOAA OISST). Told "Use NOAA OISST, that's fine", it fetched daily OISST v2.1 at 40.125°N, 4.125°E (Balearic Sea) in its own scripts and ran `mhw_point` with Hobday et al. (2016) stated (90th percentile, 1982-2011, 11-day window, 5 days, 2-day gaps; categories after Hobday et al. 2018): a short event 13-25 May and a main event from 31 May still running on 31 December (at least 215 days), peak 4.38 °C on 16 June, mean 2.57 °C, about 552 °C·days, category Strong (2.89 threshold multiples). It repeated the run with 1991-2020, where the same year splits into four events, still Strong, and said which conclusion depends on the baseline. This agrees with the reference. It disclosed about 1,400 baseline days missing from its download (the NOAA server failed often; it fetched in pieces), which is its fetch, not the product. Not done: a regional (gridded) answer, a view, and the Argo subsurface comparison. | `.cloud-run/qual/q12/results/`; `.cloud-run/shots/q12-dismiss-2-dismissed.png` |
| Q6 | `sess_af69818aa140` | **Pass (science); ellipses as a static figure; spectra not made.** Plain prompt. The agent fetched 6-minute currents (bin 4) and water levels from CO-OPS in its own code and ran `harmonics` (UTide, automatic constituent selection, SNR ≥ 2) on both. Against NOAA's published constants: M2 current 0.54 m/s against 0.59, minor 0.04, major axis 150-156° anticlockwise from east, which is NOAA's 294.7° true; Sewells Point M2 35.0 ± 0.3 cm against 35.0, N2 9.1 against 8.0, S2 7.1 against 6.4, K1 4.4 against 5.1 cm. Tides explain 76% of current and 78% of sea-level variance. It stated the three-month record's limits (K1/P1 and long-period terms poorly separated) and that it did not compare with NOAA's constants. The ellipses are a matplotlib figure registered as an artifact, not a linked view; no rotary spectra or current series view (O6's must-see). | `.cloud-run/qual/q06/out/` |
| Q4 | — | **Not run: credentials.** GLORYS (Drake Passage transport) comes from Copernicus Marine; the `copernicus` connector has no account saved in this session. The `transport` tool and the connector path are tested offline (clio-kit tests); nothing here is evidence for this row. | — |
| Q7 | — | **Not run: credentials.** One year of CMEMS sea-level anomaly needs a Copernicus Marine account (as Q4). | — |
| Q8 | — | **Not run: credentials and Q7.** SWOT passes come from PO.DAAC through NASA Earthdata (no login saved) and depend on Q7's eddy. | — |
| Q9 | — | **Not run: credentials.** GLORYS currents for the backward trajectories need a Copernicus Marine account. | — |
| Q13 | — | **Not run: credentials.** The current operational forecast over the Q7 region is a Copernicus Marine product. | — |
| Q16 | — | **Not run: desktop application.** "ParaView opens on the eddy" is the person's ParaView; a cloud session cannot reach a desktop application (and Q16's eddy output depends on credentialed model data). | — |
| Q17 | — | **Not run: desktop application.** The QGIS project is for the person to adjust in their QGIS; not reachable from a cloud session. | — |
| Q25 | — | **Not run: hardware.** CorrDiff needs a GPU; this session has 4 Xeon cores at 2.1 GHz, 15 GB of memory and no GPU. | — |
| Q10 | `sess_8df19dd3df83` | **Partial: correct comparison from published numbers; the local trend was not computed from the gauge.** Plain prompt. The agent's first `web_search` waited an hour on CLIO's one-time consent card for the search source (approved as the person would; this is designed behaviour, not a hang). It then reported relative sea level −13.1 ± 0.35 mm/yr (NOAA's 2016 trend, read from a secondary site because NOAA's page and API did not give it the number), uplift of about 15 mm/yr (Juneau Climate Report, after Hu & Freymueller 2019), the implied ocean rise of about +2 mm/yr, and the co-located GNSS station JNU1 without a velocity, stating each as secondary and what would tighten it. This agrees with the reference. Not done: fetching the gauge's monthly means and computing the trend itself (O11's "local trend"); the ocean tools were not used. | session transcript |
| Q26 | `sess_16b97532cbe3` | **Pass (science); views not made; one fix found by it.** Plain prompt. `station_fetch` failed with a CSV parser error: NOAA retired ISD in 2025 and the old address now redirects to an HTML page. The agent checked, said so, took METARs from the Iowa Environmental Mesonet instead, fetched the 00 UTC HRES runs for October from ECMWF's open-data archive on AWS (1 of 1,271 fields missing, stated), interpolated bilinearly and matched to the nearest observation within 30 min (5,439 pairs). RMSE about 1 °C at 24 h at four stations, 2.1-2.8 °C at 7 days and 3.5-4.5 °C at 10 days; Heathrow and Paris 0.5-0.7 °C too cold; Rome a flat +1.2 to +1.6 °C bias and ~2 °C error from 0 h, which it attributed (untested, said so) to the coastal grid cell. Within the reference. Fixed from this run: `station_fetch` reads NOAA GHCN-hourly, takes ICAO codes and removes values with a failed quality code (clio-kit `a4882fb4`; the five airports return October 2026 data). Not made: the stations on a map coloured by error and the per-station error series (W9's must-see). | `.cloud-run/qual/q26/oct2026/summary_by_lead.csv` |
| Q11 (OMZ) | `sess_dbd061c36091` | **Partial: extent right, change correctly not claimed; views not made; one fix found.** Plain prompt. Every `argo_fetch` call failed writing argopy's cache in the read-only home: the earlier fix set argopy's option, but argopy fixes the folder at import (default arguments and a singleton keep it), and the BGC path uses those. The agent ran argopy itself with the cache moved: 178,843 good adjusted DOXY samples, profiles interpolated to 5 dbar, O₂ < 20 and < 5 µmol/kg layers by stated thresholds. Extent: the OMZ in nearly every occupied 2° box north of ~14°N, top at a median 115-130 dbar, thickness 700-1150 m (capped by the 1500 dbar profiles), minima near zero (sensor-limited, said so); this matches the reference. On change it refused a trend: 6-9 floats a year in 2016-2019, one in 2020-2021, none passing in 2022, two after, and the late "weakening" sits at the basin's edge; it said what would make a trend defensible. Its per-year map is a PNG it did not look at; no profile stack or OMZ-thickness map view (O12's must-see). Fixed from this run: argopy is imported with its cache in the server's folder (clio-kit `0c5d3257`, checked under a read-only `/root`: before, the BGC fetch fails; after, it returns rows). The bloom half (O13) is not run. | `.cloud-run/qual/q11/argo/omz_profile_metrics.csv`, `omz_box_extent.csv` |
