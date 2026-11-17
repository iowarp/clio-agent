# nifc (sess_408083696c98)

**user**: How many wildfires have burned within 20 km of Lake Tahoe according to NDP's national fire perimeter data, and in which years?

- call `load_skill` {"file": "", "files": null, "skill_id": "fire-perimeters"}
- call `connected_data_search_ndp` {"endpoint_id": "", "offset": 0, "organization": "", "query": "fire perimeters"}
- call `connected_data_search_ndp` {"endpoint_id": "", "offset": 0, "organization": "nifc", "query": "wildfire perimeter"}
- call `geo_geocode` {"query": "Lake Tahoe"}
  - result: # Skill: fire-perimeters
Skill directory (use as SKILL_ROOT): /home/user/clio-agent/.cloud-run/xdg/config/clio-agent/plugins/snapshots/clio/bcf7156332033b93/plugins/wildfire/skills/fire-perimeters
# Fire perimeters and fire history

Perimeter collections mix sources, dates and versions. A sum of pol
  - result: {"original_chars": 320907, "preview": "{\"datasets\": [{\"access\": null, \"description\": \"This is an export of the data archived from the 2022 National Incident Feature Service. Sensitive fields and features have been removed. Each edit to a feature is captured in the Archive. The GDB_FROM and GD
  - result: [{"display_name":"Lake Tahoe, Placer County, California, United States","lat":39.0885405,"lon":-120.0503528,"bbox":[-120.1639382,38.9281733,-119.9260578,39.2490133],"type":"lake","importance":0.5754997470294765,"provenance":"osm_nominatim"}]
  - result: {"original_chars": 526304, "preview": "{\"datasets\": [{\"access\": null, \"description\": \"This service displays post-fire debris flow data for fires that have occurred in 2018. Post-fire debris-flow likelihood, volume, and combined hazards are estimated at both the drainage-basin scale and in a s
- call `run_code` {"background": false, "code": "r = await tools.ndp.get_dataset_details(dataset_id=\"6b717a87-f5d2-4aa4-8251-34ccd39cfd66\")\nimport json\ns=json.dumps(r,default=str)\nprint(len(s))\nimport re\nprint(re.findall(r'https?://[^\"\\s]+',s)[:30])", "label": ""}
- call `run_code` {"background": false, "code": "r = await tools.ndp.get_dataset_details(dataset_id=\"6b717a87-f5d2-4aa4-8251-34ccd39cfd66\")\nprint(str(r)[:3000])", "label": ""}
- call `ndp_get_dataset_details` {"dataset_id": "6b717a87-f5d2-4aa4-8251-34ccd39cfd66"}
- call `ndp_get_dataset_details` {"dataset_id": "6b717a87-f5d2-4aa4-8251-34ccd39cfd66"}
  - ERROR result: 2 validation errors for call[get_dataset_details]
dataset_identifier
  Missing required argument [type=missing_argument, input_value={'dataset_id': '6b717a87-...4aa4-8251-34ccd39cfd66'}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.12/v/missing_argument
dataset_id
  - ERROR result: 2 validation errors for call[get_dataset_details]
dataset_identifier
  Missing required argument [type=missing_argument, input_value={'dataset_id': '6b717a87-...4aa4-8251-34ccd39cfd66'}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.12/v/missing_argument
dataset_id
  - result: (New kernel: the previous one had stopped; earlier variables are gone.)

Error:
Traceback (most recent call last):
  File "<script>", line 1, in <module>
ToolError: 2 validation errors for call[get_dataset_details]
dataset_identifier
  Missing required argument [type=missing_argument, input_value={'
  - ERROR result: Error:
Traceback (most recent call last):
  File "<script>", line 1, in <module>
ToolError: 2 validation errors for call[get_dataset_details]
dataset_identifier
  Missing required argument [type=missing_argument, input_value={'dataset_id': '6b717a87-...4aa4-8251-34ccd39cfd66'}, input_type=dict]
    
- call `run_code` {"background": false, "code": "r = await tools.ndp.get_dataset_details(dataset_identifier=\"6b717a87-f5d2-4aa4-8251-34ccd39cfd66\")\nimport json,re\ns=json.dumps(r,default=str)\nprint(len(s))\nprint(set(re.findall(r'https?://[^\"\\\\\\s]+',s)))\nprint(s[:1500])", "label": ""}
- call `ndp_get_dataset_details` {"dataset_identifier": "6b717a87-f5d2-4aa4-8251-34ccd39cfd66"}
  - result: {"original_chars": 19568, "preview": "{\"dataset\":{\"id\":\"6b717a87-f5d2-4aa4-8251-34ccd39cfd66\",\"name\":\"interagencyfireperimeterhistory-all-years-view\",\"title\":\"InterAgencyFirePerimeterHistory All Years View\",\"owner_org\":\"nifc\",\"notes\":\"<div>Historical Fires<br /></div><p style='m
  - result: 19894
{'https://www.nwcg.gov/sites/default/files/stds/WildlandFirePerimeters_definition.pdf</font></a><br', 'https://frap.fire.ca.gov/mapping/gis-data</span></a><br', 'https://nifc.maps.arcgis.com/home/item.html?id=098ebc8e561143389ca3d42be3707caa</font></a><br', 'https://data-nifc.opendata.arcgis.c
- call `geoservices_fetch_features` {"bbox": [-120.4, 38.74, -119.69, 39.43], "max_features": 5000, "output_path": "/home/user/clio-agent/.cloud-run/ndp-ws/organizations-nifc-203413/tahoe_fires_bbox.geojson", "url": "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/InterAgencyFirePerimeterHistory_All_Years_View/FeatureServer/0"}
  - result: {"content": [], "structuredContent": {"bounds": [-120.906279, 38.617885, -119.630652, 39.579967], "feature_count": 388, "geometry_types": {"MultiPolygon": 29, "Polygon": 359}, "output_path": "/home/user/clio-agent/.cloud-run/ndp-ws/organizations-nifc-203413/tahoe_fires_bbox.geojson", "paging": {"com
- call `shell_bash` {"command": "cd /tmp && mkdir -p tahoe_work && cd tahoe_work && curl -sS \"https://nominatim.openstreetmap.org/search?q=Lake+Tahoe&format=geojson&polygon_geojson=1&limit=3\" -H \"User-Agent: ndp-agent\" -o lake.json; python3 -c \"\nimport json;d=json.load(open('lake.json'))\nfor f in d['features']:print(f['properties'].get('display_name'),f['geometry']['type'],f['properties'].get('type'))\n\"; pyt
  - ERROR result: status=failed
- call `shell_bash` {"command": "cd /tmp/tahoe_work && cat > a.py <<'EOF'\nimport json,sys,collections\nfrom shapely.geometry import shape\nfrom shapely.ops import transform\nfrom pyproj import Transformer\nt=Transformer.from_crs(4326,32610,always_xy=True).transform\nlake=shape(json.load(open('lake.json'))['features'][0]['geometry'])\nL=transform(t,lake); print('lake area km2',round(L.area/1e6,1))\nB=L.buffer(20000)\
  - ERROR result: status=failed
**assistant**: Fix sort with None-safe key.

- call `shell_bash` {"command": "cd /tmp/tahoe_work && sed -i 's/^rows.sort()/rows.sort(key=lambda r:tuple(str(x) for x in r))/' a.py && \"$CLIO_UV\" run --no-project --python \"$CLIO_PYTHON\" --with shapely --with pyproj python -I -W ignore a.py 2>/dev/null"}
  - result: {"original_chars": 40137, "preview": "{\"handle\": \"task_bd7989d7d62649aeb7d469866021f866\", \"status\": \"completed\", \"pending\": false, \"result\": {\"meta\": {\"io.modelcontextprotocol/serverInfo\": {\"name\": \"CLIO shell\", \"version\": \"4.1.2\"}}, \"task_id\": \"o5epdij-3daEyfSPkc2oqaghpvj
- call `shell_bash` {"command": "cd /tmp/tahoe_work && cat > b.py <<'EOF'\nimport json,collections\nfrom shapely.geometry import shape\nfrom shapely.ops import transform\nfrom pyproj import Transformer\nt=Transformer.from_crs(4326,32610,always_xy=True).transform\nL=transform(t,shape(json.load(open('lake.json'))['features'][0]['geometry']))\nB=L.buffer(20000)\nfc=json.load(open('/home/user/clio-agent/.cloud-run/ndp-ws
  - result: {"handle": "task_eee13822b8ef4c9aae853a73a2e4fb77", "status": "completed", "pending": false, "result": {"meta": {"io.modelcontextprotocol/serverInfo": {"name": "CLIO shell", "version": "4.1.2"}}, "task_id": "jncUCEX-G_uOtgfLYJxbHbP2h0ewJ88MjAnwNlzCfho", "status": "completed", "created_at": "2026-11-
**assistant**: **About 158 distinct wildfires burned within 20 km of Lake Tahoe, in 73 different years between 1910 and 2019.** This comes from NIFC's InterAgencyFirePerimeterHistory (All Years View) in the NDP catalog, and it excludes prescribed burns. The layer has no 2020 or later fires in this area (see the caveats).

**Method**
- I fetched all 388 perimeters in a box around the lake and kept those that touch a 20 km buffer around the lake outline.
- The lake outline is the OpenStreetMap polygon (about 498 km²), so distance is measured from the shoreline, not from a point.
- That left 255 polygons, but many are copies of the same fire (USFS, BLM and CAL FIRE versions of the same perimeter).
- I merged same-year polygons that overlap by more than half of the smaller one, which gives 158 fires. Including 8 prescribed burns would give 166.

**Fires per year** (a year is absent if no fire was found)

| Decade | Fires per year |
|---|---|
| 1910s | 1910: 1, 1915: 1, 1916: 1, 1917: 1, 1918: 3, 1919: 1 |
| 1920s | 1920: 1, 1922: 1, 1924: 5, 1925: 1, 1926: 6, 1928: 4, 1929: 1 |
| 1930s | 1932: 1, 1935: 2, 1939: 1 |
| 1940s | 1943: 2, 1944: 1, 1947: 2, 1948: 1, 1949: 6 |
| 1950s | 1950: 1, 1951: 2, 1955: 1, 1957: 1, 1959: 1 |
| 1960s | 1960: 3, 1961: 1, 1965: 1, 1966: 2, 1967: 1, 1968: 2 |
| 1970s | 1973: 2, 1976: 1, 1977: 1, 1978: 1, 1979: 3 |
| 1980s | 1980: 1, 1981: 3, 1983: 2, 1984: 5, 1985: 1, 1986: 1, 1987: 4, 1988: 2, 1989: 1 |
| 1990s | 1990: 1, 1992: 2, 1994: 3, 1995: 2, 1996: 4, 1997: 1, 1998: 1, 1999: 2 |
| 2000s | 2000: 2, 2001: 5, 2002: 4, 2003: 3, 2004: 3, 2005: 1, 2006: 3, 2007: 3, 2008: 2, 2009: 1 |
| 2010s | 2011: 3, 2012: 2, 2014: 4, 2016: 6, 2017: 5, 2018: 5, 2019: 3 |

Fires closest to the lake include the 2007 Angora (3,070 acres, about 0.7 km from the shore), 2016 Emerald and 1994 Cascade (both about 0.1 km), 2002 Kiva (0 km, touching the shoreline) and 1984 Glenbrook (0.4 km). The biggest nearby are the 2001 Martis fire (about 14,000 acres, 5 km out) and the 2004 Waterfall fire (about 8,800 acres, 5.7 km out).

**Caveats**
- **No data after 2019.** The layer's description says it runs through 2021, but I found no fires from 2020 onward. Large recent fires such as the 2021 Caldor fire may be missing, so the count is probably low for recent years.
- **Incomplete archive.** Agencies set their own minimum fire sizes, and some old perimeters are generalized. Treat early-century counts as a lower bound.
- **The merge is approximate.** Overlapping same-year fires could be merged wrongly, and one fire mapped differently by two agencies could be counted twice. Some entries have no name or fire type, so they could not be classified.
- **Distance is shoreline to perimeter.** A fire counts if any part of its perimeter is within 20 km, not only its origin point.

Files are in `/home/user/clio-agent/.cloud-run/ndp-ws/organizations-nifc-203413/`:
- [tahoe_fires_bbox.geojson](workspace-file:/home/user/clio-agent/.cloud-run/ndp-ws/organizations-nifc-203413/tahoe_fires_bbox.geojson) holds the 388 fetched perimeters.
- The lake polygon and my analysis scripts (`lake.json`, `a.py`, `b.py`) are in `/tmp/tahoe_work`, not in the workspace.

