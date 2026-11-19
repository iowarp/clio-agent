# wfsi (sess_6ecf46cb8325)

**user**: Find a fire science dataset on NDP from the Wildland Fire Science Initiative about fuel moisture and show me some of its actual measurements.

- call `connected_data_search_ndp` {"endpoint_id": "", "offset": 0, "organization": "", "query": "fuel moisture"}
- call `connected_data_ndp_organizations` {"endpoint_id": "", "query": "wildland fire"}
  - result: {"organizations": []}
  - result: {"original_chars": 255601, "preview": "{\"datasets\": [{\"access\": null, \"description\": \"This dataset contains fuel moisture data collected prior to 2022 prescribed burns on US army base Fort Stewart in longleaf pine-wiregrass ecosystems. Fuel moisture samples were collected from four burn units
**assistant**: The catalog has several WFSI fuel moisture datasets. I'll use "Fuel Moisture Content Data from the 2024 DoD Wildland Fire Science Initiative Fort Stewart Campaign". To read its measurements I need the connected-data tools, so I'm loading them now.

- call `search_tools` {"limit": 0, "query": "select:connected_data_connect,connected_data_download,connected_data_status,connected_data_open"}
  - result: Loaded 4 tools; call them from your next step with the arguments their schemas give.

## connected_data_connect  (connected-data)
Connect and index an approved source folder. Returns an Indexing task handle immediately (the task-protocol skill covers handles); files are downloaded only by connected_
- call `connected_data_connect` {"configuration": {"endpoint_id": "ndp", "resource_ids": ["7acbe345-6301-456d-9a2b-6721cdd7ca6e", "41693413-43c6-4594-a5ac-0b09052e3485"]}, "label": "Fort Stewart 2024 Fuel Moisture (WFSI)", "mode": "read_only", "provider": "ndp", "root": "44c61799-e348-44b9-b108-78f00e432262"}
  - result: {"accepted": true, "activity": "executing", "handle": "task_c5515397f3c84cfb807780ca4427546c", "kind": "Indexing", "status": "running"}
- call `wait_tasks` {"interrupt_on_mail": true, "return_when": "all", "tasks": "task_c5515397f3c84cfb807780ca4427546c", "timeout_s": null}
  - result: {"outcomes": [{"handle": "task_c5515397f3c84cfb807780ca4427546c", "next_cursor": null, "pending": false, "result": {"created_at": "2026-11-17T20:23:08.726627+00:00", "error": null, "input_requests": null, "last_updated_at": "2026-11-17T20:23:09.089967+00:00", "meta": {"io.modelcontextprotocol/server
- call `connected_data_open` {"folder": "", "linked": true, "offset": 0, "path": "", "range": false, "revision": "link_5b2ca4df751b4f369d87f39327e4863c", "source_id": "source_d1af28f9a9284684bc088d8bdafc0638"}
  - result: {"access": "read_only", "entries": [{"kind": "file", "path": "3-Fuel_Moisture_2024_Ft_Stewart.csv", "revision": "d3fa4a6552128e1828be2694f7ef533795ad890371c93abecc0fd63f5d5f7763", "sha256": null, "size": 0}, {"kind": "file", "path": "5-Fuel_Moisture_2024_Ft_Stewart_dd.csv", "revision": "754c57721907
- call `connected_data_open` {"folder": "", "linked": true, "offset": 0, "path": "3-Fuel_Moisture_2024_Ft_Stewart.csv", "range": false, "revision": "link_5b2ca4df751b4f369d87f39327e4863c", "source_id": "source_d1af28f9a9284684bc088d8bdafc0638"}
- call `connected_data_open` {"folder": "", "linked": true, "offset": 0, "path": "5-Fuel_Moisture_2024_Ft_Stewart_dd.csv", "range": false, "revision": "link_5b2ca4df751b4f369d87f39327e4863c", "source_id": "source_d1af28f9a9284684bc088d8bdafc0638"}
  - result: {"name": "5-Fuel_Moisture_2024_Ft_Stewart_dd.csv", "path": "5-Fuel_Moisture_2024_Ft_Stewart_dd.csv", "resource_id": "res_0e80d5fe51644d04becbadf487ae5c3a", "revision": "link_5b2ca4df751b4f369d87f39327e4863c", "sha256": "2a34ed6defd8cc712e696d956313f6e94dde282110305a2a28659971c3467ad5", "source_id": 
  - result: {"name": "3-Fuel_Moisture_2024_Ft_Stewart.csv", "path": "3-Fuel_Moisture_2024_Ft_Stewart.csv", "resource_id": "res_2419e78600ae40c592b8a1d170705465", "revision": "link_5b2ca4df751b4f369d87f39327e4863c", "sha256": "ed1bb67d0612e9bdde8c13d24b441ebd8d0e9d303a42960c99085dfc4c6a89a8", "source_id": "sourc
- call `workspace_resource_read` {"derivative_id": "", "limit": 0, "offset": 0, "resource_id": "res_2419e78600ae40c592b8a1d170705465"}
- call `workspace_resource_read` {"derivative_id": "", "limit": 0, "offset": 0, "resource_id": "res_0e80d5fe51644d04becbadf487ae5c3a"}
  - result: {"content": "column_or_row_name,unit,definition,column_or_row_long_name,data_type\r\nMonth,,\"the month (February) when fuel types were collected, during the prescribed burn\nValues: 2\",,text\r\nDay,,\"the day during February when fuel types were collected during the prescribed burn\n\tValues: 8 or
  - result: {"content": "Month,Day,Year,Time,Time_Block,Replicate,Landcover_Type,Fuel_Type,Fuel_Moisture_Content,\r\n2,8,2024,1043,1,1,Flatwoods,100hr,26.80,\r\n2,8,2024,1046,1,2,Flatwoods,100hr,6.78,\r\n2,8,2024,1047,1,3,Flatwoods,100hr,5.78,\r\n2,8,2024,1407,2,3,Flatwoods,100hr,20.47,\r\n2,8,2024,1408,2,2,Fla
**assistant**: I found a WFSI fuel moisture dataset on NDP and read its measurements. I did not save any local copy or run any analysis, so everything below comes straight from the two files I opened.

## Dataset
**Fuel Moisture Content Data from the 2024 Department of Defense Wildland Fire Science Initiative Fort Stewart Campaign** (organization `wfsi`, NDP dataset `44c61799-e348-44b9-b108-78f00e432262`). It holds fuel moisture samples collected on February 8 and 10, 2024, in burn units at Fort Stewart, Georgia. I linked two of its files as a read-only source: the data table `3-Fuel_Moisture_2024_Ft_Stewart.csv` and its data dictionary `5-Fuel_Moisture_2024_Ft_Stewart_dd.csv`. By my count the table has 150 rows.

## Sample measurements
Moisture content is in percent, calculated as 100 × (wet weight − dry weight) / dry weight. That is why values can exceed 100%.

| Date | Time | Landcover | Fuel type | Replicate | Moisture (%) |
|---|---|---|---|---|---|
| 2024-02-08 | 10:38 | Flatwoods | 1hr (small dead twigs) | 3 | 14.05 |
| 2024-02-08 | 10:41 | Flatwoods | 10hr (dead woody) | 1 | 17.64 |
| 2024-02-08 | 10:43 | Flatwoods | 100hr (dead woody) | 1 | 26.80 |
| 2024-02-08 | 10:36 | Flatwoods | BG (wiregrass/bluestem) | 3 | 40.53 |
| 2024-02-08 | 10:32 | Flatwoods | DPN (dead pine needles) | 1 | 17.73 |
| 2024-02-08 | 10:33 | Flatwoods | LPN (live pine needles) | 1 | 197.67 |
| 2024-02-08 | 11:19 | Wetlands | WL (live leaves) | 1 | 92.10 |
| 2024-02-08 | 11:21 | Wetlands | WD (leaf litter) | 1 | 30.47 |
| 2024-02-10 | 10:50 | Flatwoods | 100hr (dead woody) | 1 | 49.14 |
| 2024-02-10 | 13:41 | Flatwoods | 1hr (small dead twigs) | 3 | 14.01 |
| 2024-02-10 | 10:39 | Flatwoods | LPN (live pine needles) | 1 | 165.82 |
| 2024-02-10 | 13:42 | Wetlands | WL (live leaves) | 1 | 101.37 |

## What the raw values show
- **Live foliage is much wetter than dead fuel.** Live pine needles (LPN) are 155–212%. Live leaves (WL) are mostly about 90–160%, with a few wetland samples near 54–61%. Dead fine fuels such as 1hr twigs and dead pine needles are mostly about 9–33%.
- **Moisture rose between the two days.** Flatwoods 1hr fuels were about 9–14% on Feb 8 and about 14–19% on Feb 10. Dead pine needles went from about 8–22% to about 21–33%.
- **One apparent outlier.** A Flatwoods 100hr sample on Feb 8 at 14:08, replicate 2, reads 102.60%. Its sibling replicates read 20.47% and 27.40%.
- **Multiple sampling times per day.** Sampling ran in one to three time blocks per day. Each fuel type has three replicates per block, and replicate numbers do not always follow time order.

## Caveats
- The data dictionary labels `Fuel_Moisture_Content` as "integer", but the file contains decimal values.
- Only two of the dataset's resources are linked. The rest, such as the file-level metadata, the XML record and the CHANGES file, are not, so I have not checked the full methodology.

I can plot moisture by fuel type and day, or compare this campaign with the 2022 Fort Stewart or 2025 Fort Stewart datasets.

