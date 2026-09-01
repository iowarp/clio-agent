---
name: present-interactive-analysis
title: Present Interactive Analysis
description: Optionally present observed data through validated A2UI maps, tables, charts, metrics, artifacts, and progressive updates without guessing protocol props.
---

Use this generic presentation skill only after the underlying evidence exists and
only when interaction or structure helps the user more than prose. A2UI is a view
of observed state, never an analysis substitute. Do not mention the protocol or
ask the user to supply component payloads.

Prefer one small surface at the step it explains. Reuse a stable semantic
`surface_id` to update that view in place. Do not accumulate unrelated work into
one final tabbed dashboard. Tabs are appropriate only when several views of the
same result belong together and the available width justifies them.

Use only these validated component shapes. Replace every example value with
current evidence. The component list is flat and has exactly one component whose
id is `root`.

## Map

```yaml
surface_id: observed-region
components:
  - id: root
    component: clio.map.v1
    title: Observed locations
    points:
      - id: observed-id
        label: Observed label
        latitude: 0
        longitude: 0
        detail: Observed detail
        category: observed-category
```

Map points require `id`, `label`, numeric `latitude`, and numeric `longitude`.
The optional fields are `detail` and `category`. Never provide a tile URL,
basemap style, image URL, geocoder URL, CSS, or scripts.

## Data table

```yaml
surface_id: observed-table
components:
  - id: root
    component: clio.data-table.v1
    columns:
      - {key: id, label: Identifier}
      - {key: value, label: Observed value}
    rows:
      - {id: observed-id, value: 0}
```

Tables accept `columns` and `rows`, not `title`. Keep rows bounded. Compose a
Text label in a Column only when the surrounding conversation does not already
name the table.

## Interactive time series

Prefer a data-backed interactive chart over a rendered image. Reference the
registered CSV artifact and name the exact observed columns:

```yaml
surface_id: observed-series
components:
  - id: root
    component: clio.time-series.v1
    title: Observed time series
    dataUri: artifact://registered-csv-artifact-id
    xKey: time
    yKeys: [east, north, up]
```

`dataUri` must use `artifact://<artifact-id>` with the `artifact_id` returned
when the CSV was registered. `xKey` names the x column and `yKeys` names one to
five numeric columns. The renderer obtains a
bounded preview from that artifact and owns hover values, legend interaction,
zoom, and pan. Inline `series` rows are also valid for small already-observed
datasets. A PNG is a durable export, not the interactive chart; show it as an
artifact only when the user explicitly opens that artifact, or as an explicit
degraded fallback. Never place a static image of the same data beside or below
an interactive time series. The registered image remains available through the
conversation artifact and workspace canvas without duplicating the chart.

## Artifact

```yaml
surface_id: observed-artifact
components:
  - id: root
    component: clio.artifact.v1
    name: result.png
    uri: artifact://registered-artifact-id
    mediaType: image/png
```

Artifact requires `name`, `uri`, and `mediaType`. It does not accept
`artifact_id`, `kind`, `path`, or a bare filesystem path.

## Metrics and limitations

Each `clio.metric.v1` represents exactly one value. Several metrics require one
component per metric inside a Row or Grid. A callout requires `title`, `body`,
and `severity`; it does not accept `text` or `level`. A status uses `detail`, not
`message`. Do not add properties absent from the relevant shape.

Call `create_a2ui_surface` once per coherent revision. Require `rendered=true`
and `state=ready` before saying the view is available. If validation fails,
correct the props from this skill and retry a bounded revision; do not print the
payload as chat text, silently replace an interactive chart with an image, or
claim success.
