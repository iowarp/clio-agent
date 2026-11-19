# opentopography (sess_f3fdebef57f1)

**user**: Using NDP, get a bare-earth elevation model of the area around Wolverton in Sequoia National Park and tell me how steep it is.

- call `connected_data_search_ndp` {"endpoint_id": "", "offset": 0, "organization": "opentopography", "query": "Sequoia DEM"}
- call `connected_data_search_ndp` {"endpoint_id": "", "offset": 0, "organization": "", "query": "Sequoia lidar"}
- call `geo_geocode` {"countrycodes": "us", "limit": 3, "query": "Wolverton, Sequoia National Park, California"}
  - result: {"datasets": [{"access": null, "description": "Lidar data was collected over the Kern Plateau in the Sequoia National Forest, California for the USDA Forest Service between August 13th  18th, 2011. The Kern Plateau project area covers over 134,00 acres (>544 km<sup>2</sup>).", "endpoint_id": "ndp",
  - result: {"datasets": [], "next_offset": null, "total": 0, "warnings": []}
  - ERROR result: No matching location found for query: 'Wolverton, Sequoia National Park, California'
- call `connected_data_ndp_organizations` {"endpoint_id": "", "query": "opentopography"}
- call `connected_data_search_ndp` {"endpoint_id": "", "offset": 0, "organization": "", "query": "Sierra Nevada lidar"}
- call `connected_data_search_ndp` {"endpoint_id": "", "offset": 0, "organization": "", "query": "Sequoia Kings Canyon"}
- call `connected_data_search_ndp` {"endpoint_id": "", "offset": 0, "organization": "", "query": "3DEP California"}
- call `geo_geocode` {"query": "Wolverton Trailhead, Tulare County, California"}
  - result: {"datasets": [], "next_offset": null, "total": 0, "warnings": []}
  - result: {"organizations": [{"endpoint_id": "ndp", "endpoint_name": "NDP catalog", "id": "opentopography"}, {"endpoint_id": "ndp-test", "endpoint_name": "NDP test catalog", "id": "opentopography"}]}
  - result: [{"display_name":"Wolverton Trailhead Info, Lakes Trail, Tulare County, California, 93262, United States","lat":36.5969471,"lon":-118.7345358,"bbox":[-118.7345858,36.5968971,-118.7344858,36.5969971],"type":"board","importance":9.99999999995449e-6,"provenance":"osm_nominatim"}]
  - result: {"content": [{"text": "[clio: tool_result_stored_in_file] The result was 905,997 bytes. Full result: /home/user/clio-agent/.cloud-run/xdg/state/clio-agent/workspaces/3a7785cfee43e4071dd679dc/tool-output/sess_f3fdebef57f1/result-2cb8aaf2e9293567.json", "type": "text"}], "isError": false, "stored_resu
  - result: {"content": [{"text": "[clio: tool_result_stored_in_file] The result was 750,473 bytes. Full result: /home/user/clio-agent/.cloud-run/xdg/state/clio-agent/workspaces/3a7785cfee43e4071dd679dc/tool-output/sess_f3fdebef57f1/result-525514f4339d3415.json", "type": "text"}], "isError": false, "stored_resu
- call `run_code` {"background": false, "code": "r = await tools.ndp.get_dataset_details(dataset_id=\"4d4a9021-78de-4b5e-9c5f-8c92a7700b4c\")\nimport json\ns=json.dumps(r,default=str)\nprint(len(s)); print(s[:1500])\nfor res in (r.get(\"resources\") or [])[:80] if isinstance(r,dict) else []:\n    if \"DTMarea6\" in str(res.get(\"name\")) or \".vrt\" in str(res.get(\"name\")):\n        print(res)", "label": ""}
- call `connected_data_ndp_tiles_covering` {"dataset_id": "4d4a9021-78de-4b5e-9c5f-8c92a7700b4c", "east": -118.725, "endpoint_id": "", "header_offset": 0, "north": 36.605, "south": 36.59, "west": -118.745}
  - ERROR result: This dataset appears in several catalogs; pass its endpoint_id
- call `ndp_get_dataset_details` {"dataset_id": "4d4a9021-78de-4b5e-9c5f-8c92a7700b4c"}
  - ERROR result: 2 validation errors for call[get_dataset_details]
dataset_identifier
  Missing required argument [type=missing_argument, input_value={'dataset_id': '4d4a9021-...4b5e-9c5f-8c92a7700b4c'}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.12/v/missing_argument
dataset_id
  - ERROR result: Error:
Traceback (most recent call last):
  File "<script>", line 1, in <module>
ToolError: 2 validation errors for call[get_dataset_details]
dataset_identifier
  Missing required argument [type=missing_argument, input_value={'dataset_id': '4d4a9021-...4b5e-9c5f-8c92a7700b4c'}, input_type=dict]
    
- call `connected_data_ndp_tiles_covering` {"dataset_id": "4d4a9021-78de-4b5e-9c5f-8c92a7700b4c", "east": -118.725, "endpoint_id": "ndp", "header_offset": 0, "north": 36.605, "south": 36.59, "west": -118.745}
- call `run_code` {"background": false, "code": "r = await tools.ndp.get_dataset_details(dataset_identifier=\"4d4a9021-78de-4b5e-9c5f-8c92a7700b4c\")\ns=str(r); print(len(s)); print(s[:1200])", "label": ""}
- call `ndp_get_dataset_details` {"dataset_identifier": "4d4a9021-78de-4b5e-9c5f-8c92a7700b4c"}
  - result: {"original_chars": 20016, "preview": "{\"dataset\":{\"id\":\"4d4a9021-78de-4b5e-9c5f-8c92a7700b4c\",\"name\":\"southern-sierra-nevada-critical-zone-observatory-snow-off2\",\"title\":\"Southern Sierra Nevada Critical Zone Observatory: Snow Off\",\"owner_org\":\"opentopography\",\"notes\":\"Southern S
  - result: 20738
{'dataset': {'id': '4d4a9021-78de-4b5e-9c5f-8c92a7700b4c', 'name': 'southern-sierra-nevada-critical-zone-observatory-snow-off2', 'title': 'Southern Sierra Nevada Critical Zone Observatory: Snow Off', 'owner_org': 'opentopography', 'notes': "Southern Sierra CZO Lidar flight was acquired in 2010
  - result: {"covering": [{"bounds": [-118.778692, 36.57322, -118.609444, 36.647702], "id": "0c0e1bf1-bb57-4932-8dd6-2e19b284678d", "name": "SSCZO_Aug10 - DTMarea6_fgrd_w001001.adf", "path": "3-SSCZO_Aug10 - DTMarea6_fgrd_w001001.adf"}, {"bounds": [-118.778692, 36.57322, -118.609444, 36.647702], "id": "c4e9986a
- call `run_code` {"background": false, "code": "r = await tools.ndp.get_dataset_details(dataset_identifier=\"4d4a9021-78de-4b5e-9c5f-8c92a7700b4c\")\nfor res in r.get(\"resources\",[]):\n    if \"area6_fgrd_w001001.adf\" in str(res) or \"be.vrt\" in str(res):\n        print(res)", "label": ""}
- call `search_tools` {"limit": 0, "query": "select:connected_data_connect,connected_data_download,connected_data_status"}
  - result: Loaded 3 tools; call them from your next step with the arguments their schemas give.

## connected_data_connect  (connected-data)
Connect and index an approved source folder. Returns an Indexing task handle immediately (the task-protocol skill covers handles); files are downloaded only by connected_
- call `ndp_get_dataset_details` {"dataset_identifier": "4d4a9021-78de-4b5e-9c5f-8c92a7700b4c"}
  - result: {"original_chars": 20016, "preview": "{\"dataset\":{\"id\":\"4d4a9021-78de-4b5e-9c5f-8c92a7700b4c\",\"name\":\"southern-sierra-nevada-critical-zone-observatory-snow-off2\",\"title\":\"Southern Sierra Nevada Critical Zone Observatory: Snow Off\",\"owner_org\":\"opentopography\",\"notes\":\"Southern S
  - result: The script printed nothing.

(Script finished; 1 tool call.)
- call `connected_data_connect` {"configuration": {"endpoint_id": "ndp", "resource_ids": ["0c0e1bf1-bb57-4932-8dd6-2e19b284678d", "c4e9986a-f91f-454d-9758-a5f2dcfd612b", "0862a4c4-cbb5-4d43-98f9-407b52154bac", "044ac5e9-381f-479d-b535-83c47b1cf31d", "36fdf586-0e20-4715-93c3-f75395240ed3", "d6ca5261-28f6-40c1-a71c-9cfb87bf5952"]}, "label": "SSCZO Aug10 Wolverton (Area 6) bare-earth DTM", "mode": "read_only", "provider": "ndp", "r
- call `connected_data_status` {}
  - result: {"accounts": [{"authenticated": false, "login_action": {"context": {"clio_id": "clio_579bdc6594694fd7a48adfe281301938", "workspace_id": "ws_2e11297687de"}, "name": "data_source/login/github"}, "provider": "github", "sign_in_available": true}, {"authenticated": false, "login_action": {"context": {"cl
  - result: {"accepted": true, "activity": "queued", "handle": "task_855206047757481cbd3f9c812cab896d", "kind": "Indexing", "status": "accepted"}
- call `connected_data_status` {}
- call `wait_tasks` {"interrupt_on_mail": true, "return_when": "all", "tasks": "task_855206047757481cbd3f9c812cab896d", "timeout_s": null}
  - result: {"accounts": [{"authenticated": false, "login_action": {"context": {"clio_id": "clio_579bdc6594694fd7a48adfe281301938", "workspace_id": "ws_2e11297687de"}, "name": "data_source/login/github"}, "provider": "github", "sign_in_available": true}, {"authenticated": false, "login_action": {"context": {"cl
  - result: {"outcomes": [{"handle": "task_855206047757481cbd3f9c812cab896d", "next_cursor": null, "pending": false, "result": {"created_at": "2026-11-17T20:37:17.228878+00:00", "error": null, "input_requests": null, "last_updated_at": "2026-11-17T20:37:17.504888+00:00", "meta": {"io.modelcontextprotocol/server
- call `connected_data_download` {"paths": ["3-SSCZO_Aug10 - DTMarea6_fgrd_w001001.adf", "8-SSCZO_Aug10 - DTMarea6_fgrd_prj.adf", "10-SSCZO_Aug10 - DTMarea6_fgrd_sta.adf", "19-SSCZO_Aug10 - DTMarea6_fgrd_w001001x.adf", "22-SSCZO_Aug10 - DTMarea6_fgrd_dblbnd.adf", "31-SSCZO_Aug10 - DTMarea6_fgrd_hdr.adf"], "source_id": "source_a1eed9da18864521b3da335dc620cf66"}
  - result: {"accepted": true, "activity": "executing", "handle": "task_495f742d47ae4033a10b7db53d674abc", "kind": "Download", "status": "running"}
- call `wait_tasks` {"interrupt_on_mail": true, "return_when": "all", "tasks": "task_495f742d47ae4033a10b7db53d674abc", "timeout_s": null}
  - result: {"outcomes": [{"handle": "task_495f742d47ae4033a10b7db53d674abc", "next_cursor": null, "pending": false, "result": {"created_at": "2026-11-17T20:37:21.200963+00:00", "error": null, "input_requests": null, "last_updated_at": "2026-11-17T20:37:46.524607+00:00", "meta": {"io.modelcontextprotocol/server
- call `shell_bash` {"command": "find /home/user/clio-agent/.cloud-run -name \"*area6_fgrd*\" 2>/dev/null | head"}
  - result: {"handle": "task_d2895a1d49e441a2be2ea8aa4a44f238", "status": "completed", "pending": false, "result": {"meta": {"io.modelcontextprotocol/serverInfo": {"name": "CLIO shell", "version": "4.1.2"}}, "task_id": "3UAwOL6iLP39Z40Ng8YZTJuTMlfsf7bkpL6avgK8unw", "status": "completed", "created_at": "2026-11-
- call `shell_bash` {"command": "S=/home/user/clio-agent/.cloud-run/xdg/data/clio-agent/connected-sources/source_a1eed9da18864521b3da335dc620cf66/snapshot_4240be24c5a64c09b3b792cb68cb3352\nW=/home/user/clio-agent/.cloud-run/ndp-ws/organizations-opentopography-203628\nmkdir -p $W/area6_grid\nfor t in w001001 w001001x prj sta dblbnd hdr; do cp \"$S\"/*\"area6_fgrd_$t.adf\" $W/area6_grid/$t.adf; done\nls -l $W/area6_gri
  - result: {"handle": "task_f5bbb052d541423bab50c5debeb28344", "status": "completed", "pending": false, "result": {"meta": {"io.modelcontextprotocol/serverInfo": {"name": "CLIO shell", "version": "4.1.2"}}, "task_id": "PM5VcfkYP2Z4xyuNZBgjkj_YGWNfKBwb2hYZkHl-MNc", "status": "completed", "created_at": "2026-11-
- call `geoservices_read_raster_window` {"bbox": [-118.745, 36.588, -118.725, 36.606], "dst_crs": "EPSG:32611", "output_path": "/home/user/clio-agent/.cloud-run/ndp-ws/organizations-opentopography-203628/wolverton_dtm.tif", "sources": "/home/user/clio-agent/.cloud-run/ndp-ws/organizations-opentopography-203628/area6_grid"}
  - result: {"content": [], "structuredContent": {"band_description": null, "bounds_native": [343896.034, 4050554.322, 345721.204, 4052583.561], "crs": "EPSG:32611", "decimation": 1.0, "native_resolution": 1.0, "nodata": null, "output_path": "/home/user/clio-agent/.cloud-run/ndp-ws/organizations-opentopography-
- call `terrain_dem_terrain` {"cell_size": 1.0, "filepath": "/home/user/clio-agent/.cloud-run/ndp-ws/organizations-opentopography-203628/wolverton_dtm.tif"}
- call `shell_bash` {"command": "cd /tmp && cat > /tmp/sl.py <<'E'\nimport rasterio, numpy as np\nd=rasterio.open(\"/home/user/clio-agent/.cloud-run/ndp-ws/organizations-opentopography-203628/wolverton_dtm.tif\")\nz=d.read(1).astype(float); z[(z<1000)|(z>4000)]=np.nan\ngy,gx=np.gradient(z,1.0)\ns=np.degrees(np.arctan(np.hypot(gx,gy))); v=s[np.isfinite(s)]\nprint(\"nodata?\",d.nodata, \"n\",v.size)\nprint(\"mean\",v.m
  - ERROR result: DEM has 3702925 cells, above the 2000000 cell safety limit.
  - result: {"handle": "task_559c1497ef604f40bc819f03592f3594", "status": "completed", "pending": false, "result": {"meta": {"io.modelcontextprotocol/serverInfo": {"name": "CLIO shell", "version": "4.1.2"}}, "task_id": "2FF1BH8O9RTmEbpkqX3NBw98nusQWNbbly9anK3IiRw", "status": "completed", "created_at": "2026-11-
**assistant**: I got a 1 m bare-earth elevation model of Wolverton, and the terrain there is moderately steep: the typical slope is about 18°. I computed that myself with a finite-difference slope calculation in Python. The `terrain_dem_terrain` tool refused the grid as over its 2-million-cell limit, so that tool was not used.

**Data used**
- Dataset: Southern Sierra Nevada Critical Zone Observatory: Snow Off (OpenTopography, via NDP). It is airborne lidar flown in 2010, and the Wolverton sub-area (Area 6) is part of it.
- Layer: `DTMarea6_fgrd`, the filtered (ground-only) 1 m bare-earth DTM.
- Why this one: a keyword search on the catalog turned up no other bare-earth DEM that I could identify as covering Wolverton, so I used the SSCZO survey, which names Wolverton as Area 6.
- Output: [wolverton_dtm.tif](workspace-file:/home/user/clio-agent/.cloud-run/ndp-ws/organizations-opentopography-203628/wolverton_dtm.tif), a 2029 × 1825 grid at 1 m in EPSG:32611, covering about 2 km × 1.8 km around Wolverton (−118.745 to −118.725 lon, 36.588 to 36.606 lat). The Wolverton trailhead at about 36.597, −118.735 sits near the centre.
- Elevations run from 1,959 m to 2,469 m, mean 2,197 m, and about 97% of the window has valid data.

**How steep it is**

| Measure | Value |
|---|---|
| Mean slope | 18.1° |
| Median slope | 18.2° |
| 10th to 90th percentile | 4.7° to 30.5° |
| 99th percentile | 39.7° |

| Slope class | Share of area |
|---|---|
| 0–5° | 10.7% |
| 5–15° | 28.0% |
| 15–30° | 50.1% |
| 30–45° | 10.9% |
| over 45° | 0.3% |

About half the area falls in the 15–30° range, with relatively flat ground only around the trailhead and the valley floor, and a few steeper pockets in the creek drainages. Only about 11% is steeper than 30°.

**Caveats**
- The window is my own choice of "around Wolverton"; a larger or smaller window would shift the statistics.
- The slope figures come from a simple gradient on 1 m cells, so they pick up fine local roughness.
- Under dense forest, lidar ground returns are sparser, so the bare-earth surface may be a little smoothed there.
- I did not check the vertical datum. The data are in metres, but I did not confirm whether elevations are above the ellipsoid or a geoid model.
- I did not render a map or visually inspect the DEM, so the elevation range and statistics are the only checks on its content.

