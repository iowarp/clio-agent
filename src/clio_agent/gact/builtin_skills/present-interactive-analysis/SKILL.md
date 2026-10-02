---
name: present-interactive-analysis
title: Present Interactive Analysis
description: Use for answers people will explore or edit: charts, maps, forecasts, checklists, and especially email/chat/text drafts with alternative versions to tweak. Load the active A2UI catalog for exact shapes.
---

Use this skill when interaction or structure helps the person use the answer.
Data views reflect observed evidence; message drafts and guides can present
authored content for the person to edit or follow. Do not mention the protocol
or ask the user to supply component payloads.

A `create_a2ui_surface` call renders inline in the chat as an interactive
view — hoverable, clickable, linkable to the session's other views.
Producing the same data through the artifact tools instead (a `.json` spec,
a rendered `.png`) makes a file in the workspace: the user sees a file
attachment, not a chart, unless a surface for it also exists. Reach for the
surface when the ask is to see, render, plot, or map something; reach for an
artifact when the deliverable itself is a file the user asked to keep.

Component shapes are NOT in this skill — the catalog itself is the allowlist and
the source of truth (`docs/design/a2ui-compat-campaign-2026-09.md` S2/S4). Load a
catalog's index with `load_skill("a2ui-catalog-<slug>")` (see "Skills available to
you" for the ids your session can currently produce) and one component's exact
schema with `load_skill("a2ui-catalog-<slug>", file="catalog.json#/components/<ExactComponentId>")`
before producing it. Copy the complete key from the catalog index: for example,
`catalog.json#/components/clio.chart.v1`, not a display label such as `Chart`.

When a user names a public data source, use an available fetch tool or a local
shell HTTP request to obtain the data before deciding that access is unavailable.
Check the returned status and data shape; a shell request may need the person's
normal tool approval. Keep the source data as a registered artifact when a view
will query more than a small inline sample.

## Choosing a view

Match the surface to the shape of the evidence, not to what looks impressive:

- A spatial result shown as point markers (stations, sites, cities) → a map
  component.
  Use `category`/`categoryField` for groups and `value`/`valueField` for a
  measured magnitude. Putting a numeric measure into `category` makes a
  separate legend entry for every distinct number; the numeric value gives
  the renderer a continuous colour scale. Give that scale a short `valueLabel`
  and `valueUnit` when known; the renderer supplies the legend.
- A GeoJSON FeatureCollection of sites, paths, or regions → a map with
  `geojsonUri` pointing to the registered artifact. Name feature-property
  fields for labels or colour when useful; the map draws the actual shapes.
  A custom projected chart remains available when its encodings or layers
  are the point of the analysis.
- Time-ordered positions such as hurricane or vessel tracks → a map with
  `dataUri`, latitude/longitude/label fields, `trackField` for the path ID,
  and `orderField` for an ISO-8601 time or numeric sequence. The renderer joins each path and
  keeps its observations selectable.
  When an archive contains many tracks, show a meaningful period or named
  subset that people can compare. Keep the full archive as an artifact;
  simply taking the first rows can leave paths incomplete. State the period
  shown and check that the final answer matches the rendered slice.
  Spot-check a few parsed coordinates against the source before registering
  a track dataset; repeated or swapped columns can look plausible in a CSV
  while placing the paths in the wrong location.
- A registered 3D model or simulation mesh → an orbitable mesh viewport.
  It accepts common model formats; use its `format` field when the bytes are
  ambiguous, and `materialUri` for an OBJ's registered MTL companion.
  Scalar colouring, frames, and thresholds apply when the GLB contains
  CLIO FEA result fields. Read the exact catalog schema before creating it.
- A registered two-dimensional field, image grid, or geographic raster →
  `clio.raster-viewport.v1`. Give it `rasterUri`; choose `variable` for a
  NetCDF or zipped Zarr archive with multiple fields, or `band` for a
  multiband GeoTIFF. The viewer requests bounded samples while the person
  pans and zooms, and shows values and a colour legend. Use `unit` when known.
  Read the catalog entry for the exact accepted shape.
- Structured rows and columns → a data table.
- A quantity that changes over an index or time for a few series → the catalog's
  chart component (`clio.chart.v1`), typically its `trajectories` preset.
- Many entities or grouped comparisons (one line per sample over time, spectra,
  distributions per group, a matrix of values) → the same `clio.chart.v1`
  component. Use one of its **named presets** by filling in field names; don't
  write a chart spec by hand unless no preset fits. That's longer, more likely to
  fail, and still has to pass the catalog's spec guard.
