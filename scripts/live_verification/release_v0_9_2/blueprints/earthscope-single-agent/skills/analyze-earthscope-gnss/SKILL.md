---
name: analyze-earthscope-gnss
title: Analyze EarthScope GNSS Data
description: Profile the exact staged station series and state only the coverage, displacement, and uncertainty facts the tool actually observed.
---

Use `pandas_profile_csv` on the exact `acquisition.local_path` returned by staging.
Do not compose or rename the path. If there is no staged, analysis-ready station
CSV, record `profile.status=blocked` and stop.

Preserve the tool's file size, columns, rows examined, rows used for numeric
summaries, missing-value scope, and reported min/max/mean values. A scan-limited
profile is not full-file truth. When `scan_limited=true`, `row_count` is the scan
cap reached, not the file's total row count; describe it as "at least" that many
rows and never relabel it as a total. Do not infer or claim full duration, recency,
cadence, continuity, gap-free coverage, completeness, noise, quality, reference
frame, or suitability unless a tool explicitly measured that property. A filename
suffix is not a date range. Treat opaque quality fields as opaque.

When discussing station suitability, use only tool-observed station distance,
resource availability, columns, and profile facts. Prefer "geographically
grounded candidate" or "preliminary profile" to an unsupported quality verdict.
Record the exact scope and limitations in `workflow_state.profile`.
