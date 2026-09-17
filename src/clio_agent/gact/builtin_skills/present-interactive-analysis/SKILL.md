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
- A quantity that changes over an index or time → an interactive time series,
  preferring a registered artifact reference over inlined rows for anything
  non-trivial in size.
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
