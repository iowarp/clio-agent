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
| regional-mom6 install | 1.0.4 installs from PyPI but imports `xesmf`, which needs ESMF/esmpy; ESMF has no PyPI wheel and no Ubuntu 24.04 package. Needs a source or Spack build of ESMF for O18/Q14. | `ModuleNotFoundError: No module named 'ESMF'` |

## Needs user

- **Copyleft workers.** Approve shipping the marEx (AGPL-3.0-or-later) and
  py-eddy-tracker (GPL-3.0) workers as separate processes inside clio-kit, or
  choose to make them a separately installed extra.
- **Credentials** for Copernicus Marine, CDS, ECMWF Polytope, NASA Earthdata
  and TPXO come through CLIO's connector card; none are present in this
  session.

## Qualification fixtures

Chosen before the work, with the reference each result is compared against.
Rows still marked "to choose" are filled in as their work starts.

| Q | Fixture | Reference values |
| --- | --- | --- |
| Q1 | GO-SHIP A16N, 2013 occupation (CCHDO expocode 33RO20130803), CTD casts | Talley et al. (2011), *Descriptive Physical Oceanography*, Atlantic chapter: AAIW as the intermediate salinity minimum (~700–1200 dbar) south of ~20°N; Mediterranean Overflow Water as the intermediate salinity maximum near 30–40°N; NADW between ~1500 and ~4000 dbar (S ≈ 34.9–35.0); AABW-influenced bottom water (θ < ~2 °C) in the south |

## Qualification results

Filled in as each row runs. A tool-directed prompt is recorded as "mechanism
verified with a tool-directed prompt; not evidence of agent judgement".