- For anything non-trivial in size, pass a registered artifact reference instead
  of inlined rows. Let the server-side query filter, aggregate or downsample it
  (for example, a bounded number of points per entity) instead of trimming the
  data yourself.

- A single observed value → one metric component per value.
- A requested illustration, photograph, or generated schematic the person
  should inspect alongside data → an Image in the same surface. Register the
  actual file first and use its artifact URL; keep a short description of what
  it depicts. A file that has not been created or registered cannot be shown.
  For a generated schematic, keep its labels clear of the title, legend, and
  edges at the displayed size. Inspect the rendered image when possible and
  correct collisions before presenting it. The surrounding surface can carry
  the report title and readings; they need not be repeated inside the image.
- A weather or field-site conditions question with observed and forecast data →
  a compact weather view, accompanied by the decision-relevant answer in prose.
  Let the view carry the hour-by-hour and day-by-day detail; keep the prose to
  the useful judgment, uncertainty, and source rather than repeating its rows.
  For a live named-location forecast, `get_weather_forecast` supplies the data;
  the renderer makes no weather request of its own.
  Give `observedAt` and every hourly `time` an ISO 8601 UTC offset or `Z`;
  use `YYYY-MM-DD` for each daily date.
- A request for email, chat, or text alternatives the person can revise → a
  message-draft view with labelled versions. The person edits the draft in
  place and decides whether to copy, use, or open an email version in their
  chosen mail app; CLIO does not send it.
- A protocol, recipe, or setup guide with steps the person will perform → an
  interactive steps view, especially when timers or quantities matter. For a
  recipe, put measured materials in `ingredients` and actions in `steps` so
  the completion count tracks cooking rather than the shopping list. Put
  amounts that change with the scale in quantity fields. Keep step details
  true after a changed count: if `quantity` shows 8 mL for 4 samples, write
  “Add 2 mL to each tube” in `detail`, not “8 mL total for 4 samples.” The
  fixed per-unit instruction matters when the starting count changes. In a
  recipe, omit fixed cup/tablespoon/egg equivalents from `detail` beside a
  scaled amount; give countable units singular and plural labels so the
  display reads correctly at one and many. If the amount is adjustable, keep
  the title independent of the starting count (for example “Tomato pasta”
  rather than “Tomato pasta for two”); the control displays the current
  servings. Group related prep so the checklist remains easy to scan.
- A parameter the person should adjust while inspecting a result → a bound
  slider; use its two-value range mode for an interval. A date or time cutoff
  can use `DateTimeInput`. Read the component schemas for their value shapes,
  then bind the values used by the view's data query.
- Ongoing/completed work, a warning, or a diff → the matching status/callout/diff
  component, never repurposing a generic text block for it.
- A durable export (an image, a report) the user did not ask to view inline → a
  registered artifact reference, not an inlined image.

### How to set up filters

Choose filters for the scientific question, not for the component type. Inspect
the available fields, their units and values, and the range or number of
distinct values in the displayed slice. Ask what a reader would narrow to
isolate a phenomenon, compare cohorts, or check an outlier. Usually a few
interpretable dimensions are enough. A row ID or nearly unique label is rarely
a useful filter; a coordinate, measurement, or time field may be useful when
the question calls for a spatial, numeric, or temporal range. Choose time
precision that matches the data and question: years for a multi-year archive
and dates for a shorter span. If the question needs a sub-day cutoff, apply
that exact interval in `dataQuery.filter` and show the observation timestamps.

Keep chosen fields in the registered artifact and the view's projected
`dataQuery.columns`. On a referenced map, set `filterFields` to the real columns
worth narrowing; the viewer creates the controls and applies them to the map
and `Reference this`. If omitted, it exposes its default map fields. Use
`dataUri` when a map needs filters on measured columns, even for a small
dataset: inline points do not carry arbitrary measurements. A referenced map,
chart, or table needs a registered CSV or Parquet table; a source JSON file is
not a queryable table. Preserve numeric columns as numbers and timestamps as
ISO-8601 strings with an offset when writing it. Check the registered table's
schema and a sample row before presenting the view: a conversion can silently
turn a timestamp into locale-formatted text. A chart exposes
native filters for encoded fields; a table exposes them for displayed columns.
Include a field in an encoding or table column list when readers need it
visible there. Do not add filter buttons or actions to the component spec.
Use `dataQuery.filter` for the initial scientific slice, and leave subsequent
exploration to the viewer. Confirm that the visible rows, counts, and
`Reference this` payload describe the same filtered subset.

## Composing surfaces

