---
name: create-dashboard
title: Create Dashboard
description: Author and revise a substantial interactive dashboard in the side panel using the existing A2UI components, tabs, layouts, shared data bindings, and linked views.
---

Use this skill when the person wants a dashboard or consolidated interactive
report. Author an interconnected presentation of substantial data, with a clear
overview and useful detail. The dashboard is an artifact alongside chat, with
the existing A2UI semantics throughout. Develop it over as many tool calls as
useful; a dashboard is not limited to one or two graphs or one generation call.

Inspect relevant earlier views with `inspect_a2ui_surface` and read their
referenced data. Decide what tells the story: retain meaningful comparisons,
remove repetition, add explanations, and generate new evidence-based figures
when useful. For example, show an original shape once with two chosen evolutions.
Keep units, source identity, and limitations clear. Do not invent measurements
or imply that a derived visual was part of the original recording.

Load the active catalog index and exact component schemas with `load_skill`.
The catalog remains the authority for shapes and actions. Reuse its tabs, rows,
columns, lists, text, metrics, charts, tables, meshes, images, and inputs. Use
tabs or other catalog-supported navigation to make substantial content readable.
Keep an overview, comparisons, and supporting details easy to find. Do not ask
the person to supply component payloads.

Choose layout deliberately. `Grid` suits a group of KPI cards or side-by-side
analyses; `Frame` gives a section its own title and explanatory context. Use
`Row` for a short horizontal group rather than squeezing many cards onto one
line. Compose these inside tabs and columns as useful, and check the docked
view as well as an expanded view. Dividers, lists, images, icons, and modals
remain available when they serve the presentation; do not add them solely to
exercise the catalog.

Write an editable JSON document inside the workspace:

```json
{
  "format": "clio.dashboard.document.v1",
  "title": "Design evolution",
  "catalog_id": "ACTIVE_CATALOG_ID",
  "source_surface_ids": ["earlier-comparison"],
  "components": [
    {"id": "root", "component": "Column", "children": ["summary", "analysis"]},
    {"id": "summary", "component": "Text", "text": "An evidence-based summary."},
    {"id": "analysis", "component": "Text", "text": "Replace this with your authored tabs and linked data views."}
  ],
  "data_model": {}
}
```

There must be one `root` component. The whole dashboard shares one ordinary
A2UI data model, so related controls and views can bind to the same paths.
Use the catalog's existing selection and query/filter contracts. Linked datasets
need a real shared entity field; matching row numbers in unrelated datasets
does not establish a relationship. Mesh views with a deliberately shared
`syncGroup` rotate and zoom together, including views authored from results
produced hours apart. Choose these links based on what the comparison means.

Reference full registered source data rather than sampling it to fit the report.
Small inline tables remain allowed by the catalog. Generate additional images
or charts through real analysis and normal artifact registration. Keep a
derivation distinct from observed evidence. Do not repeat the dashboard's views
inline in chat.

Call `publish_dashboard_report(definition_path="reports/design.dashboard.document.json")`
to validate and publish an immutable artifact version. Return its artifact
reference so the person can open the dashboard in the side panel. Keep the
returned report id and source path. For follow-ups, inspect the referenced
artifact version, edit the source through additional tool calls, and publish
again with `report_id`. Earlier artifact versions remain available. Add, remove,
rearrange, annotate, or interconnect content as the person's goals require.

The viewer supplies standard Reference this and labelled image capture actions
for the dashboard and its individual views. References identify the artifact
and source document as well as the selected component or data zone. These let
the person ask you to revise part of the system or produce a new derived visual.
PNG downloads capture the displayed dashboard; self-contained HTML includes the
entire dashboard, its tabs and interactions, and referenced data. Missing
dependencies are reported rather than silently omitted from an export.
