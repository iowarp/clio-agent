# case03 (sess_fa1ff4fa516f)

**user**: What does the current wildfire picture look like around Los Angeles together with the weather? Bring together NDP's fire and weather data for the region and give me a brief with the caveats.

- call `connected_data_search_ndp` {"endpoint_id": "", "offset": 0, "organization": "", "query": "wildfire"}
- call `connected_data_search_ndp` {"endpoint_id": "", "offset": 0, "organization": "", "query": "fire perimeter"}
- call `connected_data_search_ndp` {"endpoint_id": "", "offset": 0, "organization": "", "query": "weather"}
- call `connected_data_search_ndp` {"endpoint_id": "", "offset": 0, "organization": "", "query": "Los Angeles"}
- call `connected_data_ndp_organizations` {"endpoint_id": "", "query": ""}
- call `geo_geocode` {"query": "Los Angeles, California"}
  - result: {"original_chars": 22971, "preview": "{\"organizations\": [{\"endpoint_id\": \"ndp\", \"endpoint_name\": \"NDP catalog\", \"id\": \"alertcalifornia\"}, {\"endpoint_id\": \"ndp\", \"endpoint_name\": \"NDP catalog\", \"id\": \"app-fpp-c40-mit-c41\"}, {\"endpoint_id\": \"ndp\", \"endpoint_name\": \"NDP
  - result: {"original_chars": 332379, "preview": "{\"datasets\": [{\"access\": null, \"coverage\": {\"location\": null, \"time\": null}, \"description\": \"Hourly weather data, including a \\\"health index,\\\" collected from the weather stations in Canada.\", \"endpoint_id\": \"ndp\", \"endpoint_name\": \"NDP
  - result: {"content": [{"text": "[clio: tool_result_stored_in_file] The result was 530,582 bytes. Full result: /home/user/clio-agent/.cloud-run/xdg/state/clio-agent/workspaces/14910824ec695e82eb4a1318/tool-output/sess_fa1ff4fa516f/result-9adf43a31fdfff73.json", "type": "text"}], "isError": false, "stored_resu
  - result: {"content": [{"text": "[clio: tool_result_stored_in_file] The result was 860,076 bytes. Full result: /home/user/clio-agent/.cloud-run/xdg/state/clio-agent/workspaces/14910824ec695e82eb4a1318/tool-output/sess_fa1ff4fa516f/result-97644d2007d3ccd3.json", "type": "text"}], "isError": false, "stored_resu
  - result: {"content": [{"text": "[clio: tool_result_stored_in_file] The result was 551,219 bytes. Full result: /home/user/clio-agent/.cloud-run/xdg/state/clio-agent/workspaces/14910824ec695e82eb4a1318/tool-output/sess_fa1ff4fa516f/result-51b23c1128a95f31.json", "type": "text"}], "isError": false, "stored_resu
  - result: [{"display_name":"Los Angeles, Los Angeles County, California, United States","lat":34.0536923,"lon":-118.2427676,"bbox":[-118.6681798,33.659541,-118.1552983,34.337306],"type":"administrative","importance":0.8410828881088647,"provenance":"osm_nominatim"}]
- call `run_code` {"background": false, "code": "for ds in [\"71ea8edd-3c3d-460e-aca2-7672971651e9\",\"7567e42e-2890-4414-942b-d397c1862125\",\"bdce39c4-21c8-4124-bbd4-dd582854af36\",\"c06d2b57-35c4-472d-a19b-b53197683e0c\",\"29b431c7-a670-442f-987e-d7b280e9d7e2\"]:\n    try:\n        r = await tools.ndp.get_dataset_details(dataset_id=ds)\n        s=str(r)\n        import re\n        print(ds, [u for u in re.findal
- call `ndp_get_dataset_details` {"dataset_id": "71ea8edd-3c3d-460e-aca2-7672971651e9"}
  - ERROR result: 2 validation errors for call[get_dataset_details]
dataset_identifier
  Missing required argument [type=missing_argument, input_value={'dataset_id': '71ea8edd-...460e-aca2-7672971651e9'}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.12/v/missing_argument
dataset_id
- call `ndp_get_dataset_details` {"dataset_id": "7567e42e-2890-4414-942b-d397c1862125"}
  - ERROR result: 2 validation errors for call[get_dataset_details]
dataset_identifier
  Missing required argument [type=missing_argument, input_value={'dataset_id': '7567e42e-...4414-942b-d397c1862125'}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.12/v/missing_argument
dataset_id
- call `ndp_get_dataset_details` {"dataset_id": "bdce39c4-21c8-4124-bbd4-dd582854af36"}
  - ERROR result: 2 validation errors for call[get_dataset_details]
dataset_identifier
  Missing required argument [type=missing_argument, input_value={'dataset_id': 'bdce39c4-...4124-bbd4-dd582854af36'}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.12/v/missing_argument
dataset_id
- call `ndp_get_dataset_details` {"dataset_id": "c06d2b57-35c4-472d-a19b-b53197683e0c"}
  - ERROR result: 2 validation errors for call[get_dataset_details]
dataset_identifier
  Missing required argument [type=missing_argument, input_value={'dataset_id': 'c06d2b57-...472d-a19b-b53197683e0c'}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.12/v/missing_argument
dataset_id
- call `ndp_get_dataset_details` {"dataset_id": "29b431c7-a670-442f-987e-d7b280e9d7e2"}
  - ERROR result: 2 validation errors for call[get_dataset_details]
dataset_identifier
  Missing required argument [type=missing_argument, input_value={'dataset_id': '29b431c7-...442f-987e-d7b280e9d7e2'}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.12/v/missing_argument
dataset_id
  - result: 71ea8edd-3c3d-460e-aca2-7672971651e9 ERR 2 validation errors for call[get_dataset_details]
dataset_identifier
  Missing required argument [type=missing_argument, input_value={'dataset_id': '71ea8edd-...460e-aca2-7672971651e9'}, input_type=dict]
    For further information visit https://errors.pydant
- call `run_code` {"background": false, "code": "import re\nfor ds in [\"71ea8edd-3c3d-460e-aca2-7672971651e9\",\"7567e42e-2890-4414-942b-d397c1862125\",\"bdce39c4-21c8-4124-bbd4-dd582854af36\",\"c06d2b57-35c4-472d-a19b-b53197683e0c\",\"29b431c7-a670-442f-987e-d7b280e9d7e2\"]:\n    try:\n        r = await tools.ndp.get_dataset_details(dataset_identifier=ds)\n        s=str(r)\n        print(ds, len(s), sorted(set(re
- call `ndp_get_dataset_details` {"dataset_identifier": "71ea8edd-3c3d-460e-aca2-7672971651e9"}
  - result: {"original_chars": 58799, "preview": "{\"dataset\":{\"id\":\"71ea8edd-3c3d-460e-aca2-7672971651e9\",\"name\":\"wfigs-current-interagency-fire-perimeters\",\"title\":\"WFIGS Current Interagency Fire Perimeters\",\"owner_org\":\"nifc\",\"notes\":\"<div><p><img alt='WFIGS_Logo_withText' src='https://ni
- call `ndp_get_dataset_details` {"dataset_identifier": "7567e42e-2890-4414-942b-d397c1862125"}
  - result: {"original_chars": 22870, "preview": "{\"dataset\":{\"id\":\"7567e42e-2890-4414-942b-d397c1862125\",\"name\":\"current-wildland-fire-incident-locations\",\"title\":\"Current Wildland Fire Incident Locations\",\"owner_org\":\"nifc\",\"notes\":\"<div><img alt='WFIGS_Logo_withText' src='https://nifc.ma
- call `ndp_get_dataset_details` {"dataset_identifier": "bdce39c4-21c8-4124-bbd4-dd582854af36"}
  - result: {"original_chars": 21923, "preview": "{\"dataset\":{\"id\":\"bdce39c4-21c8-4124-bbd4-dd582854af36\",\"name\":\"hazardous-wildfire-conditions-zone-polygons\",\"title\":\"Hazardous Wildfire Conditions (Zone Polygons)\",\"owner_org\":\"cal-oes\",\"notes\":\"<div style=' margin-left: 20px; margin-right:
- call `ndp_get_dataset_details` {"dataset_identifier": "c06d2b57-35c4-472d-a19b-b53197683e0c"}
  - result: {"dataset":{"id":"c06d2b57-35c4-472d-a19b-b53197683e0c","name":"california-nws-watches-and-warnings","title":"California - NWS Watches and Warnings","owner_org":"cal-oes","notes":"<div><div style='font-family:&quot;Avenir Next W01&quot;, &quot;Avenir Next W00&quot;, &quot;Avenir Next&quot;, Avenir, 
- call `ndp_get_dataset_details` {"dataset_identifier": "29b431c7-a670-442f-987e-d7b280e9d7e2"}
  - result: {"dataset":{"id":"29b431c7-a670-442f-987e-d7b280e9d7e2","name":"national-weather-service-nws-api","title":"National Weather Service (NWS) API","owner_org":"national-weather-station-nws","notes":"<p><span style=\"color: rgb(0, 0, 0);\">The National Weather Service (NWS) API allows developers access t
  - result: 71ea8edd-3c3d-460e-aca2-7672971651e9 59718 ['https://data-nifc.opendata.arcgis.com/datasets/nifc::wfigs-current-interagency-fire-perimeters.csv?outSR=%7B%22latestWkid%22%3A4326%2C%22wkid%22%3A4326%7D', 'https://data-nifc.opendata.arcgis.com/datasets/nifc::wfigs-current-interagency-fire-perimeters.ge
- call `run_code` {"background": false, "code": "import asyncio\nbbox=[-119.0,33.5,-117.4,34.9]\nasync def f(name,url,**kw):\n    try:\n        r=await tools.geoservices.fetch_features(url=url,bbox=bbox,output_path=name,**kw)\n        print(name,str(r)[:1500])\n    except Exception as e:\n        print(name,\"ERR\",str(e)[:400])\nasync def w(name,url):\n    try:\n        r=await tools.web.fetch(url=url)\n        pr
- call `geoservices_fetch_features` {"bbox": [-119.0, 33.5, -117.4, 34.9], "output_path": "/home/user/clio-agent/.cloud-run/ndp-ws/benchmark-case03-205638/perims.geojson", "url": "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/WFIGS_Interagency_Perimeters_Current/FeatureServer/0"}
  - result: {"content": [], "structuredContent": {"bounds": null, "feature_count": 0, "geometry_types": {}, "output_path": "/home/user/clio-agent/.cloud-run/ndp-ws/benchmark-case03-205638/perims.geojson", "outputs": [{"media_type": "application/geo+json", "path": "/home/user/clio-agent/.cloud-run/ndp-ws/benchma
- call `web_fetch` {"url": "https://api.weather.gov/alerts/active?point=34.0537,-118.2428"}
  - ERROR result: fetch_failed: The page could not be read.
- call `geoservices_fetch_features` {"bbox": [-119.0, 33.5, -117.4, 34.9], "output_path": "/home/user/clio-agent/.cloud-run/ndp-ws/benchmark-case03-205638/nws_alerts.geojson", "url": "https://services9.arcgis.com/RHVPKKiFTONKtxq3/arcgis/rest/services/NWS_Watches_Warnings_v1/FeatureServer/6"}
  - result: {"content": [], "structuredContent": {"bounds": [-120.437229, 32.535656, -114.726196, 35.907013], "feature_count": 11, "geometry_types": {"MultiPolygon": 7, "Polygon": 4}, "output_path": "/home/user/clio-agent/.cloud-run/ndp-ws/benchmark-case03-205638/nws_alerts.geojson", "outputs": [{"media_type": 
- call `geoservices_fetch_features` {"bbox": [-119.0, 33.5, -117.4, 34.9], "output_path": "/home/user/clio-agent/.cloud-run/ndp-ws/benchmark-case03-205638/incidents.geojson", "url": "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/WFIGS_Incident_Locations_Current/FeatureServer/0"}
  - result: {"content": [], "structuredContent": {"bounds": [-118.71229, 33.902195, -117.479012, 34.718555], "feature_count": 18, "geometry_types": {"Point": 18}, "output_path": "/home/user/clio-agent/.cloud-run/ndp-ws/benchmark-case03-205638/incidents.geojson", "outputs": [{"media_type": "application/geo+json"
- call `web_fetch` {"url": "https://api.weather.gov/points/34.0537,-118.2428"}
  - ERROR result: fetch_failed: The page could not be read.
  - result: perims.geojson {'url': 'https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/WFIGS_Interagency_Perimeters_Current/FeatureServer/0', 'region': [-119.0, 33.5, -117.4, 34.9], 'where': '1=1', 'output_path': '/home/user/clio-agent/.cloud-run/ndp-ws/benchmark-case03-205638/perims.geojson', '
- call `run_code` {"background": false, "code": "import json,time\nd='/home/user/clio-agent/.cloud-run/ndp-ws/benchmark-case03-205638/'\ninc=json.load(open(d+'incidents.geojson'))\nfor f in inc['features']:\n    p=f['properties'];g=f['geometry']['coordinates']\n    print(p.get('IncidentName'),p.get('IncidentTypeCategory'),p.get('IncidentSize'),p.get('PercentContained'),p.get('FireDiscoveryDateTime'),p.get('POOState
- call `run_code` {"background": false, "code": "import subprocess\nfor u in [\"https://api.weather.gov/alerts/active?point=34.0537,-118.2428\",\"https://api.weather.gov/points/34.0537,-118.2428\"]:\n    r=subprocess.run([\"curl\",\"-sS\",\"-m\",\"30\",\"-H\",\"User-Agent: ndp-brief\",\"-H\",\"Accept: application/geo+json\",u],capture_output=True,text=True)\n    print(u,r.returncode,r.stdout[:1500],r.stderr[:300])"
  - result: https://api.weather.gov/alerts/active?point=34.0537,-118.2428 0 {
    "@context": [
        "https://geojson.org/geojson-ld/geojson-context.jsonld",
        {
            "@version": "1.1",
            "wx": "https://api.weather.gov/ontology#",
            "@vocab": "https://api.weather.gov/ontology
  - result: 2026 LYTLE CREEK PILE PLAN RX RX None None 1767668402000 US-CA San Bernardino [-117.48, 34.23]
2026 LYTLE CREEK BROADCAST BURN RX RX 0.1 None 1767668280000 US-CA San Bernardino [-117.48, 34.23]
FY26 FUELS FUNDING RX RX None None 1780929240000 US-CA Los Angeles [-118.03, 34.15]
LAC-412352 WF None Non
- call `run_code` {"background": false, "code": "import subprocess,json\ndef g(u):\n    r=subprocess.run([\"curl\",\"-sS\",\"-m\",\"30\",\"-H\",\"User-Agent: ndp-brief\",\"-H\",\"Accept: application/geo+json\",u],capture_output=True,text=True)\n    return json.loads(r.stdout)\np=g(\"https://api.weather.gov/points/34.0537,-118.2428\")['properties']\nprint(p['forecastOffice'],p['gridId'],p['gridX'],p['gridY'])\nfc=g(
  - result: https://api.weather.gov/offices/LOX LOX 155 45
2026-11-17T20:16:50+00:00
This Afternoon 66 10 mph WNW Sunny | Sunny, with a high near 66. West northwest wind around 10 mph.
Tonight 50 5 to 10 mph NNW Partly Cloudy | Partly cloudy, with a low around 50. North northwest wind 5 to 10 mph.
Wednesday 68 
**assistant**: # Los Angeles wildfire and weather brief, 17 Nov 2026

**Bottom line:** There is no significant active wildfire around Los Angeles right now, and the weather points to a flood risk rather than a fire risk. A storm is due Wednesday night to Thursday, with a Flood Watch in force.

I queried a box of roughly -119.0 to -117.4 longitude and 33.5 to 34.9 latitude, covering LA County, southern Ventura County and the San Gabriel Mountains. The NIFC incident points and NWS alerts were pulled through NDP-catalogued services. The point forecast and surface observations came from the NWS API, which NDP also lists as a dataset.

## Fire
- **Perimeters:** The NIFC current interagency perimeters layer returned **0 features** in the box. No mapped active fire perimeter exists there. This layer only holds perimeters for current-year fires that have not yet dropped off.
- **Incident locations:** The NIFC current incident layer returned 18 points, but none is a large fire.
  - 12 are new LA County wildfire reports (names like LAC-412352) with no recorded size or containment.
  - 3 more (CHANTRY, HIGHWAY, LAGO) are listed at 0.1 acres.
  - The CLARK and LOMA incidents also have no size recorded.
  - 3 are San Bernardino and LA County prescribed or fuels-project entries (Lytle Creek burn, FY26 Fuels Funding).
- **Reading:** This looks like routine small initial-attack activity and not a major event. Several of these records are probably stale, since a few small fires carry discovery times from earlier this autumn.

## Weather
- **Now:** The LAX and Hawthorne observations at about 20:10 UTC show about 19 °C, relative humidity around 30%, and winds near 9 m/s (the API reports m/s; the Hawthorne station also reports a gust near 15 m/s).
- **Alerts:** There is **no Red Flag Warning anywhere in California** and none for LA. The active alerts are:
  - Flood Watch for LA and Ventura counties through 19 Nov, including the Santa Monica Mountains and Santa Susana Mountains.
  - High Surf Advisory for LA and Ventura beaches.
  - Small Craft Advisory for the Santa Barbara Channel.
  - Wind Advisories for the San Bernardino mountains and the Mojave, outside the LA basin.
  - Freeze Warning for the Mojave.
- **Forecast (NWS Oxnard office, issued 17 Nov, 20:16 UTC):**
  - Sunny, high 66 °F today, with WNW wind around 10 mph.
  - Rain Wednesday night, becoming heavy Thursday, with 1–2 inches possible and a chance of thunderstorms.
- **Meaning:** Rain on fresh burn scars raises debris-flow risk. The Palisades and Eaton fire areas are the obvious concern, though I did not analyze scar locations.

## Caveats
- **Not verified live:** I did not query the VIIRS hotspot layer, so the NIFC layers may miss a very new fire. Nor did I query the CAL FIRE incident feed or the Santa Ana (SAWTI) index. I did not cross-check CAL FIRE or CalOES records, which could include fires that NIFC has not yet ingested.
- **Incident layer quality:** The new LA County reports have no size or containment, and I did not check whether each is still active or already out.
- **Observations:** I used only two or three stations, so mountain and canyon conditions may differ. I did not use NDP's RAWS station layer or the Synoptic data.
- **Alert layer vintage:** The alert polygons come from the CalOES NWS layer, updated within the past hour. Some alerts are outside LA County, and I did not separate them precisely.
- **Method:** I fetched the services directly via the URLs in the NDP records and did not attach them through NDP's connected-data flow. The NWS data comes from the live API rather than a versioned NDP snapshot.
- **Time-sensitive:** Conditions will change quickly with Thursday's storm.

## Files
Raw pulls are in the workspace: `incidents.geojson`, `perims.geojson` (empty) and `nws_alerts.geojson`, in `/home/user/clio-agent/.cloud-run/ndp-ws/benchmark-case03-205638/`. I did not render a map.

