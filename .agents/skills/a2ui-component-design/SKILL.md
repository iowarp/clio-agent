---
name: a2ui-component-design
description: clio-schemas is the canonical copy; this repository vendors the guidance. Guidance for designing or changing A2UI components and catalogs across clio-schemas, clio-agent, and gact-tui — the data-by-reference contract, one charting layer, shared selection, and progressive disclosure. Load before adding a component, a component property, or a catalog/validator/renderer change, and before writing an agent-facing catalog description or instructions.md section.
---

# Designing A2UI components (CLIO catalogs)

Distilled from issue #1533 (data by reference for every component, surfaces from files,
one-call progressive disclosure) — the friction a live probe found: a model that reaches for
A2UI correctly but then fights the catalog to get real data onto the screen. Every rule below
exists to remove one such fight, not to add ceremony.

## 1. Inline values OR a dataset reference — both, never inline-only

Like `plot([1, 2, 3])` versus `plot(dataset.x)`: every component that carries rows, points, a
diagram, source, or a diff must accept its content two ways — a small inline property (`data`,
`rows`, `points`, `source`, `code`, `diff`) **or** `dataUri` (a reference to a registered
artifact) — never both at once, never neither. A component that only takes inline values forces
an agent to copy a dataset into a tool call by hand, which is what produced the original bug (a
map split into 7 panels because the model manually chunked points past a hand-typed limit).

- A cap on the inline path (e.g. the map's 500 points, the chart's 10k rows) limits what can be
  **drawn** inline; it never limits what can be **referenced** through `dataUri`. A referenced
  dataset is bounded by `dataQuery`/`limit` and the viewer instead.
- The three tabular components (`clio.chart.v1`, `clio.map.v1`, `clio.data-table.v1`) additionally
  accept `dataQuery` — one shared shape (filter, aggregate, downsample, limit), `$ref`'d from a
  single `$defs/DataQuery`, never redefined per component. `dataQuery` is only ever valid alongside
  `dataUri`.
- A component that reads a dataset names its columns with `*Field` properties (`xField`,
  `latitudeField`, `entityField`, `labelField`, ...) instead of assuming a fixed row shape. Field
  names required by `dataUri` (e.g. the map's `latitudeField`/`longitudeField`/`labelField`) are
  optional when the component uses inline values instead — the cross-field rule is "required
  together with `dataUri`," not "always required."
- Encode "exactly one of X or dataUri" as a real cross-field rule in both the JSON Schema
  (`oneOf` with `required`/`not: {required: [...]}` branches) and the pydantic model
  (`model_validator(mode="after")`), so a wrong payload is a typed validation error, not a
  silently-empty view.

## 2. One charting layer

`clio.chart.v1` is the only chart component: a `preset` (a named, guarded Vega-Lite template) or
an agent-authored Vega-Lite `spec`, guarded (size, view count, no `url`/`usermeta`, `data` only as
the named source) so it can never fetch a URL or smuggle rows outside the component's own
`data`/`dataUri`. Do not add a second, narrower chart component for a new plot type — extend the
preset library or let the agent author a `spec` instead. A bespoke chart component duplicates the
data-by-reference and selection work this skill exists to keep in one place.

## 3. Linking views: shared selection

Components that show the same rows from different angles should share selection as a normal
renderer behavior. Views using one `dataUri` link through the dataset's stable `__row` key.
Inline chart, map and table views also link when they contain the same unique entity values,
regardless of row order. The person does not need to ask for this, and the producer does not add
a selection control or path for ordinary same-dataset views. Preserve a stable entity key when
transforming rows for another view. Only use an explicit `/selection/<key>` path and matching
`selectionField` values when different datasets share a conceptual entity. The value at that
path is `SelectionState` (`{field, values[], source?}`, one shared `$defs/SelectionState`).
Do not invent a second selection mechanism.

## 4. Document in the catalog, not beside it

An agent reads a component's contract two ways: the generated one-line catalog index (a
`description` plus its property signature) and, on a deeper look, the component's schema and
`instructions.md`. That means:

- Every component schema carries a one-sentence `description` (what it is for, not how it's
  implemented) — e.g. `clio.map.v1`: "Points on an interactive map (stations, sites,
  epicenters)." Every data-input property (`dataUri`, `*Field`, `dataQuery`, ...) carries a short
  `description` too. Keep both short and exact: the generated index is built from them verbatim.
- `instructions.md` states each cross-cutting rule once (inline-or-dataset, shared selection,
  routing actions) rather than repeating it in every component's example — repetition there is
  what a `load_skill` budget pays for on every dashboard.
- Never restate a property's shape in prose beyond what the schema already enforces; prose drifts,
  JSON Schema does not.

## 5. Catalog, validator, and renderer change together

A new property or component lands as one change across: the JSON Schema (`catalog_bounded.py` for
anything with bounds or cross-field rules, the generic factory for a plain scalar shape), the
pydantic model that mirrors it, and the renderer that draws it (gact-tui). Landing only the schema
half of a change ships a contract nothing enforces or draws. Where a rule needs the actual dataset
to check (a `*Field` column name against a referenced table's real columns), that check belongs in
the producer/validator boundary that has the data — it must reject a wrong column name as a typed
error at validation time, not let the scientist discover it as a blank chart. This repo's JSON
Schema can only validate a name's *shape* (a non-empty string, a length bound); it cannot see the
dataset, so it is not where that existence check runs.

## 6. Progressive disclosure: one or two lookups per surface

An agent should be able to build most surfaces from a generated index alone (one line per
component: description + signature) plus, at most, one `load_skill` call for the one component it
needs more detail on. Keep signatures in the index short enough that this stays true — this is why
descriptions and property lists stay terse (rule 4). Whatever mechanism returns a component's
schema to an agent should keep the `$defs` that schema references resolvable inline in the same
returned file, rather than costing the agent a further round trip per referenced `$def`.

## 7. Surfaces keep their definition

A surface's component list should be authorable either as an inline array or from a file (e.g. a
`components_path`-style parameter naming a file the agent wrote); whichever way it was authored,
the final component JSON that was actually sent should be stored as a registered artifact attached
to the surface. A surface's definition should always be recoverable after the fact, the same way a
component's data is always a real reference, never a value that only existed inside one tool call.

## 8. Acceptance is a live run, not a schema review

A catalog/validator/renderer change is not done when it type-checks; it is done when a live agent
run of a real prompt (e.g. "chart these 500 rows and show the matching stations on a map") on both
a strong and a weak model produces the expected surface with no data loss (no manual chunking, no
silently dropped rows, no dataset the agent gave up referencing) and no more than one or two
`load_skill`/lookup round trips. Measure tool calls, lookup count, and rows lost between the
dataset and what actually rendered — not just "no validation errors."
