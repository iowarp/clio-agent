---
title: "Linked data"
description: "Select once and find the same observation across views."
source: https://clio.iowarp.ai/docs/widgets/linked-data/
---

# Linked data

Select once and find the same observation across views.

When CLIO draws several views of the same records, the views can share selection. A station selected on a map is the same station highlighted in a chart or table. The person does not have to ask the agent to add selection controls.

*An interactive example is shown on the site version of this page.*

## How linking works

Views reading the same dataset share each row's stable identity. Inline map, chart, and table views can also link by a unique entity field. When different datasets describe one conceptual entity, the agent can bind them to a shared selection path. [See the chart contract](docs/widgets/chart.md) and [map contract](docs/widgets/map.md) for the data fields.

A control can write to a data-model path that another component reads. In the thermal example, elapsed time is bound to the viewport's frame. The two viewports also share a camera synchronization group, so the comparison keeps the same viewing angle. [See the numeric slider](docs/widgets/numeric-slider.md) and [3D viewport](docs/widgets/mesh-viewport.md) for their inputs.

## One viewer that follows the selection

A 3D or raster viewer can take its file from the selection instead of a fixed file.
In a parameter sweep the agent lays out a grid or table of cases and one large
viewer bound to the selected case's mesh. Choosing a case loads that design, so a
dashboard needs one viewer per study rather than one per case. Viewers load only
when they scroll into view, and the agent sets their height so the result being
read gets the room it needs.

![Dashboard titled Topology optimization sweep: case browser, with a heatmap of surface area by volume fraction and filter radius, a table of cases, and a large 3D view of the selected design](plugin-asset://guide/captures/docs-case-browser.webp)
*A case browser the agent built for a 48-case sweep: the heatmap and the table select a case, and the 3D view shows its design. Here it was opened from a link with case 12 selected.*

## Links between views

A button or a workflow node can open another view: one of the study's
[views](docs/science-log.md) or a saved dashboard from any conversation in the
workspace. The link can carry a selection, so the view opens on the case it names.
An exported dashboard keeps its selections and the files they show, and works
offline.

For a larger agent-authored report, see [Dashboards](docs/dashboards.md). The
development workflow uses these same linking and layout semantics in a saved
artifact with tabs, explanations, and supporting evidence.