Prefer one small surface at the step it explains. Reuse a stable semantic
`surface_id` to update that view in place. Do not accumulate unrelated work into
one final tabbed dashboard. Tabs are appropriate only when several views of the
same result belong together and the available width justifies them.
For a briefing or report that asks for several related views, compose the
relevant metrics, media, map, chart, and table with Row, Grid, or Tabs where
that makes comparison easier. Let each view carry its own detail and avoid
repeating a generic heading around it. Use the user's named measurement in
titles, legends, and captions; when only units are given, call it a reading
instead of assigning an unstated physical phenomenon.
For a correction in a later turn, call `inspect_a2ui_surface` to find the
existing id and current components, then update that surface. A correction
should replace the earlier view so both versions do not compete in the chat.

## Custom charts with Altair

The general chart component renders **Vega-Lite**. When no named preset fits
(a layered overlay, a particular facet layout, a custom encoding), write the
chart in Python with **Altair**, which produces Vega-Lite, and pass the
exported spec to the component instead of writing Vega-Lite JSON by hand.

- **Data comes only from the component.** Build every chart on
  `alt.NamedData("source")`. A DataFrame, URL or inline values passed to
  `alt.Chart(...)` get inlined into the spec, and the server's chart guard
  refuses them. Rows arrive through the component's inline data or its
  artifact reference.
- **Layout keys the guard allows.** Past the usual mark/encoding grammar,
  the guard also allows `facet` + `columns` (a wrapped small-multiples
  grid), `spacing`/`padding`/`align`/`bounds`/`center` (composition
  layout), and `projection` (a geoshape or lon/lat point map) at the top
  level. No `url` anywhere, in any of them.
- **A choropleth from inline rows.** A row's cell can hold a GeoJSON
  Geometry object instead of a scalar, so a `geoshape` mark can draw real
  shapes straight from rows the agent already has, no artifact needed. Set
  `projection` for the map projection; leave `projection.fit` unset — the
  renderer fits the map to the geometry actually present in the rows.
- **Shared selection.** Charts, maps and tables reading the same artifact
  automatically link through its stable `__row` key. A custom chart gains a
  point selection from the renderer when it has a selectable field; the
  renderer also supplies box selection, zoom and Reference this.
- **Keep specs small.** The guard caps size, nesting and view count.

Write a Python file that assigns the Altair chart to `chart`, resolve this
skill's directory as `SKILL_ROOT`, and export plus pre-check it:

```text
uv run --no-project --with "altair>=5" --with "clio-schemas>=0.5.2" python "SKILL_ROOT/scripts/vega_spec.py" build "CHART.py" "SPEC.json"
```

The pre-check calls the same guard the server runs
(`clio_schemas.a2ui.chart_spec.check_chart_spec`), so it names the same rule
a server refusal would instead of a hand-copied approximation;
`vega_spec.py check SPEC.json` re-checks an existing spec. The server's
guard is still the final authority. Load the chart component's schema from
the catalog skill before putting the spec into a surface.

## Linking views on one surface

When multiple views on one surface show the same observations, build them from
one shared dataset. The renderer links chart, map and table selection by the
artifact's stable `__row` key when they use the same `dataUri`. Small inline
views also link when each has the same set of unique entity values, even if
the rows appear in a different order. This is normal interaction; the person
does not need to ask for linking, and you do not need to add selection controls
or a selection path. The user can click or box-select a chart, map or table,
see matching rows highlighted, and use Reference this to carry those rows and
active filters into the next message. For larger or evolving data, prefer one
shared artifact so filtering, sorting and pagination retain stable identity.
Only bind an explicit `/selection/` path with matching `selectionField` values
when separate datasets share a concept across different columns or files.
Views on different surfaces keep separate selection state.

## Preserving a user's choice into the next turn

A click or selection inside a surface is local visual state only — it does not by
itself reach the agent. When the user must choose among options before analysis
continues, pair the selector with a submit action whose event delivers the
resolved selection as structured context; load the catalog's Button/action and
your chosen selector's schemas together so the wiring between them is correct on
the first try. Bind the selector's value to the same data-model path read by the submit action; a path mentioned only by the action has no selected value to deliver. Include the selected item's displayed fields in that context
when a follow-up may ask about them; an ID alone can lose the detail the
person just saw. If a later question needs details beyond a selected ID, use
`inspect_a2ui_surface` to read the existing view before answering. A field
visible in that view should not be called unavailable merely because the
submit event carried only its ID.

## Producing and verifying

Call `create_a2ui_surface` once per coherent revision (leave `catalog_id` empty
to use the session's negotiated catalog). Require `rendered=true` and
`state=ready` before saying the view is available. A refusal names what to load
next (`hint`) — load exactly that component's schema, correct the call, and
retry a bounded number of times; do not print the payload as chat text, silently
replace an interactive component with a static image, or claim success on a
refusal.
