# Wildfire grind — review log (failure → matcher/fix)

Each entry: a distinct failure found by reading a run trace, and what now guards
against it. Provider: argonne_metis / gpt-oss-120b.

| r | failure (from trace) | fix / matcher |
|---|---|---|
| r1 | `main` delegated to `data.fire_discovery` (dotted leaf id) → `delegate_not_available`; 0 tools, dead 6.4s turn. | `main.md`: delegate only to direct children by exact id (`data`/`geography`/…); `data` owns fire/smoke/air sub-experts. Drop the nested tree from main's prompt. |
| r2 | Full route + tools ran (114s), but `geospatial_render_feature_map` got `output_path=/workspace/...` → `Permission denied: '/workspace'`; render failed 6x, no PNG. Model invents an unwritable path (#645). | `geospatial_server._safe_artifact_path`: relocate render output into a writable artifact root (`CLIO_ARTIFACTS_ROOT` or `<cwd>/.clio/artifacts/geo`), keeping the caller's filename. |
