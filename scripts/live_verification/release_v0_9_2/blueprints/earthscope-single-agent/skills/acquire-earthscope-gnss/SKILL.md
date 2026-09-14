---
name: acquire-earthscope-gnss
title: Acquire EarthScope GNSS Data
description: Discover, normalize, spatially rank, and stage a real EarthScope GNSS station series from a grounded region without inventing paths or stations.
---

Use this skill only after the region is grounded. Perform the following procedure
yourself in causal order.

**Local-first staging.** Whenever a step below stages a resource with
`ndp_stage_resource`, first look for a local copy: if a file whose name matches
that resource already exists in the active workspace, use that local path as the
staged result and continue the procedure unchanged — do NOT download. Only call
`ndp_stage_resource` to fetch remotely when no local copy of that resource is
present. This applies both to the station catalog and to each station's
time-series CSV. Never invent a path — use a local file only when it is actually
there; otherwise fetch remotely as written.

1. Find the EarthScope station catalog with
   `ndp_search_datasets(search_terms=["earthscope", "converted"], limit=10)`.
   Select the returned `earthscope_converted_data.csv` resource URL.
   Preserve that exact tool-observed URL in workflow state and reuse it throughout
   the session. If a resumed turn no longer has the URL in active context, repeat
   this bounded search once and select the same returned resource; do not insert a
   redundant `ndp_get_dataset_details` dependency between discovery and staging.
2. Stage that catalog by URL with `ndp_stage_resource`. When an active workspace
   root is available, use it as `output_dir`. Copy the returned raw path.
3. Normalize the catalog with `pandas_filter_data`, keeping rows where `Latitude`
   is between -90 and 90 and writing `earthscope_stations_clean.csv` under the
   active workspace root. This also rewrites the source as UTF-8. The verified
   columns are station id `Site`, latitude `Latitude`, and longitude `(deg)`;
   `Longitude` is elevation and must never be used as longitude.
4. Call `geo_filter_points_by_radius` once with the fixed region center/radius and
   explicit columns `Latitude`, `(deg)`, and `Site`. Preserve the returned ranked
   points, `total_points`, `within_radius_count`, and `skipped_invalid`.
5. When the filter returns multiple ranked stations with coordinates, present the
   spatial evidence before searching for a station series. Load
   `present-interactive-analysis`, then create or update `earthscope-stations`
   immediately from only the bounded, tool-returned ranked points. Identify the
   first ranked point as the leading candidate; otherwise prefer the interactive map
   over a static or prose-only presentation. When the user asks to choose before
   staging, use the skill's human-selectable map recipe: include one bounded
   mutually-exclusive `ChoicePicker`, default it to the leading candidate, and
   submit `selected_station_ids` through the recipe's `agent.submit` action. Stop
   after the surface is ready. Do not search for or stage a station series until a
   Browser-submitted structured selection resumes the session. Require
   `rendered=true` and `state=ready` before continuing to station-resource search.
   If coordinates are unavailable, use a compact table instead. Skip this step
   only when one or zero stations were returned or presentation itself fails; in
   that case keep the observed station evidence and report the presentation
   failure rather than fabricating a view.

The staged raw catalog is never analysis-ready, even when a parser can read it.
Steps 2, 3, and 4 are a strict dependency chain: never call
`geo_filter_points_by_radius` on the raw staged catalog, and never omit
`pandas_filter_data`. If normalization fails, report that failure instead of
continuing with a partial or high-invalid filter result.

A zero-candidate conclusion is valid only when the filter structurally succeeded,
input rows were nonzero, invalid skips were not substantial, and
`within_radius_count` is zero. Otherwise record `filter_failed`; retry once with
the same radius and explicit columns. Never widen the region to manufacture a
candidate.

For a request that needs a station series, walk the ranked station ids in order:

- search with `ndp_search_datasets(dataset_title=<station id>, limit=20)`;
- choose a returned per-station CSV resource whose filename starts with that exact
  station id;
- stage its URL with `ndp_stage_resource(max_bytes=60000000)`;
- copy the returned station id, URL, path, and size byte-for-byte.

Try the next ranked station only when the current station has no matching CSV or
staging fails. A successful acquisition requires an on-disk station time-series
CSV and `analysis_ready=true`; the metadata catalog alone is `metadata_only`.
Never derive a station id or filename from a city name.

After a station CSV stages successfully, update `earthscope-stations` once so the
staged station is visibly selected rather than merely the leading candidate.
Preserve the same bounded points and observed distance/count evidence. Do not
wait until the end of the turn or combine this map with later time-series output
inside a large dashboard.
