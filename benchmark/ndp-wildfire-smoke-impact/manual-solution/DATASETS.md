# Verified live datasets (manual solve, 2026-06-06)

All discovered through the NDP catalog API, then queried directly on their
backing Esri FeatureServers. NDP catalog API: `http://155.101.6.191:8003`
(endpoints `/organization`, `/search`; catalogs `global`/`local`/`pre_ckan`;
server is flaky — use retry/backoff).

## 1. Active fire perimeters — NDP org `nifc`

- Dataset: **WFIGS Current Interagency Fire Perimeters**
- FeatureServer:
  `https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/WFIGS_Interagency_Perimeters_Current/FeatureServer/0`
- Also offered: GeoJSON, CSV, KML, Shapefile.
- Live count at solve: **69** perimeters (`1=1`).
- Key fields (verified): `poly_IncidentName`, `poly_GISAcres`,
  `attr_PercentContained`, `attr_FireCause`, `attr_POOState` (e.g. `US-CA`),
  `attr_POOCounty`, `attr_FireDiscoveryDateTime`, `attr_PrimaryFuelModel`,
  `attr_TotalIncidentPersonnel`, `attr_IncidentTypeCategory` (`'WF'` = wildfire
  vs prescribed burns), `attr_IncidentComplexityLevel`, geometry = perimeter
  polygon.

## 2. Smoke forecast — NDP org `cal-oes`

- Dataset: **NWS 48 hour Smoke Forecast** (national NDGD gridded product)
- FeatureServer:
  `https://services9.arcgis.com/RHVPKKiFTONKtxq3/arcgis/rest/services/NDGD_SmokeForecast_v1/FeatureServer/0`
- Live count at solve: **4,889** polygons nationally.
- Key fields: `smoke_classdesc` (concentration class, e.g. `"3 - 25"` µg/m³),
  `referencedate`, `todate` (epoch ms), geometry = polygon grid cells.
- Currently dense over: CA Sierra (~-120,37 and -118,35), Pacific NW (~-123,43),
  Alberta (~-113,60), Mexico (~-103,19). NOT over the largest contained fires —
  this is why impact-based selection matters.

## 3. Air quality — NDP org `cal-oes`

- Dataset: **AirNow Air Quality Monitoring Data (Current)**
- FeatureServer:
  `https://services.arcgis.com/cJ9YHowT8TU7DUyn/arcgis/rest/services/Air%20Now%20Current%20Monitor%20Data%20Public/FeatureServer/0`
- Live count at solve: **4,422** monitors.
- Key fields: `OZONEPM_AQI`, `OZONEPM_AQI_LABEL`, `OZONEPM_AQI_SORT`,
  `OZONE_AQI`, `Latitude`, `Longitude`, `Elevation`, `LocalTimeString`,
  `AQSID`, `DataSource`.

## Spatial query pattern used

Esri `/query` with `geometryType=esriGeometryEnvelope`, `inSR=4326`,
`spatialRel=esriSpatialRelIntersects`, `f=geojson`. Render reprojected to 3857
for a CartoDB Positron basemap (contextily).

## Reproduce

`fuse_wildfire_smoke_air.py` in this folder. Run:
`uv run --with geopandas --with shapely --with contextily --with matplotlib --with httpx python fuse_wildfire_smoke_air.py`

## Honest findings

- Largest Western WF = Seven Cabins (NM, 71% contained): 0 smoke, 3 monitors.
- Largest CA WF = Santa Rosa Island (100% contained, Channel Islands): 0 smoke,
  28 monitors. Both prove "largest" ≠ "impactful".
- In June, most large perimeters are contained; the live demo strength depends
  on selecting the fire under active smoke, not the biggest one.
