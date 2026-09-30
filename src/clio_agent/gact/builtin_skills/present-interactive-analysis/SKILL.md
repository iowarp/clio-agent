---
name: present-interactive-analysis
title: Present Interactive Analysis
description: When and why to present observed data as an interactive A2UI surface instead of prose, and how to reach the exact component shapes for the active catalog.
---

Use this generic presentation skill only after the underlying evidence exists and
only when interaction or structure helps the user more than prose. A2UI is a view
of observed state, never an analysis substitute. Do not mention the protocol or
ask the user to supply component payloads.

Component shapes are NOT in this skill — the catalog itself is the allowlist and
the source of truth (`docs/design/a2ui-compat-campaign-2026-09.md` S2/S4). Load a
catalog's index with `load_skill("a2ui-catalog-<slug>")` (see "Skills available to
you" for the ids your session can currently produce) and one component's exact
schema with `load_skill("a2ui-catalog-<slug>", file="catalog.json#/components/<Name>")`
before producing it.

## Choosing a view

Match the surface to the shape of the evidence, not to what looks impressive:

- A spatial result (stations, sites, points on a map) → a map component.
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
- Ongoing/completed work, a warning, or a diff → the matching status/callout/diff
  component, never repurposing a generic text block for it.
- A durable export (an image, a report) the user did not ask to view inline → a
  registered artifact reference, not an inlined image.

## Composing surfaces

Prefer one small surface at the step it explains. Reuse a stable semantic
`surface_id` to update that view in place. Do not accumulate unrelated work into
one final tabbed dashboard. Tabs are appropriate only when several views of the
same result belong together and the available width justifies them.

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
- **Shared selection.** Define one point selection whose name matches the
  component's selection parameter, over the entity field. The component's
  selection binding then links the chart to the other views (see below).
- **Keep specs small.** The guard caps size, nesting and view count. No URLs
  anywhere.

Write a Python file that assigns the Altair chart to `chart`, resolve this
skill's directory as `SKILL_ROOT`, and export plus pre-check it:

```text
uv run --no-project --with "altair>=5" python "SKILL_ROOT/scripts/vega_spec.py" build "CHART.py" "SPEC.json"
```

The script writes the spec and names any rule it would obviously break;
`vega_spec.py check SPEC.json` re-checks an existing spec. The server's guard
is the final authority. Load the chart component's schema from the catalog
skill before putting the spec into a surface.

## Linking views on one surface

To let several views on the same surface follow one selection, bind each view's
selection to the same data-model path under `/selection/`, one path per concept
(e.g. the selected samples). A chart, table or map on that surface then
highlights whatever the others select, entirely in the client, with no agent
turn. Use this whenever views show the same entities from different angles.
Views on different surfaces don't share selection.

## Preserving a user's choice into the next turn

A click or selection inside a surface is local visual state only — it does not by
itself reach the agent. When the user must choose among options before analysis
continues, pair the selector with a submit action whose event delivers the
resolved selection as structured context; load the catalog's Button/action and
your chosen selector's schemas together so the wiring between them is correct on
the first try.

## Producing and verifying

Call `create_a2ui_surface` once per coherent revision (leave `catalog_id` empty
to use the session's negotiated catalog). Require `rendered=true` and
`state=ready` before saying the view is available. A refusal names what to load
next (`hint`) — load exactly that component's schema, correct the call, and
retry a bounded number of times; do not print the payload as chat text, silently
replace an interactive component with a static image, or claim success on a
refusal.
