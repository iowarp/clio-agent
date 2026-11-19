# r7-snow (sess_e436ee637f6c)

**user**: How deep was the snow in the Southern Sierra Critical Zone Observatory in March 2010? Use NDP data and show me a snow-depth map.

- call `connected_data_search_ndp` {"endpoint_id": "", "offset": 0, "organization": "", "query": "Southern Sierra snow depth"}
- call `load_skill` {"file": "", "files": null, "skill_id": "work-with-ndp"}
- call `search_tools` {"limit": 0, "query": "select:connected_data_connect,connected_data_status,connected_data_download"}
- call `connected_data_search_ndp` {"endpoint_id": "", "offset": 0, "organization": "", "query": "Southern Sierra Critical Zone Observatory"}
- call `connected_data_search_ndp` {"endpoint_id": "", "offset": 0, "organization": "", "query": "snow depth lidar Sierra"}
  - result: Loaded 3 tools; call them from your next step with the arguments their schemas give.

## connected_data_connect  (connected-data)
Connect and index an approved source folder. Returns an Indexing task handle immediately (the task-protocol skill covers handles); files are downloaded only by connected_
  - result: {"original_chars": 12535, "preview": "# Skill: work-with-ndp\nSkill directory (use as SKILL_ROOT): /home/user/clio-agent/.cloud-run/xdg/config/clio-agent/plugins/snapshots/clio/bcf7156332033b93/plugins/ndp/skills/work-with-ndp\n# Working with NDP\n\nThe National Data Platform is a federated catalog 
  - result: {"datasets": [], "next_offset": null, "total": 0, "warnings": []}
  - result: {"datasets": [], "next_offset": null, "total": 0, "warnings": []}
  - result: {"original_chars": 121451, "preview": "{\"datasets\": [{\"access\": null, \"description\": \"Southern Sierra CZO Lidar flight was acquired in 2010 and consists of 2 flights - Snow on and snow off. Each flight has 6 sub-areas defined as: Bull (Area 1), Courtwright Road (Area 2), Providence (Area 3), 
**assistant**: I found the Southern Sierra CZO 2010 lidar "Snow On" (March 2010) and "Snow Off" (August 2010) datasets. Snow depth is the difference between the two surfaces. Next I'll connect only the bare-earth DTM resources from each dataset.

- call `run_code` {"background": false, "code": "r = await tools.call(\"connected_data_search_ndp\", query=\"Southern Sierra Critical Zone Observatory\")\nsel = {\"4d4a9021-78de-4b5e-9c5f-8c92a7700b4c\":None,\"4ba6843f-3428-4939-a4b0-2a38570a57e3\":None}\nout=[]\nseen=set()\nfor d in r[\"datasets\"]:\n    if d[\"id\"] in sel and d[\"endpoint_id\"]==\"ndp\" and d[\"id\"] not in seen:\n        seen.add(d[\"id\"])\n  
- call `connected_data_search_ndp` {"endpoint_id": "", "offset": 0, "organization": "", "query": "Southern Sierra Critical Zone Observatory"}
  - result: {"original_chars": 121451, "preview": "{\"datasets\": [{\"access\": null, \"description\": \"Southern Sierra CZO Lidar flight was acquired in 2010 and consists of 2 flights - Snow on and snow off. Each flight has 6 sub-areas defined as: Bull (Area 1), Courtwright Road (Area 2), Providence (Area 3), 
- call `connected_data_connect` {"configuration": {"endpoint_id": "ndp", "resource_ids": ["974f1e3a-5adf-4f0a-b09a-c697be461cd0", "76e75c08-af9a-4c9b-a7d9-36ee46956d34", "0c0e1bf1-bb57-4932-8dd6-2e19b284678d", "2d0e30c9-9084-4680-98d0-170ee581876b", "247ade83-3dbd-423c-b883-1cd938dfe19d", "a5d55d90-75cb-4aff-88d8-82f60779f0b8", "d55dbd38-158c-438e-9978-9a47949600a9", "c4e9986a-f91f-454d-9758-a5f2dcfd612b", "73002d8c-d4f2-46d6-99
  - result: {"accepted": true, "activity": "executing", "handle": "task_18ea49e81071494fa39979f2c233621b", "kind": "Indexing", "status": "running"}
- call `connected_data_connect` {"configuration": {"endpoint_id": "ndp", "resource_ids": ["8b17ccfd-dd71-43d6-9f94-014a0d6e5091", "339e529a-5400-4f4f-bdb0-38b83594b3ca", "5350758d-4749-4d8d-a6a5-73e6dfb6d066", "e6ed52cd-d6c1-4666-bd65-67e4b8deb1f2", "c6a8d920-34aa-4a47-99d9-f1cd3eaa9642", "86e480d0-98e0-4a81-8a16-3c624fc5cba9", "387797d6-630c-4b6c-9bf2-1938df5b80d0", "f262ce43-6f10-4a79-9153-7aa6c7fb6619", "5f5f9c8f-3cfb-4b84-93
  - result: {"accepted": true, "activity": "executing", "handle": "task_f2d77e08c1cf4bd498f355854895ca01", "kind": "Indexing", "status": "running"}
  - result: 4d4a9021-78de-4b5e-9c5f-8c92a7700b4c 37 {'handle': 'task_18ea49e81071494fa39979f2c233621b', 'kind': 'Indexing', 'accepted': True, 'status': 'running', 'activity': 'executing'}
4ba6843f-3428-4939-a4b0-2a38570a57e3 38 {'handle': 'task_f2d77e08c1cf4bd498f355854895ca01', 'kind': 'Indexing', 'accepted': 
**assistant**: The snow-off index is done. I'll wait for the snow-on index, then check the source status.

- call `connected_data_status` {}
- call `wait_tasks` {"interrupt_on_mail": true, "return_when": "all", "tasks": ["task_f2d77e08c1cf4bd498f355854895ca01"], "timeout_s": null}
  - result: {"original_chars": 15804, "preview": "{\"accounts\": [{\"authenticated\": false, \"login_action\": {\"context\": {\"clio_id\": \"clio_579bdc6594694fd7a48adfe281301938\", \"workspace_id\": \"ws_cf8a576b5616\"}, \"name\": \"data_source/login/github\"}, \"provider\": \"github\", \"sign_in_available\": 
  - result: {"outcomes": [{"handle": "task_f2d77e08c1cf4bd498f355854895ca01", "next_cursor": null, "pending": false, "result": {"created_at": "2026-11-17T20:45:40.438177+00:00", "error": null, "input_requests": null, "last_updated_at": "2026-11-17T20:45:40.694680+00:00", "meta": {"io.modelcontextprotocol/server
**assistant**: Both sources are indexed. Now I'll download all the selected DTM files, including the VRT mosaics, to local disk.

- call `run_code` {"background": false, "code": "st = await tools.call(\"connected_data_status\")\nfor s in st[\"sources\"]:\n    paths=[r[\"path\"] for r in s[\"selection\"][\"resources\"]]\n    res = await tools.call(\"connected_data_download\", source_id=s[\"id\"], paths=paths)\n    print(s[\"label\"], len(paths), str(res)[:400])", "label": ""}
- call `connected_data_status` {}
  - result: {"original_chars": 15804, "preview": "{\"accounts\": [{\"authenticated\": false, \"login_action\": {\"context\": {\"clio_id\": \"clio_579bdc6594694fd7a48adfe281301938\", \"workspace_id\": \"ws_cf8a576b5616\"}, \"name\": \"data_source/login/github\"}, \"provider\": \"github\", \"sign_in_available\": 
- call `connected_data_download` {"paths": ["1-SSCZO_Aug10 - DTMarea2_fgrd_w001001.adf", "2-SSCZO_Aug10 - DTMarea5_fgrd_dblbnd.adf", "3-SSCZO_Aug10 - DTMarea6_fgrd_w001001.adf", "4-SSCZO_Aug10 - DTMarea3_fgrd_prj.adf", "5-SSCZO_Aug10 - DTMarea1_fgrd_w001001x.adf", "6-SSCZO_Aug10 - DTMSSCZO_Aug10_be.vrt", "7-SSCZO_Aug10 - DTMarea2_fgrd_prj.adf", "8-SSCZO_Aug10 - DTMarea6_fgrd_prj.adf", "9-SSCZO_Aug10 - DTMarea4_fgrd_hdr.adf", "10-
  - result: {"accepted": true, "activity": "queued", "handle": "task_a6b51d6b46624ea6932ee50b828d7dd4", "kind": "Download", "status": "accepted"}
- call `connected_data_download` {"paths": ["1-SSCZO_Mar10 - DTMarea2_fgrd_w001001.adf", "2-SSCZO_Mar10 - DTMarea5_fgrd_dblbnd.adf", "3-SSCZO_Mar10 - DTMarea6_fgrd_w001001.adf", "4-SSCZO_Mar10 - DTMarea4_fshd_w001001x.adf", "5-SSCZO_Mar10 - DTMarea3_fgrd_prj.adf", "6-SSCZO_Mar10 - DTMarea1_fgrd_w001001x.adf", "7-SSCZO_Mar10 - DTMarea2_fgrd_prj.adf", "8-SSCZO_Mar10 - DTMarea6_fgrd_w001001x.adf", "9-SSCZO_Mar10 - DTMarea6_fgrd_sta.
  - result: {"accepted": true, "activity": "executing", "handle": "task_1188b954a36148509b1a261097c60874", "kind": "Download", "status": "running"}
  - result: Southern Sierra Nevada Critical Zone Observatory: Snow Off 37 {'handle': 'task_a6b51d6b46624ea6932ee50b828d7dd4', 'kind': 'Download', 'accepted': True, 'status': 'accepted', 'activity': 'queued'}
Southern Sierra Nevada Critical Zone Observatory: Snow On 38 {'handle': 'task_1188b954a36148509b1a261097
- call `wait_tasks` {"interrupt_on_mail": true, "return_when": "all", "tasks": ["task_a6b51d6b46624ea6932ee50b828d7dd4", "task_1188b954a36148509b1a261097c60874"], "timeout_s": null}
  - result: {"original_chars": 21665, "preview": "{\"outcomes\": [{\"handle\": \"task_a6b51d6b46624ea6932ee50b828d7dd4\", \"next_cursor\": null, \"pending\": false, \"result\": {\"created_at\": \"2026-11-17T20:45:52.175741+00:00\", \"error\": null, \"input_requests\": null, \"last_updated_at\": \"2026-11-17T20:
**assistant**: Both downloads finished, but the files came down as flat, number-prefixed names, so the ArcGrid folder structure is lost. I'll find where they landed and check what's usable.

- call `run_code` {"background": false, "code": "st = await tools.call(\"connected_data_status\")\nfor s in st[\"sources\"]:\n    print(s[\"label\"], s[\"local_path\"], s[\"revision\"], s[\"open_references\"][\"downloaded\"])\nimport subprocess\nfor s in st[\"sources\"]:\n    p=s[\"local_path\"]\n    if p:\n        print(subprocess.run([\"bash\",\"-c\",f\"ls -la '{p}' | head -20; cd '{p}'; for f in *.vrt; do echo =
- call `connected_data_status` {}
  - result: {"original_chars": 16458, "preview": "{\"accounts\": [{\"authenticated\": false, \"login_action\": {\"context\": {\"clio_id\": \"clio_579bdc6594694fd7a48adfe281301938\", \"workspace_id\": \"ws_cf8a576b5616\"}, \"name\": \"data_source/login/github\"}, \"provider\": \"github\", \"sign_in_available\": 
  - result: Southern Sierra Nevada Critical Zone Observatory: Snow Off /home/user/clio-agent/.cloud-run/xdg/data/clio-agent/connected-sources/source_5bba4a98e35e489aa97cc2361d41da9f/snapshot_6096f0b7c36045bbac04aaa6a3a9bf5c 5fe6ff1963e123a8e734aad41f57a445d2f0657f738742fd52e347457d22a4b7 {'source_id': 'source_5
**assistant**: The snow-on files are named `...area4_fshd...` for area 4 instead of `fgrd`, so that area may be a different product. I'll rebuild the ArcGrid folders in the workspace, check each grid, and see whether GDAL is available.

- call `shell_bash` {"command": "cd /home/user/clio-agent/.cloud-run/ndp-ws/snow_depth-204504 && cat > /tmp/rebuild.py <<'EOF'\nimport os,re,shutil,sys\nsrc={\"Aug10\":\"/home/user/clio-agent/.cloud-run/xdg/data/clio-agent/connected-sources/source_5bba4a98e35e489aa97cc2361d41da9f/snapshot_6096f0b7c36045bbac04aaa6a3a9bf5c\",\n\"Mar10\":\"/home/user/clio-agent/.cloud-run/xdg/data/clio-agent/connected-sources/source_381
  - result: {"handle": "task_fc57f67c05d94f3eb584b306cb594d3f", "status": "completed", "pending": false, "result": {"meta": {"io.modelcontextprotocol/serverInfo": {"name": "CLIO shell", "version": "4.1.2"}}, "task_id": "sKuhB2AwSSWsj7ZNsBgqTKHFiJ3FzL0mt8OZqltffgA", "status": "completed", "created_at": "2026-11-
**assistant**: `gdalinfo` isn't installed, but rasterio is. I'll open each rebuilt grid with it.

- call `shell_bash` {"command": "cd /home/user/clio-agent/.cloud-run/ndp-ws/snow_depth-204504 && cat > /tmp/inspect.py <<'EOF'\nimport rasterio,glob,numpy as np\nfor d in sorted(glob.glob(\"grids/*/area*\")):\n    try:\n        with rasterio.open(d) as r:\n            a=r.read(1,out_shape=(r.height//20,r.width//20))\n            v=a[(a>-1e30)&np.isfinite(a)]\n            print(d,r.shape,r.res,r.crs,r.dtypes[0],r.noda
  - result: {"handle": "task_ea2d70c5a1bc4ccd84488756d0c689b3", "status": "completed", "pending": false, "result": {"meta": {"io.modelcontextprotocol/serverInfo": {"name": "CLIO shell", "version": "4.1.2"}}, "task_id": "H61FejQkt35X-PcAtshOxEa_jq_ndetiWVHHmyoxGFo", "status": "completed", "created_at": "2026-11-
**assistant**: Mar10 area 4 is a uint8 shaded-relief grid at 0.5 m, not a DTM, so I'll leave it out of the snow-depth calculation. For the other five areas I'll subtract the August 2010 snow-off DTM from the March 2010 snow-on DTM on their overlapping windows, sampled every 4 m, then map and summarize the result.

- call `shell_bash` {"command": "cd /home/user/clio-agent/.cloud-run/ndp-ws/snow_depth-204504 && cat > /tmp/depth.py <<'EOF'\nimport rasterio,numpy as np,json\nfrom rasterio.windows import from_bounds\nfrom rasterio.transform import from_origin\nimport matplotlib; matplotlib.use(\"Agg\")\nimport matplotlib.pyplot as plt\nD=4; names={1:\"Bull\",2:\"Courtwright Rd\",3:\"Providence\",5:\"Soaproot Saddle\",6:\"Wolverton\
  - result: {"handle": "task_9c6240cb8e9642e2a2d27d7dc18fb21f", "status": "completed", "pending": false, "result": {"meta": {"io.modelcontextprotocol/serverInfo": {"name": "CLIO shell", "version": "4.1.2"}}, "task_id": "BEPnEhhEkVh3h62edNrZrzphusIRpG_2pCzMC-3-E9o", "status": "completed", "created_at": "2026-11-
**assistant**: The numbers are in. I'll load the image viewer and look at the map before reporting.

- call `search_tools` {"limit": 0, "query": "select:view_image,create_artifact"}
  - result: Loaded 2 tools; call them from your next step with the arguments their schemas give.

## view_image  (workspace)
Attach a workspace image to the next model step for visual inspection.

Use this only for an image that already exists inside the active workspace or
this conversation's scratch folder (w
- call `create_artifact` {"annotation": "Snow depth map, March 2010, Southern Sierra CZO (Mar10 snow-on DTM minus Aug10 snow-off DTM, 4 m)", "artifacts": null, "content": "", "kind": "image", "name": "", "path": "/home/user/clio-agent/.cloud-run/ndp-ws/snow_depth-204504/out/snow_depth_map_Mar2010.png", "pdf_preview": null, "purpose": "deliverable", "used": null}
- call `view_image` {"path": "/home/user/clio-agent/.cloud-run/ndp-ws/snow_depth-204504/out/snow_depth_map_Mar2010.png"}
  - result: {"artifacts": [{"accepted": true, "artifact_id": "artifact_9448f8e05929474db5ebf003afc6e474", "created": true, "custody": "cas", "kind": "image", "mechanism": "model", "name": "snow_depth_map_Mar2010.png", "reason": "", "sha256": "5011783b32ae58af9c3fa2575dac50872bc83bde725c2dfa5282ceb1a2209d7a", "v
  - result: {"attached_to_model": true, "delivered_step": 11, "delivery": "tool_result", "media_type": "image/png", "path": "out/snow_depth_map_Mar2010.png", "sha256": "5011783b32ae58af9c3fa2575dac50872bc83bde725c2dfa5282ceb1a2209d7a", "size_bytes": 740936, "snapshot": "clio-tool-output:sess_e436ee637f6c/viewed
**assistant**: In March 2010 the snow in the Southern Sierra CZO lidar footprint was typically about 1–2.5 m deep. The mean depth was 1.8 m at Bull, 2.2 m at Courtwright Road and 2.4 m at Wolverton. Providence was shallower at about 0.9 m. Soaproot Saddle shows essentially no snow, which is probably real: it is a low-elevation area, about 1,000–1,400 m. I did not check this against a snow-cover or station record.

I made the map by subtracting the August 2010 snow-off bare-earth DTM from the March 2010 snow-on DTM. Both come from the two NDP datasets "Southern Sierra Nevada Critical Zone Observatory: Snow On" and "Snow Off" (publisher opentopography).

The map is the artifact `snow_depth_map_Mar2010.png`, a panel per survey area with a shared 0–3 m scale. I looked at the rendered image and it matches the statistics below.

| Area | Mean (m) | Median (m) | 5th–95th percentile (m) |
|---|---|---|---|
| 1 Bull | 1.81 | 1.82 | 0.84–2.65 |
| 2 Courtwright Rd | 2.22 | 2.25 | 1.49–2.85 |
| 3 Providence | 0.91 | 0.83 | 0.06–2.05 |
| 5 Soaproot Saddle | 0.02 | 0.01 | −0.16–0.17 |
| 6 Wolverton | 2.41 | 2.37 | 0.54–4.51 |

Wolverton is the deepest, with the heaviest snow, well above 3 m (the colour scale tops out there), in its eastern, higher part. Providence is deeper in the north and thin in the south.

Caveats:
- **Area 4 (San Joaquin Range) is missing.** The snow-on data for it is only an 8-bit shaded-relief image, not elevation, so no depth could be computed.
- **Depth is an estimate, not a measured value.** Snow-on and snow-off lidar were processed separately with ground filtering, so under dense canopy the snow-on "ground" can include vegetation. The summary statistics are not corrected for any of this.
- **Resolution and outliers.** The 1 m grids were sampled every 4 m, so the statistics come from every fourth cell. I discarded differences over 50 m as errors; a few remaining extreme cells are probably artifacts.
- **Coverage.** The survey covers only the lidar footprint, not the whole observatory. Areas 3 and 6 look spatially unusual at their edges and I did not investigate them. The Mar10 and Aug10 area-5 grids differ in width (6001 vs 5001 cells), so I only used where they overlap.
- **No ground truth.** I did not compare the result with snow-course or station measurements.

Files are in `/home/user/clio-agent/.cloud-run/ndp-ws/snow_depth-204504/out/`:
- `snow_depth_map_Mar2010.png`
- `snow_depth_area{1,2,3,5,6}_Mar2010_4m.tif` (GeoTIFFs, UTM 11N, metres)

