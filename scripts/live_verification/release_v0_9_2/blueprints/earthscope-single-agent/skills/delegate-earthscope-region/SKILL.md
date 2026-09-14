---
name: delegate-earthscope-region
title: Delegate an EarthScope Region
description: Resolve and count one independently assigned EarthScope region in a fresh self-directed child turn.
effect: spawn_subagent_with_skill
---

You are already running this skill in the delegated child. Do not call
`load_skill`, `spawn_skill_task`, or any other delegation tool. Complete the
specific regional assignment directly in this child turn.

The assignment must include the full place name, fixed radius, exact cleaned
catalog path, and required columns. Do not infer missing values from another
region or stage any station time-series resource.

1. Resolve the assigned place with `geo_geocode`; retry once with a more specific
   place string only when the first result is empty or ambiguous.
2. Call `geo_filter_points_by_radius` with the assigned radius, exact catalog
   path, latitude `Latitude`, longitude `(deg)`, and station id `Site`.
3. Return the resolved label and center, radius, exact `within_radius_count`,
   `input_rows`, `skipped_invalid`, filter status, and provenance. If either tool
   fails structurally, return that visible blocker rather than a zero.

Do not create A2UI in this child. The parent owns the comparative table/map after
all regional evidence has returned.
