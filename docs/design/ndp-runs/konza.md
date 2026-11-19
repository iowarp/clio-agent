# konza (sess_a0fcdee472d8)

**user**: Can the NDP observations from the 2022 Konza Prairie burn tell us whether the smoke or CO2 signal changed with the wind? Investigate what the measurements actually support and show me an evidence-based result.

- call `connected_data_search_ndp` {"endpoint_id": "", "offset": 0, "organization": "", "query": "Konza Prairie burn"}
- call `connected_data_search_ndp` {"endpoint_id": "", "offset": 0, "organization": "", "query": "Konza smoke CO2"}
- call `load_skill` {"file": "", "files": null, "skill_id": "work-with-ndp"}
  - result: {"original_chars": 12535, "preview": "# Skill: work-with-ndp\nSkill directory (use as SKILL_ROOT): /home/user/clio-agent/.cloud-run/xdg/config/clio-agent/plugins/snapshots/clio/ffc94ee7ab9171b9/plugins/ndp/skills/work-with-ndp\n# Working with NDP\n\nThe National Data Platform is a federated catalog 
  - result: {"original_chars": 21820, "preview": "{\"datasets\": [{\"access\": null, \"coverage\": {\"location\": null, \"time\": null}, \"description\": \"This dataset is part of an integration of National Data Platform and Sage. The original analysis can be found at: https://github.com/waggle-sensor/KonzaBurn
  - result: {"original_chars": 16981, "preview": "{\"datasets\": [{\"access\": null, \"coverage\": {\"location\": {\"bbox\": [-96.615052, 39.099457, -96.607609, 39.108295], \"field\": \"spatial\", \"span_degrees\": [0.007, 0.009]}, \"time\": null}, \"description\": \"The Konza Prairie Biological Station, locate
- call `search_tools` {"limit": 0, "query": "select:connected_data_connect,connected_data_download,connected_data_open,connected_data_status,connected_data_index"}
- call `load_skill` {"file": "references/discovery-and-analysis.md", "files": null, "skill_id": "work-with-ndp"}
  - result: Loaded 5 tools; call them from your next step with the arguments their schemas give.

## connected_data_connect  (connected-data)
Connect and index an approved source folder. Returns an Indexing task handle immediately (the task-protocol skill covers handles); files are downloaded only by connected_
  - result: # Find, access and analyze scientific data

## Discover an appropriate collection

Start from the user's research question: phenomenon/variable, place or station,
time interval, resolution and desired output. Use known references and infer what
the context establishes; ask only for ambiguity that ch
- call `connected_data_connect` {"configuration": {"endpoint_id": "ndp", "resource_ids": ["07a135d1-bcd3-4e12-aff5-1e91d667fe5e", "ac7e07d8-a67c-4025-a5bf-3ccf9c324d4b", "db35cec6-e8b1-4543-b203-b301fdc99a14", "8a6fc572-d84b-4c2f-9f92-e141d7e99687", "90d20433-16ca-48ed-a44f-dbebce72f473", "ec0da0f0-1492-407c-ae35-4f35ebfe73b5", "7dc6c1c4-7da2-4ed8-86b3-a00ced222c71", "a68b6a76-3cff-48c9-8349-2e37d107943c", "4e1ccb5d-aef9-4c06-84
  - result: {"accepted": true, "activity": "executing", "handle": "task_ba142f23f31e476a9bc95e636317aeac", "kind": "Indexing", "status": "running"}
- call `wait_tasks` {"interrupt_on_mail": true, "return_when": "all", "tasks": "task_ba142f23f31e476a9bc95e636317aeac", "timeout_s": null}
  - result: {"outcomes": [{"handle": "task_ba142f23f31e476a9bc95e636317aeac", "next_cursor": null, "pending": false, "result": {"created_at": "2026-11-17T20:51:28.196262+00:00", "error": null, "input_requests": null, "last_updated_at": "2026-11-17T20:51:28.587844+00:00", "meta": {"io.modelcontextprotocol/server
- call `connected_data_open` {"folder": "", "linked": true, "offset": 0, "path": "", "range": false, "revision": "link_94d66cb4fc0e4a8887901e5cf5761b8d", "source_id": "source_47089fd0c7fb4157893367de4528ca92"}
  - result: {"access": "read_only", "entries": [{"kind": "file", "path": "1-reading.sensor.csat3.pkl", "revision": "67275179107c41270d82648d5be1b0ceb9ecec36444c64fbccb43be9ef675f74", "sha256": null, "size": 0}, {"kind": "file", "path": "2-reading.sensor.g2131i_raw.pkl", "revision": "c9f5a768ace773c55c799feeb3cb
- call `connected_data_download` {"paths": ["1-reading.sensor.csat3.pkl", "2-reading.sensor.g2131i_raw.pkl", "7-reading.sensor.li7200_raw.pkl", "8-reading.sensor.li840a.pkl", "17-reading.sensor.windobserverii.pkl", "18-V008.ndjson", "19-W01D.ndjson", "20-W01F.ndjson", "21-W038.ndjson", "27-Mobile Deployment Platform Measurements and Sensors"], "source_id": "source_47089fd0c7fb4157893367de4528ca92"}
  - result: {"accepted": true, "activity": "executing", "handle": "task_2eb33760e4b045cfa6e5a128e1717bd9", "kind": "Download", "status": "running"}
- call `wait_tasks` {"interrupt_on_mail": true, "return_when": "all", "tasks": "task_2eb33760e4b045cfa6e5a128e1717bd9", "timeout_s": null}
  - result: {"outcomes": [{"handle": "task_2eb33760e4b045cfa6e5a128e1717bd9", "next_cursor": null, "pending": false, "reason": "", "result": {"created_at": "2026-11-17T20:51:36.284081+00:00", "error": null, "input_requests": null, "last_updated_at": "2026-11-17T20:52:41.957852+00:00", "meta": {"io.modelcontextp
**assistant**: The bulk download failed with "NDP could not be reached". I'll try linked reads of a few individual resources, plus a smaller download, to see whether the failure is general or specific to the bulk request.

- call `connected_data_open` {"folder": "", "linked": true, "offset": 0, "path": "27-Mobile Deployment Platform Measurements and Sensors", "range": false, "revision": "link_94d66cb4fc0e4a8887901e5cf5761b8d", "source_id": "source_47089fd0c7fb4157893367de4528ca92"}
- call `connected_data_download` {"paths": ["17-reading.sensor.windobserverii.pkl"], "source_id": "source_47089fd0c7fb4157893367de4528ca92"}
- call `connected_data_open` {"folder": "", "linked": true, "offset": 0, "path": "18-V008.ndjson", "range": false, "revision": "link_94d66cb4fc0e4a8887901e5cf5761b8d", "source_id": "source_47089fd0c7fb4157893367de4528ca92"}
  - result: {"accepted": true, "activity": "queued", "handle": "task_f876d337c5454bf597630de27a31d18d", "kind": "Download", "status": "accepted"}
  - result: {"name": "27-Mobile Deployment Platform Measurements and Sensors", "path": "27-Mobile Deployment Platform Measurements and Sensors", "resource_id": "res_2dbb3c1f7c284d6ba953b29333cd2ff7", "revision": "link_94d66cb4fc0e4a8887901e5cf5761b8d", "sha256": "81ffd538eeb165916ed422239d1179356d4db924f3e4703a
  - ERROR result: NDP could not be reached while reading this resource
**assistant**: The HTML documentation resource opened, but the ndjson read timed out. I'll read the documentation and wait for the single-file download.

- call `workspace_resource_read` {"derivative_id": "", "limit": 0, "offset": 0, "resource_id": "res_2dbb3c1f7c284d6ba953b29333cd2ff7"}
- call `wait_tasks` {"interrupt_on_mail": true, "return_when": "all", "tasks": "task_f876d337c5454bf597630de27a31d18d", "timeout_s": null}
  - ERROR result: original resource is not text; choose a named textual derivative
  - result: {"outcomes": [{"handle": "task_f876d337c5454bf597630de27a31d18d", "next_cursor": null, "pending": false, "reason": "", "result": {"created_at": "2026-11-17T20:52:47.182898+00:00", "error": null, "input_requests": null, "last_updated_at": "2026-11-17T20:53:52.661032+00:00", "meta": {"io.modelcontextp
**assistant**: **I can't yet say whether the smoke or CO2 signal changed with the wind. None of the Konza measurement bytes could be retrieved, so no analysis was run and there is no result or figure.**

## What I found (catalog metadata only)
- **Dataset:** [NEON MDP / Sage / WIFIRE BP3D: Konza Prairie Burn Experiment](https://data.nationaldataplatform.org), ID `335fa1cb-d6d2-4e96-bc8e-c309d957b24d`, organization `sage-neon`. The catalog gives the bbox as [-96.615, 39.099, -96.608, 39.108], and the description says data were collected on 2022-04-15.
- **Sensors that could answer your question:** the dataset lists a CSAT3 sonic anemometer (`1-reading.sensor.csat3.pkl`) and a WindObserver II (`17-reading.sensor.windobserverii.pkl`) for wind. It lists LI-840A, LI-7200 and G2131-i analyzers (`8-…li840a.pkl`, `7-…li7200_raw.pkl`, `2-…g2131i_raw.pkl`) for CO2 and related gases. Three ndjson files (V008, W01D, W01F, W038 are also listed) are probably the Sage particle or air-quality streams, but I haven't opened them to confirm.
- **Not usable for this question:** the other Konza datasets (`KonzaBurn images` and the classification results) are RGB/IR images and LLaVA model outputs. They are derived classifications, not smoke or CO2 concentrations, and they have no wind data.

## What failed
- I connected the 10 selected resources and indexing succeeded.
- The bulk download failed with "NDP could not be reached while reading this resource".
- A single-file download of the WindObserver file failed the same way.
- A linked read of `18-V008.ndjson` hit an HTTP read timeout.
- The documentation resource opened, but its contents are not text, so I couldn't read it. I haven't tried a textual derivative of it.
- I stopped after these attempts rather than keep retrying a backend that is timing out consistently. This looks like an NDP-side or network outage, not an access denial or a CLIO size limit.

## What the data could support, once retrievable
- **Likely achievable:** the sonic anemometer wind direction and speed and the CO2 analyzers are all in the same dataset, so a direct comparison of CO2 against wind direction and speed over the burn window is plausible. Because the .pkl files are Python pickles, I would check their format before loading them rather than unpickle them blindly.
- **Limits to check first:**
  - Whether the CO2 records span the actual burn period.
  - Whether the analyzers are "raw" instrument output or derived values.
  - Whether the station was fixed or mobile (the platform is a "Mobile Deployment Platform").
  - Time alignment between instruments.
- **Even with clean data, expect a limited claim.** This is one burn on one day at one platform. That could show association with wind, but not cause, and not a general conclusion.

## Next steps
Retry the download once NDP is reachable (the source is already indexed as `source_47089fd0c7fb4157893367de4528ca92`). I can also try the `tools.ndp` staging route, or you can attach the files yourself. Tell me which you prefer, or just say "retry" and I'll start from the wind and CO2 files.

