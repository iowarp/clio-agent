# Wildfire grind — review log (failure → matcher/fix)

Each entry: a distinct failure found by reading a run trace, and what now guards
against it. Provider: argonne_metis / gpt-oss-120b.

| r | failure (from trace) | fix / matcher |
|---|---|---|
| r1 | `main` delegated to `data.fire_discovery` (dotted leaf id) → `delegate_not_available`; 0 tools, dead 6.4s turn. | `main.md`: delegate only to direct children by exact id (`data`/`geography`/…); `data` owns fire/smoke/air sub-experts. Drop the nested tree from main's prompt. |
| r2 | Full route + tools ran (114s), but `geospatial_render_feature_map` got `output_path=/workspace/...` → `Permission denied: '/workspace'`; render failed 6x, no PNG. Model invents an unwritable path (#645). | `geospatial_server._safe_artifact_path`: relocate render output into a writable artifact root (`CLIO_ARTIFACTS_ROOT` or `<cwd>/.clio/artifacts/geo`), keeping the caller's filename. |
| r3 | Non-deterministic: `main` emitted a premature narration ("awaiting the live hazard data…") and finalized with 0 handoffs/tools (r2 had driven the full route — same pack). Loose planner-driven main is too flaky. | Port the proven EarthScope main structure: `module: chain_of_thought` + typed/structural continuation-contract chain (start→data, data→analysis, analysis→visualization/synthesis by `when_state impact.present`, visualization→synthesis) + "first response is a delegation to data, never finalize before synthesis". Fold acquisition under `data` (fire→geography→smoke→air, emits `workflow_state.acquisition`); `analysis` emits `workflow_state.impact.present`. |
| r4 | Contract chain made the route reliable (full data→analysis→viz→synthesis), but render failed: model passed layer `geojson` as invented filenames (`smoke_forecast.geojson`) → "path not found". Real GeoJSON never reached the renderer (LLMs can't inline 100s of features). | File-based flow: `query_arcgis_features` relocates `output_path` to writable artifacts dir; `visualization` is now self-contained — fetches each layer with `output_path` (saving real GeoJSON files) and renders from the returned saved paths, never invented names. |
