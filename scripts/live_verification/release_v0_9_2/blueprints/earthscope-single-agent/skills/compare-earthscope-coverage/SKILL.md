---
name: compare-earthscope-coverage
title: Compare EarthScope Station Coverage
description: Compare tool-observed GNSS station counts across several fixed regions without staging station time-series resources.
---

Use this skill for metadata-only regional comparisons. For every named region,
resolve it fresh when requested and preserve the same user-specified radius for
every region. Reuse one already-staged cleaned station catalog when its
provenance is current; do not download or stage any station time-series CSV.

Before dispatching any region, the parent must have the literal path to one
current cleaned station catalog and the verified `Latitude`, `(deg)`, and `Site`
columns. The raw staged EarthScope catalog is never this cleaned catalog: its
header repeats unit labels and a plain CSV reader can overwrite the real
longitude column. If retained state does not contain a previously normalized
`earthscope_stations_clean.csv`, perform this exact preparation in the parent:

1. Find and stage only the EarthScope station-metadata catalog using the same
   bounded search and `ndp_stage_resource` procedure documented by
   `acquire-earthscope-gnss`.
2. Call `pandas_filter_data` on the returned raw path, keep rows whose
   `Latitude` is between -90 and 90, and write
   `earthscope_stations_clean.csv` under the active workspace root.
3. Use the returned normalized output path for every parent and child radius
   filter. Never substitute the raw staged path, and never treat profiling the
   raw file as normalization.

Catalog metadata preparation is allowed for this comparison; station
time-series staging is not. Do not dispatch an assignment with a missing,
described-only, raw, or failed-normalization catalog path. If normalization
fails, report the comparison unavailable instead of dispatching invalid work.

For three or more independent regions, fan out by calling
`load_skill(skill_id="delegate-earthscope-region", task=<specific assignment>)`
once per delegated region, and resolve directly any region you do not delegate.
Concurrent children are admission-bounded: spawns past the runtime's fan-out
bound queue instead of running in parallel, so a wider fan-out does not
necessarily finish sooner. Each task must contain the full place, common radius,
the literal cleaned catalog path, and the explicit `Latitude`, `(deg)`, and
`Site` columns, because a child cannot discover, stage, or normalize the catalog
itself. Collect every returned task id before comparing the regions; children
return counts only, and the parent still owns the comparison
and its presentation. For one or two regions, or when parallelism adds no value,
perform them directly.

Call `geo_filter_points_by_radius` separately for each grounded center with the
same catalog and explicit columns. Report the exact `within_radius_count` values,
filter status, input rows, and invalid skips. Rank regions only from those
returned counts. If any filter is structurally invalid, show that region as
unavailable rather than comparing a fabricated zero.

Comparing two or more resolved regions benefits from direct spatial and tabular
interaction. After all regions return, load `present-interactive-analysis` and
create or update `earthscope-coverage` with a compact count table and map. Every
row and point must come from the geocoder/filter results, and the visible label
must make the shared radius and metadata-only scope clear.
