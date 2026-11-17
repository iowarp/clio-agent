# cal-oes (sess_01738ac6a708)

**user**: What hazard or emergency layers does NDP have from California's Office of Emergency Services that cover Sonoma County? Get the actual features for the county from one of them.

- call `connected_data_ndp_organizations` {"endpoint_id": "", "query": "emergency"}
- call `connected_data_search_ndp` {"endpoint_id": "", "offset": 0, "organization": "", "query": "Cal OES"}
- call `geo_geocode` {"query": "Sonoma County, California"}
  - result: {"organizations": []}
  - result: {"original_chars": 226003, "preview": "{\"datasets\": [{\"access\": null, \"description\": \"<font size='4'>Active Incident feature layer used to track open shelters entered into the CalEOC reporting system.</font>\", \"endpoint_id\": \"ndp\", \"endpoint_name\": \"NDP catalog\", \"id\": \"a0960175-d
  - result: [{"display_name":"Sonoma County, California, United States","lat":38.5110803,"lon":-122.8473388,"bbox":[-123.632497,38.0695272,-122.3496966,38.8526549],"type":"administrative","importance":0.603026179283432,"provenance":"osm_nominatim"}]
- call `run_code` {"background": false, "code": "seen={}\noff=0\nwhile off is not None and off<300:\n    r=await tools.ndp.search_datasets(query=\"Cal OES\",offset=off)\n    for d in r[\"datasets\"]:\n        seen.setdefault(d[\"id\"],d[\"title\"])\n    off=r.get(\"next_offset\")\nprint(len(seen))\nfor k,v in seen.items(): print(k[:8],v)\nd=await tools.ndp.get_dataset_details(dataset_id=\"cb729021-d080-435a-9cb9-3e
- call `ndp_search_datasets` {"offset": 0, "query": "Cal OES"}
  - ERROR result: 2 validation errors for call[search_datasets]
query
  Unexpected keyword argument [type=unexpected_keyword_argument, input_value='Cal OES', input_type=str]
    For further information visit https://errors.pydantic.dev/2.12/v/unexpected_keyword_argument
offset
  Unexpected keyword argument [type=unex
  - ERROR result: Error:
Traceback (most recent call last):
  File "<script>", line 4, in <module>
ToolError: 2 validation errors for call[search_datasets]
query
  Unexpected keyword argument [type=unexpected_keyword_argument, input_value='Cal OES', input_type=str]
    For further information visit https://errors.pyd
- call `run_code` {"background": false, "code": "help(tools.ndp.search_datasets); help(tools.ndp.get_dataset_details)", "label": ""}
  - result: Help on function search_datasets in module __main__:

search_datasets(**kwargs: Any) -> asyncio.Future[Any]
    search_datasets(search_terms: any = ..., search_keys: any = ..., dataset_name: any = ..., dataset_title: any = ..., owner_org: any = ..., resource_url: any = ..., resource_name: any = ...,
- call `run_code` {"background": false, "code": "d=await tools.ndp.get_dataset_details(dataset_identifier=\"cb729021-d080-435a-9cb9-3ee8586c5436\")\nprint(str(d)[:3500])", "label": ""}
- call `ndp_get_dataset_details` {"dataset_identifier": "cb729021-d080-435a-9cb9-3ee8586c5436"}
  - result: {"dataset":{"id":"cb729021-d080-435a-9cb9-3ee8586c5436","name":"frap-fire-threat-areas","title":"FRAP - Fire Threat Areas","owner_org":"cal-oes","notes":"Fire Threat is a combination of two factors: 1) fire frequency, or the likelihood of a given area burning, and 2) potential fire behavior (hazard)
  - result: {'dataset': {'id': 'cb729021-d080-435a-9cb9-3ee8586c5436', 'name': 'frap-fire-threat-areas', 'title': 'FRAP - Fire Threat Areas', 'owner_org': 'cal-oes', 'notes': 'Fire Threat is a combination of two factors: 1) fire frequency, or the likelihood of a given area burning, and 2) potential fire behavio
- call `geoservices_describe_geo_service` {"bbox": [-123.632497, 38.0695272, -122.3496966, 38.8526549], "url": "https://egis.fire.ca.gov/arcgis/rest/services/FRAP/FireThreat/MapServer/0"}
  - ERROR result: ArcGIS describe https://egis.fire.ca.gov/arcgis/rest/services/FRAP/FireThreat/MapServer/0 was refused by the service: 404 Service FRAP/FireThreat/MapServer not found
- call `geoservices_fetch_features` {"bbox": [-123.632497, 38.0695272, -122.3496966, 38.8526549], "max_features": 5000, "output_path": "/home/user/clio-agent/.cloud-run/ndp-ws/organizations-cal-oes-201950/sonoma_fire_threat.geojson", "url": "https://egis.fire.ca.gov/arcgis/rest/services/FRAP/FireThreat/MapServer/0"}
  - ERROR result: ArcGIS layer was refused by the service: 404 Service FRAP/FireThreat/MapServer not found
- call `run_code` {"background": false, "code": "ids={\"fire-stations\":\"59204c80-6fa4-46eb-a926-58133da67ae6\",\"eoc\":\"3860a8b8-6a8b-4701-874a-71b87cfb7a43\",\"psap\":\"d05fd32f-6411-4e04-8ad3-d2d0844d91c9\",\"hosp\":\"89501dbf-a1f7-481f-897a-a05b4d5c637d\",\"calfire_ops\":\"e8a2a38d-ad25-430f-b2e0-64e111a68134\",\"shelters\":\"a0960175-d3a4-49b5-8189-d531f3d28d7b\",\"fireperim\":\"72af74ed-4a33-4f55-b72c-a4e8b
- call `ndp_get_dataset_details` {"dataset_identifier": "59204c80-6fa4-46eb-a926-58133da67ae6"}
  - result: {"dataset":{"id":"59204c80-6fa4-46eb-a926-58133da67ae6","name":"fire-stations","title":"Fire Stations","owner_org":"cal-oes","notes":"<font size='4'><a href='https://calema.maps.arcgis.com/home/item.html?id=d33b8b5d03a84170847b48d7d4c1bdf6' target='_blank'>Fire Stations</a><span style='font-family: 
- call `ndp_get_dataset_details` {"dataset_identifier": "3860a8b8-6a8b-4701-874a-71b87cfb7a43"}
  - result: {"dataset":{"id":"3860a8b8-6a8b-4701-874a-71b87cfb7a43","name":"local-emergency-operations-centers-eoc","title":"Local Emergency Operations Centers (EOC)","owner_org":"cal-oes","notes":"<font size='4'><a href='http://calema.maps.arcgis.com/home/item.html?id=40522b0c861a48e5a4dc59e414f3137e#overview'
- call `ndp_get_dataset_details` {"dataset_identifier": "d05fd32f-6411-4e04-8ad3-d2d0844d91c9"}
  - result: {"dataset":{"id":"d05fd32f-6411-4e04-8ad3-d2d0844d91c9","name":"psap-911-service-area-boundaries","title":"PSAP 911 Service Area Boundaries","owner_org":"cal-oes","notes":"<font size='4'><span style='font-family: &quot;Avenir Next W01&quot;, &quot;Avenir Next W00&quot;, &quot;Avenir Next&quot;, Aven
- call `ndp_get_dataset_details` {"dataset_identifier": "89501dbf-a1f7-481f-897a-a05b4d5c637d"}
  - result: {"dataset":{"id":"89501dbf-a1f7-481f-897a-a05b4d5c637d","name":"hospitals","title":"Hospitals","owner_org":"cal-oes","notes":"<font size='4'><span style='font-family:&quot;Avenir Next W01&quot;, &quot;Avenir Next W00&quot;, &quot;Avenir Next&quot;, Avenir, &quot;Helvetica Neue&quot;, sans-serif; bac
- call `ndp_get_dataset_details` {"dataset_identifier": "e8a2a38d-ad25-430f-b2e0-64e111a68134"}
  - result: {"dataset":{"id":"e8a2a38d-ad25-430f-b2e0-64e111a68134","name":"cal-fire-operational-units","title":"CAL FIRE Operational Units","owner_org":"cal-oes","notes":"<div><font size='4'>This <a href='https://calema.maps.arcgis.com/home/item.html?id=9780b1db2a9b46358efb1a9bf2b34d24#overview' rel='nofollow 
- call `ndp_get_dataset_details` {"dataset_identifier": "a0960175-d3a4-49b5-8189-d531f3d28d7b"}
  - result: {"dataset":{"id":"a0960175-d3a4-49b5-8189-d531f3d28d7b","name":"cal-oes-shelters-caleoc","title":"Cal OES -  Shelters (CalEOC)","owner_org":"cal-oes","notes":"<font size='4'>Active Incident feature layer used to track open shelters entered into the CalEOC reporting system.</font>","resources":[{"id"
- call `ndp_get_dataset_details` {"dataset_identifier": "72af74ed-4a33-4f55-b72c-a4e8baa8445c"}
  - result: {"dataset":{"id":"72af74ed-4a33-4f55-b72c-a4e8baa8445c","name":"fire-perimeters-caloes-tracked","title":"Fire Perimeters (CalOES Tracked)","owner_org":"cal-oes","notes":"<a href='http://calema.maps.arcgis.com/home/item.html?id=7b9e70026462400b820cc4b3dd4df0b6' target='_blank'>Feature layer</a> showi
- call `ndp_get_dataset_details` {"dataset_identifier": "d4be0694-87ad-4ebc-b3bb-669035c4502c"}
  - result: {"dataset":{"id":"d4be0694-87ad-4ebc-b3bb-669035c4502c","name":"all-fire-perimeters-caloes-trackedpublic","title":"All Fire Perimeters (CalOES Tracked)(Public)","owner_org":"cal-oes","notes":"<a href='http://calema.maps.arcgis.com/home/item.html?id=7b9e70026462400b820cc4b3dd4df0b6' target='_blank'>F
  - result: fire-stations https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Fire_Station/FeatureServer/0
eoc https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Local_Emergency_Operations_Centers_EOC/FeatureServer/0
psap https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/s
- call `geoservices_fetch_features` {"bbox": [-123.632497, 38.0695272, -122.3496966, 38.8526549], "output_path": "/home/user/clio-agent/.cloud-run/ndp-ws/organizations-cal-oes-201950/sonoma_fire_stations.geojson", "url": "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Fire_Station/FeatureServer/0"}
- call `geoservices_fetch_features` {"bbox": [-123.632497, 38.0695272, -122.3496966, 38.8526549], "output_path": "/home/user/clio-agent/.cloud-run/ndp-ws/organizations-cal-oes-201950/sonoma_eoc.geojson", "url": "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Local_Emergency_Operations_Centers_EOC/FeatureServer/0"}
- call `geoservices_fetch_features` {"bbox": [-123.632497, 38.0695272, -122.3496966, 38.8526549], "output_path": "/home/user/clio-agent/.cloud-run/ndp-ws/organizations-cal-oes-201950/sonoma_fire_perimeters.geojson", "url": "https://services.arcgis.com/BLN4oKB0N1YSgvY8/arcgis/rest/services/Fires_Dataset_(CalOES_Tracked)_(All_Fires)_(Public)/FeatureServer/1"}
  - ERROR result: ArcGIS layer was refused by the service: 400 Invalid URL Invalid URL
  - ERROR result: ArcGIS layer was refused by the service: 400 Invalid URL Invalid URL
  - ERROR result: ArcGIS layer was refused by the service: 400  The requested layer (layerId: 1) was not found.
- call `geoservices_describe_geo_service` {"url": "https://services.arcgis.com/BLN4oKB0N1YSgvY8/arcgis/rest/services/Fires_Dataset_(CalOES_Tracked)_(All_Fires)_(Public)/FeatureServer"}
- call `geoservices_describe_geo_service` {"url": "https://services.arcgis.com/BLN4oKB0N1YSgvY8/arcgis/rest/services/CalEOC_Shelters_Public/FeatureServer/0"}
- call `geoservices_describe_geo_service` {"url": "https://egis.fire.ca.gov/arcgis/rest/services/FRAP/CalFireUnits/FeatureServer/0"}
- call `geoservices_describe_geo_service` {"url": "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Fire_Station/FeatureServer/0"}
  - ERROR result: ArcGIS describe https://services.arcgis.com/BLN4oKB0N1YSgvY8/arcgis/rest/services/CalEOC_Shelters_Public/FeatureServer/0 was refused by the service: 499 Token Required Token Required
  - result: {"kind":"arcgis","service_type":"FeatureServer","url":"https://services.arcgis.com/BLN4oKB0N1YSgvY8/arcgis/rest/services/Fires_Dataset_(CalOES_Tracked)_(All_Fires)_(Public)/FeatureServer","name":"Fires_Dataset_(CalOES_Tracked)_(All_Fires)_(Public)","layers":[{"id":0,"name":"Fire Points (CalOES Track
  - ERROR result: ArcGIS describe https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Fire_Station/FeatureServer/0 was refused by the service: 400 Invalid URL Invalid URL
  - ERROR result: ArcGIS describe https://egis.fire.ca.gov/arcgis/rest/services/FRAP/CalFireUnits/FeatureServer/0 was refused by the service: 499 Token Required
**assistant**: The fire points service has a working layer 0. I'll fetch Sonoma County features from it.

- call `geoservices_fetch_features` {"bbox": [-123.632497, 38.0695272, -122.3496966, 38.8526549], "output_path": "/home/user/clio-agent/.cloud-run/ndp-ws/organizations-cal-oes-201950/sonoma_fire_points.geojson", "url": "https://services.arcgis.com/BLN4oKB0N1YSgvY8/arcgis/rest/services/Fires_Dataset_(CalOES_Tracked)_(All_Fires)_(Public)/FeatureServer/0"}
- call `geoservices_fetch_features` {"count_only": true, "url": "https://services.arcgis.com/BLN4oKB0N1YSgvY8/arcgis/rest/services/Fires_Dataset_(CalOES_Tracked)_(All_Fires)_(Public)/FeatureServer/0"}
  - result: {"content": [], "structuredContent": {"bounds": null, "feature_count": 0, "geometry_types": {}, "output_path": "/home/user/clio-agent/.cloud-run/ndp-ws/organizations-cal-oes-201950/sonoma_fire_points.geojson", "paging": {"complete": true, "format": "GeoJSON", "page_size": 1000, "pages": 1, "server_m
  - result: {"url":"https://services.arcgis.com/BLN4oKB0N1YSgvY8/arcgis/rest/services/Fires_Dataset_(CalOES_Tracked)_(All_Fires)_(Public)/FeatureServer/0","region":null,"where":"1=1","count":0,"summary":"0 features match"}
- call `run_code` {"background": false, "code": "ids={\"fire-stations\":\"59204c80-6fa4-46eb-a926-58133da67ae6\",\"eoc\":\"3860a8b8-6a8b-4701-874a-71b87cfb7a43\",\"calfire_fac\":\"6bf96560-eaa7-47e5-8f08-d924df63f2dd\",\"fire_event_poly\":\"6a96d702-7454-41bb-baff-054c44a1758c\",\"ops\":\"10aaef4f-e658-4c13-95d5-aca7a4fd15fc\"}\nfor n,i in ids.items():\n    for srv in (\"global\",None):\n        try:\n            d
- call `ndp_get_dataset_details` {"dataset_identifier": "59204c80-6fa4-46eb-a926-58133da67ae6"}
  - result: {"dataset":{"id":"59204c80-6fa4-46eb-a926-58133da67ae6","name":"fire-stations","title":"Fire Stations","owner_org":"cal-oes","notes":"<font size='4'><a href='https://calema.maps.arcgis.com/home/item.html?id=d33b8b5d03a84170847b48d7d4c1bdf6' target='_blank'>Fire Stations</a><span style='font-family: 
- call `ndp_get_dataset_details` {"dataset_identifier": "3860a8b8-6a8b-4701-874a-71b87cfb7a43"}
  - result: {"dataset":{"id":"3860a8b8-6a8b-4701-874a-71b87cfb7a43","name":"local-emergency-operations-centers-eoc","title":"Local Emergency Operations Centers (EOC)","owner_org":"cal-oes","notes":"<font size='4'><a href='http://calema.maps.arcgis.com/home/item.html?id=40522b0c861a48e5a4dc59e414f3137e#overview'
- call `ndp_get_dataset_details` {"dataset_identifier": "6bf96560-eaa7-47e5-8f08-d924df63f2dd"}
  - result: {"dataset":{"id":"6bf96560-eaa7-47e5-8f08-d924df63f2dd","name":"cal-fire-facilities-for-wildland-fire-protection","title":"CAL FIRE Facilities for Wildland Fire Protection","owner_org":"cal-oes","notes":"This dataset is intended to provide a statewide depiction of CAL FIRE and contract facilities fo
- call `ndp_get_dataset_details` {"dataset_identifier": "6a96d702-7454-41bb-baff-054c44a1758c"}
  - result: {"dataset":{"id":"6a96d702-7454-41bb-baff-054c44a1758c","name":"fire-event-polygons","title":"Fire (Event Polygons)","owner_org":"cal-oes","notes":"<font size='4'><a href='http://calema.maps.arcgis.com/home/item.html?id=0973feef8b9443628d92ca2cc5e94c4f#overview' target='_blank'>Feature layer</a> sho
- call `ndp_get_dataset_details` {"dataset_identifier": "10aaef4f-e658-4c13-95d5-aca7a4fd15fc"}
  - result: {"dataset":{"id":"10aaef4f-e658-4c13-95d5-aca7a4fd15fc","name":"caloes-operation-centers","title":"CalOES Operation Centers","owner_org":"cal-oes","notes":"<div><font size='4'>This <a href='https://calema.maps.arcgis.com/home/item.html?id=22a56f289d3c440f8270743ac9cdb6e7#overview' target='_blank'>fe
  - result: fire-stations Esri REST https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Fire_Station/FeatureServer/0
fire-stations GeoJSON https://gis-calema.opendata.arcgis.com/datasets/d33b8b5d03a84170847b48d7d4c1bdf6_0.geojson?outSR=%7B%22latestWkid%22%3A3857%2C%22wkid%22%3A102100%7D
fire-stat
- call `geoservices_fetch_features` {"bbox": [-123.632497, 38.0695272, -122.3496966, 38.8526549], "output_path": "/home/user/clio-agent/.cloud-run/ndp-ws/organizations-cal-oes-201950/sonoma_fire_event_polygons.geojson", "url": "https://services.arcgis.com/BLN4oKB0N1YSgvY8/arcgis/rest/services/Fires_Dataset_(CalOES_Tracked)_(All_Fires)_(Public)/FeatureServer/5"}
  - ERROR result: ArcGIS layer was refused by the service: 400  The requested layer (layerId: 5) was not found.
- call `shell_bash` {"command": "mkdir -p /tmp/dl_fs && cd /tmp/dl_fs && curl -sS -L -m 120 -o fs.geojson \"https://gis-calema.opendata.arcgis.com/datasets/d33b8b5d03a84170847b48d7d4c1bdf6_0.geojson?outSR=%7B%22latestWkid%22%3A4326%2C%22wkid%22%3A4326%7D\" -w \"%{http_code} %{size_download}\\n\"; head -c 300 fs.geojson"}
  - result: {"handle": "task_4981640cb699476e9214ee9a76bd11ba", "status": "completed", "pending": false, "result": {"meta": {"io.modelcontextprotocol/serverInfo": {"name": "CLIO shell", "version": "4.1.2"}}, "task_id": "UBd6M8rC0C-VBM4BlhNH4lpt_mZM-EcAaU4CgdS1e-A", "status": "completed", "created_at": "2026-11-
- call `run_code` {"background": false, "code": "import asyncio\nids={\"ops\":\"10aaef4f-e658-4c13-95d5-aca7a4fd15fc\",\"counties\":\"1872fe7c-9fca-4ad5-9d40-0c4867f37231\",\"airports\":\"292a3c46-8984-42c6-82f1-8acc485e80ac\",\"heliports\":\"55935e22-47d7-47f3-9e5f-a118324c08c7\",\"hosp\":\"89501dbf-a1f7-481f-897a-a05b4d5c637d\",\"psap\":\"d05fd32f-6411-4e04-8ad3-d2d0844d91c9\",\"mar\":\"7239435f-569e-437d-84a4-29
- call `ndp_get_dataset_details` {"dataset_identifier": "1872fe7c-9fca-4ad5-9d40-0c4867f37231"}
- call `ndp_get_dataset_details` {"dataset_identifier": "7239435f-569e-437d-84a4-2937f59af0af"}
- call `ndp_get_dataset_details` {"dataset_identifier": "55935e22-47d7-47f3-9e5f-a118324c08c7"}
- call `ndp_get_dataset_details` {"dataset_identifier": "292a3c46-8984-42c6-82f1-8acc485e80ac"}
- call `ndp_get_dataset_details` {"dataset_identifier": "10aaef4f-e658-4c13-95d5-aca7a4fd15fc"}
- call `ndp_get_dataset_details` {"dataset_identifier": "89501dbf-a1f7-481f-897a-a05b4d5c637d"}
- call `ndp_get_dataset_details` {"dataset_identifier": "d05fd32f-6411-4e04-8ad3-d2d0844d91c9"}
- call `ndp_get_dataset_details` {"dataset_identifier": "e8a2a38d-ad25-430f-b2e0-64e111a68134"}
  - result: {"dataset":{"id":"55935e22-47d7-47f3-9e5f-a118324c08c7","name":"caltrans-hospital-heliports","title":"CalTrans Hospital Heliports","owner_org":"cal-oes","notes":"<div><p style='margin-bottom:0in;margin-bottom:.0001pt'><font size='4'><span style='font-family:\r\n&quot;Arial&quot;,sans-serif;color:bla
- call `ndp_get_dataset_details` {"dataset_identifier": "04697349-6a33-4847-87fb-451fa80b6524"}
  - result: {"dataset":{"id":"1872fe7c-9fca-4ad5-9d40-0c4867f37231","name":"counties-in-california","title":"Counties In California","owner_org":"cal-oes","notes":"<a href='http://calema.maps.arcgis.com/home/item.html?id=59d92c1bf84a438d83f78465dce02c61' target='_blank'>Feature layer</a><font face='Avenir Next 
- call `ndp_get_dataset_details` {"dataset_identifier": "a6c6d2b7-902a-4953-ba67-07dd6b7436b3"}
- call `geoservices_fetch_features` {"bbox": [-123.632497, 38.0695272, -122.3496966, 38.8526549], "count_only": true, "url": "https://services.arcgis.com/BLN4oKB0N1YSgvY8/arcgis/rest/services/Counties_in_California/FeatureServer/0"}
- call `geoservices_fetch_features` {"bbox": [-123.632497, 38.0695272, -122.3496966, 38.8526549], "count_only": true, "url": "https://services.arcgis.com/BLN4oKB0N1YSgvY8/arcgis/rest/services/CalTrans_HospitalHeliports/FeatureServer/0"}
  - result: {"dataset":{"id":"10aaef4f-e658-4c13-95d5-aca7a4fd15fc","name":"caloes-operation-centers","title":"CalOES Operation Centers","owner_org":"cal-oes","notes":"<div><font size='4'>This <a href='https://calema.maps.arcgis.com/home/item.html?id=22a56f289d3c440f8270743ac9cdb6e7#overview' target='_blank'>fe
  - result: {"dataset":{"id":"89501dbf-a1f7-481f-897a-a05b4d5c637d","name":"hospitals","title":"Hospitals","owner_org":"cal-oes","notes":"<font size='4'><span style='font-family:&quot;Avenir Next W01&quot;, &quot;Avenir Next W00&quot;, &quot;Avenir Next&quot;, Avenir, &quot;Helvetica Neue&quot;, sans-serif; bac
  - result: {"dataset":{"id":"d05fd32f-6411-4e04-8ad3-d2d0844d91c9","name":"psap-911-service-area-boundaries","title":"PSAP 911 Service Area Boundaries","owner_org":"cal-oes","notes":"<font size='4'><span style='font-family: &quot;Avenir Next W01&quot;, &quot;Avenir Next W00&quot;, &quot;Avenir Next&quot;, Aven
  - result: {"dataset":{"id":"7239435f-569e-437d-84a4-2937f59af0af","name":"caloes-administrative-regions-and-mars","title":"CalOES Administrative Regions and MARs","owner_org":"cal-oes","notes":"<div><div><font size='4'>The California Governor’s Office of Emergency Services’ (Cal OES) <a href='https://calema.m
- call `ndp_get_dataset_details` {"dataset_identifier": "c2081b20-bf45-4144-bd64-263e4c523cbe"}
  - result: {"dataset":{"id":"292a3c46-8984-42c6-82f1-8acc485e80ac","name":"caltrans-public-use-airports","title":"CalTrans Public Use Airports","owner_org":"cal-oes","notes":"<div><div style=''><font size='4'><font color='#000000' face='Times New Roman'>This is a point layer of </font><a href='http://calema.ma
  - result: {"dataset":{"id":"e8a2a38d-ad25-430f-b2e0-64e111a68134","name":"cal-fire-operational-units","title":"CAL FIRE Operational Units","owner_org":"cal-oes","notes":"<div><font size='4'>This <a href='https://calema.maps.arcgis.com/home/item.html?id=9780b1db2a9b46358efb1a9bf2b34d24#overview' rel='nofollow 
- call `geoservices_fetch_features` {"bbox": [-123.632497, 38.0695272, -122.3496966, 38.8526549], "count_only": true, "url": "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/PSAP_911_Service_Area_Boundaries/FeatureServer/0"}
  - result: {"dataset":{"id":"04697349-6a33-4847-87fb-451fa80b6524","name":"cellular-towers","title":"Cellular Towers","owner_org":"cal-oes","notes":"<font size='4'><span style='font-family: &quot;Avenir Next W01&quot;, &quot;Avenir Next W00&quot;, &quot;Avenir Next&quot;, Avenir, &quot;Helvetica Neue&quot;, sa
- call `geoservices_fetch_features` {"bbox": [-123.632497, 38.0695272, -122.3496966, 38.8526549], "count_only": true, "url": "https://services.arcgis.com/BLN4oKB0N1YSgvY8/arcgis/rest/services/CalOES_OperationCenters/FeatureServer/0"}
- call `geoservices_fetch_features` {"bbox": [-123.632497, 38.0695272, -122.3496966, 38.8526549], "count_only": true, "url": "https://services.arcgis.com/BLN4oKB0N1YSgvY8/arcgis/rest/services/Transportation_Air__CalTrans_PublicUseAirports/FeatureServer/0"}
- call `geoservices_fetch_features` {"bbox": [-123.632497, 38.0695272, -122.3496966, 38.8526549], "count_only": true, "url": "https://services.arcgis.com/BLN4oKB0N1YSgvY8/arcgis/rest/services/CalOES_Regions_MARs/FeatureServer"}
- call `geoservices_fetch_features` {"bbox": [-123.632497, 38.0695272, -122.3496966, 38.8526549], "count_only": true, "url": "https://egis.fire.ca.gov/arcgis/rest/services/FRAP/CalFireUnits/FeatureServer/0"}
- call `geoservices_fetch_features` {"bbox": [-123.632497, 38.0695272, -122.3496966, 38.8526549], "count_only": true, "url": "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Hospitals_1/FeatureServer/"}
  - result: {"dataset":{"id":"c2081b20-bf45-4144-bd64-263e4c523cbe","name":"firis-initial-attack-perimeters","title":"FIRIS Initial Attack Perimeters","owner_org":"cal-oes","notes":"<p>The Wildland Fire Interagency Geospatial Services (WFIGS) Group provides authoritative geospatial data products under the inter
  - result: {"dataset":{"id":"a6c6d2b7-902a-4953-ba67-07dd6b7436b3","name":"california-electric-utility-service-territory","title":"California Electric Utility Service Territory","owner_org":"cal-oes","notes":"<font size='4'>This <a href='https://calema.maps.arcgis.com/home/item.html?id=016cc0adcc0442618b9c7cd3
  - ERROR result: ArcGIS count was refused by the service: 400 Invalid URL Invalid URL
  - result: {"url":"https://services.arcgis.com/BLN4oKB0N1YSgvY8/arcgis/rest/services/Counties_in_California/FeatureServer/0","region":[-123.632497,38.0695272,-122.3496966,38.8526549],"where":"1=1","count":7,"summary":"7 features match inside the region"}
- call `geoservices_fetch_features` {"bbox": [-123.632497, 38.0695272, -122.3496966, 38.8526549], "count_only": true, "url": "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Cellular_Towers/FeatureServer/0"}
- call `geoservices_fetch_features` {"bbox": [-123.632497, 38.0695272, -122.3496966, 38.8526549], "count_only": true, "url": "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Retail_Service_Territories/FeatureServer/0"}
  - ERROR result: ArcGIS count was refused by the service: 400 Invalid URL Invalid URL
  - result: {"url":"https://services.arcgis.com/BLN4oKB0N1YSgvY8/arcgis/rest/services/CalOES_OperationCenters/FeatureServer/0","region":[-123.632497,38.0695272,-122.3496966,38.8526549],"where":"1=1","count":0,"summary":"0 features match inside the region"}
  - ERROR result: Give a layer URL (…/FeatureServer/<id>) to count features
  - result: {"url":"https://services.arcgis.com/BLN4oKB0N1YSgvY8/arcgis/rest/services/Transportation_Air__CalTrans_PublicUseAirports/FeatureServer/0","region":[-123.632497,38.0695272,-122.3496966,38.8526549],"where":"1=1","count":9,"summary":"9 features match inside the region"}
  - ERROR result: Give a layer URL (…/FeatureServer/<id>) to count features
  - ERROR result: ArcGIS count was refused by the service: 499 Token Required
  - ERROR result: ArcGIS count was refused by the service: 400 Invalid URL Invalid URL
  - ERROR result: ArcGIS count was refused by the service: 400 Invalid URL Invalid URL
  - result: ops [('https://services.arcgis.com/BLN4oKB0N1YSgvY8/arcgis/rest/services/CalOES_OperationCenters/FeatureServer/0', "497, 38.0695272, -122.3496966, 38.8526549], 'where': '1=1', 'count': 0, 'summary': '0 features match inside the region'}")]
counties [('https://services.arcgis.com/BLN4oKB0N1YSgvY8/arc
- call `run_code` {"background": false, "code": "import json\nbbox=[-123.632497,38.0695272,-122.3496966,38.8526549]\nc=await tools.geoservices.fetch_features(url=\"https://services.arcgis.com/BLN4oKB0N1YSgvY8/arcgis/rest/services/Counties_in_California/FeatureServer/0\",bbox=bbox,output_path=\"counties_near_sonoma.geojson\")\na=await tools.geoservices.fetch_features(url=\"https://services.arcgis.com/BLN4oKB0N1YSgvY
- call `geoservices_fetch_features` {"bbox": [-123.632497, 38.0695272, -122.3496966, 38.8526549], "output_path": "/home/user/clio-agent/.cloud-run/ndp-ws/organizations-cal-oes-201950/counties_near_sonoma.geojson", "url": "https://services.arcgis.com/BLN4oKB0N1YSgvY8/arcgis/rest/services/Counties_in_California/FeatureServer/0"}
  - result: {"content": [], "structuredContent": {"bounds": [-124.022005, 37.815442, -121.499851, 40.002556], "feature_count": 7, "geometry_types": {"MultiPolygon": 2, "Polygon": 5}, "output_path": "/home/user/clio-agent/.cloud-run/ndp-ws/organizations-cal-oes-201950/counties_near_sonoma.geojson", "paging": {"c
- call `geoservices_fetch_features` {"bbox": [-123.632497, 38.0695272, -122.3496966, 38.8526549], "output_path": "/home/user/clio-agent/.cloud-run/ndp-ws/organizations-cal-oes-201950/airports_bbox.geojson", "url": "https://services.arcgis.com/BLN4oKB0N1YSgvY8/arcgis/rest/services/Transportation_Air__CalTrans_PublicUseAirports/FeatureServer/0"}
  - result: {"content": [], "structuredContent": {"bounds": [-123.530639, 38.143611, -122.434083, 38.801611], "feature_count": 9, "geometry_types": {"Point": 9}, "output_path": "/home/user/clio-agent/.cloud-run/ndp-ws/organizations-cal-oes-201950/airports_bbox.geojson", "paging": {"complete": true, "format": "G
  - result: ['OBJECTID', 'CountyName', 'AdminRegion', 'FireMAR', 'LawMAR', 'State_FIPS_ID', 'County_FIPS_ID', 'Shape__Area', 'Shape__Length'] /home/user/clio-agent/.cloud-run/ndp-ws/organizations-cal-oes-201950/counties_near_sonoma.geojson
[{'OBJECTID': 17, 'CountyName': 'Lake', 'AdminRegion': 'Coastal', 'FireM
- call `run_code` {"background": false, "code": "import json\ncj=json.load(open(\"/home/user/clio-agent/.cloud-run/ndp-ws/organizations-cal-oes-201950/counties_near_sonoma.geojson\"))\nson=[f for f in cj[\"features\"] if f[\"properties\"][\"CountyName\"]==\"Sonoma\"]\njson.dump({\"type\":\"FeatureCollection\",\"features\":son},open(\"/home/user/clio-agent/.cloud-run/ndp-ws/organizations-cal-oes-201950/sonoma_county
- call `geoprocess_clip_features` {"mask": "/home/user/clio-agent/.cloud-run/ndp-ws/organizations-cal-oes-201950/sonoma_county_boundary.geojson", "output_path": "/home/user/clio-agent/.cloud-run/ndp-ws/organizations-cal-oes-201950/sonoma_public_use_airports.geojson", "source": "/home/user/clio-agent/.cloud-run/ndp-ws/organizations-cal-oes-201950/airports_bbox.geojson"}
  - result: {"content": [], "structuredContent": {"bounds_wgs84": [-122.992417, 38.223917, -122.434083, 38.775971], "clipped_by": "sonoma_county_boundary.geojson", "columns": ["OBJECTID", "OBJECTID_1", "OBJECTID_2", "FACILITY", "MAPLABEL", "AIRPORTID", "CITY", "COUNTY", "FNCTNLCLSS", "CLASSCODE", "STATECLASS", 
  - result: {'clipped_by': 'sonoma_county_boundary.geojson', 'input_features': 9, 'repaired_geometries': 0, 'output_path': '/home/user/clio-agent/.cloud-run/ndp-ws/organizations-cal-oes-201950/sonoma_public_use_airports.geojson', 'feature_count': 6, 'geometry_types': {'Point': 6}, 'bounds_wgs84': [-122.992417, 
**assistant**: NDP lists many Cal OES layers that touch Sonoma County, but only a few of their data services would actually answer. I pulled real features from one of them: 6 public-use airports inside the county.

## Layers that cover Sonoma
All of these are catalogued under the `cal-oes` organization. They fall into three groups by what I could confirm.

**Verified to return data for Sonoma**
- **Counties in California** (Cal OES regions and mutual-aid regions): 7 counties intersect the Sonoma bounding box, including Sonoma itself.
- **CalTrans Public Use Airports**: 9 points fall in the Sonoma bounding box, and 6 of them are inside the county.

**Cataloged and statewide in scope, but I could not get data from them**
- **Hazard layers**
  - *FRAP – Fire Threat Areas:* the service URL returns 404 ("Service FRAP/FireThreat/MapServer not found").
  - *Fire Points and Fire Perimeters (CalOES Tracked):* the Fire Points layer is up but has 0 records statewide right now. The other fire layers I tried either returned "layer not found" or 0 features for Sonoma.
- **Emergency-response layers**
  - Local Emergency Operations Centers, Fire Stations, PSAP 911 Service Areas, Hospitals, Cellular Towers, Electric Service Territories and Hospital Heliports: these point to hosted services that reject requests with "400 Invalid URL".
  - CAL FIRE Operational Units: returns "499 Token Required", so it needs sign-in.
  - Cal OES Shelters (CalEOC): also needs a token.
  - CalOES Operation Centers: the service works but has no features in the Sonoma area.

I did not test the remaining catalog entries; the search returned 260 results in total, and I only looked at the first 50.

## Features retrieved
These are the 6 CalTrans public-use airports inside Sonoma County:

| Facility | City | Class |
|---|---|---|
| Charles M. Schulz, Sonoma County Airport | Santa Rosa | Commercial/Primary |
| Petaluma Municipal | Petaluma | Regional |
| Sonoma Valley | Schellville/Sonoma | Community |
| Sonoma Skypark | Sonoma | Community |
| Healdsburg Municipal | Healdsburg | Community |
| Cloverdale Municipal | Cloverdale | Community |

I first fetched the 9 airports in the county's bounding box, then clipped them to the Sonoma County polygon from the Cal OES counties layer. The clip removed 3 airports that lie in neighboring counties.

This is an emergency-infrastructure layer (aviation facilities), not a hazard layer. A true hazard layer, such as fire threat or fire perimeters, was not retrievable. The airport data is for planning only, per the CalTrans disclaimer in the catalog record.

## Files
- [sonoma_public_use_airports.geojson](workspace-file:/home/user/clio-agent/.cloud-run/ndp-ws/organizations-cal-oes-201950/sonoma_public_use_airports.geojson): the 6 airports, with all CalTrans attributes.
- [sonoma_county_boundary.geojson](workspace-file:/home/user/clio-agent/.cloud-run/ndp-ws/organizations-cal-oes-201950/sonoma_county_boundary.geojson): the Cal OES Sonoma County polygon used for the clip.
- [airports_bbox.geojson](workspace-file:/home/user/clio-agent/.cloud-run/ndp-ws/organizations-cal-oes-201950/airports_bbox.geojson): the 9 airports in the bounding box, before clipping.
- [counties_near_sonoma.geojson](workspace-file:/home/user/clio-agent/.cloud-run/ndp-ws/organizations-cal-oes-201950/counties_near_sonoma.geojson): the 7 counties that intersect the bounding box.

I did not render a map of the results.

If you want an actual hazard layer for Sonoma, I can try the CAL FIRE Facilities geodatabase download. I can also look for an alternative fire-threat service.

