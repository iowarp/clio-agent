# NDP Meeting Demo Playbook

This playbook is for live CLIO demos with NDP/EarthScope collaborators. The
primary goal is to run prompts in front of attendees and show the execution
trace, not to replay static results.

## Live Backend

Start the backend:

```sh
uv run clio-agent-gact --host 127.0.0.1 --port 17831
```

Configure ALCF Sophia in the UI, or through the API:

```sh
python - <<'PY'
import httpx

base = "http://127.0.0.1:17831"
payload = {
    "provider": "argonne",
    "api_base": "https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1",
    "model": "openai/gpt-oss-120b",
    "temperature": 0.2,
    "max_tokens": 32000,
}
print(httpx.put(f"{base}/v1/providers/lm", json=payload, timeout=30).json())
PY
```

For rehearsal evidence with live trace output:

```sh
python scripts/run_demo_benchmark.py \
  --base-url http://127.0.0.1:17831 \
  --lane marketplace_agents \
  --case marketplace_seismic_waveform_review \
  --marketplace-source external/clio-agent-marketplace \
  --output-jsonl tmp/ndp-meeting-seismic/marketplace_seismic_live.jsonl \
  --report tmp/ndp-meeting-seismic/marketplace_seismic_live.md \
  --watch-events
```

## Demo 1: Geographic EarthScope/NDP Seismic Workflow

Use this prompt live in CLIO:

```text
Explore seismic activity over the last 7 days around the San Diego, California area. Start with NDP catalog discovery for relevant seismic waveform data; if catalog staging is too large or unavailable, resolve the requested geography, discover recent public earthquake and EarthScope station evidence, stage a bounded SAC waveform, inspect it, compute trace statistics, and produce a PNG plot artifact.
```

Flexible variants:

```text
Explore seismic activity over the last 5 days around Los Angeles, California...
Explore seismic activity over the last 14 days around Anchorage, Alaska...
Explore seismic activity over the last 7 days around 32.7157, -117.1611...
```

Expected live trace signals:

- `main -> data -> ndp_catalog`
- NDP tools: `ndp_search_datasets`, `ndp_get_dataset_details`,
  `ndp_stage_resource`
- `main -> analysis -> sac_format`
- SAC tools: `sac_discover_earthscope_region_waveform`,
  `sac_inspect_archive`, `sac_compute_trace_statistics`
- `main -> visualization`
- `sac_plot_traces`

What this proves:

- The prompt is geographic and can change at runtime.
- CLIO resolves the region, queries recent public USGS events, finds nearby
  EarthScope station/channel evidence, stages a bounded SAC file, computes
  statistics, and creates a verified plot.
- The workflow runs through registry-loaded Agent Blueprints and scoped tools,
  not hardcoded native experts.

Current caveats:

- Named-place geocoding is intentionally U.S.-scoped for this demo. Explicit
  latitude/longitude is accepted.
- Event/station availability changes with public services. If a recent region
  has no waveform data, retry with a larger radius/window or a more active
  U.S. region.
- This is waveform discovery and inspection, not earthquake interpretation or
  hazard forecasting.

Verified rehearsal:

- ALCF/Sophia live run passed in `126.0s`.
- Report: `tmp/ndp-meeting-seismic/marketplace_seismic_live.md`
- Evidence JSONL: `tmp/ndp-meeting-seismic/marketplace_seismic_live.jsonl`
- Verified artifact:
  `.clio-agent-artifacts/charts/sac_traces_earthscope_CI_BAR_--_BHZ_2026-05-29T021201.png`

## Demo 2: Terrain/Topography Suitability

Use this prompt live, or adapt the paths to attendee data:

```text
Evaluate these terrain points for site suitability: /tmp/clio-benchmark-data/terrain_points.csv. Grid them to /tmp/clio-benchmark-data/terrain_points_gridded.csv, derive terrain, and identify cells with elevation between 100 and 104 meters and slope below 60 degrees. Use the ready DEM /tmp/clio-benchmark-data/terrain_dem.csv only as a comparison if needed.
```

Expected live trace signals:

- `main -> terrain_derivation -> gridding`
- `terrain_pointcloud_read`
- `terrain_dem_terrain`
- `main -> suitability`

Fire-spread framing:

- This is a terrain/topography screening workflow that can support fire-spread
  research discussions by deriving slope/elevation evidence.
- It is not yet an active fire-spread simulation. If the collaborator provides
  public fuel, weather, ignition, or perimeter data, this becomes the next
  data-integration target.

Verified rehearsal:

- ALCF/Sophia live run passed in `99.8s`.
- Report: `tmp/ndp-meeting-seismic/marketplace_terrain_live.md`
- Evidence JSONL: `tmp/ndp-meeting-seismic/marketplace_terrain_live.jsonl`

## Demo 3: NDP Fire/Climate Catalog Discovery

Use this prompt live when discussing the possible fire-spread collaborator:

```text
Search the National Data Platform catalog for public wildfire, fire spread, fuel, weather, terrain, or remote-sensing datasets that could support a fire-spread workflow in Southern California. For each promising candidate, report the organization, dataset id, resource type, access path, and whether CLIO can stage it directly today.
```

Expected trace signals:

- `data -> ndp_catalog`
- `ndp_search_datasets`
- `ndp_get_dataset_details`
- optional `ndp_stage_resource` only for bounded resources

What this proves:

- CLIO can honestly discover candidate public data and surface staging/access
  blockers instead of pretending unsupported resources were analyzed.
- This is a good bridge conversation before promising a fire model.

## Demo 4: City/State Swap Challenge

Ask the attendee for a U.S. city/state during the meeting and run:

```text
Explore seismic activity over the last 10 days around <CITY, STATE>. Resolve the region, find recent public earthquake events, choose a nearby EarthScope station/channel, stage a bounded SAC waveform, inspect it, compute trace statistics, and plot it.
```

If the requested geography is unsupported, say that the current demo supports
U.S. city/state hints or explicit latitude/longitude and ask for coordinates.

## Evidence Standard

For a demo to count as successful, confirm all of the following:

- The live trace shows the intended Agent Blueprint and child expert path.
- Tool calls include the expected NDP, SAC, or terrain tools.
- Any downloaded/staged file exists on disk and is the file later inspected.
- The final answer cites observed event/station/resource/artifact paths, not
  invented paths.
- The report JSONL is saved for post-meeting trace review.
