# Addendum B to the delegation, fork and science log plan: study views

This addendum changes [delegation-fork-science-plan.md](delegation-fork-science-plan.md) and replaces the plugin-declared views built for FD34. The owner set the direction on 2026-10-27 (ledger FD36 to FD40).

A plugin can't know in advance what an open-ended study should show. A study of a new effect may end in a threshold, a scaling law or a dead end. So the **views of a study are dashboards the study's agent builds and keeps current**, not charts a plugin declares. Two things make that work:

1. **Views belong to the study.** A dashboard the agent makes can be attached to the study. A study can have several, and the Science log's Views lists them.
2. **Views read the study's data live.** A view's tables and charts can read the study's measurements, entries and plugin records directly, and they update as new entries arrive without the agent rebuilding them.

Everything else in the plan and in Addendum A stands. The rules, verification and done criteria apply unchanged.

## Rules

1. **The agent decides what a view shows.** CLIO and the plugins supply data and components, never a layout. Nothing chooses a chart for the agent.
2. **One owner per view.** A view is an A2UI surface of the session that made it, and only that session edits it. The study's root conversation keeps the study's overview views, and branches may attach drill-down views of their own.
3. **No new store.** The attachment is a science-log entry in the owner's event log, and the view is the owner's existing surface. Live data is read from the science log and the transcripts that already hold it.
4. **Live means no republish.** A view bound to a study source shows new data the next time the source changes, without the agent calling a tool.

## What already exists

- **Surfaces.** A surface is a fold of its session's transcript `a2ui` parts (`gact/a2ui_store.py`), keyed by `(session_id, surface_id)`. The producer tools write only the active session's surfaces. Any client can read another session's surfaces through `GET /v1/sessions/{sid}/a2ui/surfaces`.
- **Data by reference.** Charts, tables and maps take `dataUri: artifact://artifact_<id>` plus an optional `dataQuery`. The data is served by `POST /v1/artifacts/{id}/table-query` over `artifacts/table_query.py`. Artifact ids are immutable versions, and the client caches them forever (`IMMUTABLE_QUERY`), so they cannot be live.
- **Study data.** Measurement rows come from `science_log/measurements.py:rows` (one row per measured value, with `observation`, `branch`, `metric`, `value`, `unit` and `condition.<key>`). Entries come from `GET /v1/sessions/{sid}/science-log`. Each new entry is published as `science.entry` to every member of the study, and the client's `ScienceEntryMerger` merges it once per frame.
- **Plugin records.** A plugin tool result can carry `_meta["ai.iowarp.clio/ui_event"]` with `{view, key, group, rows, component?}`. CLIO stores it on the result part. Today gact-tui builds a fixed chart per `view` from the plugin catalog's spec plus the event rows. That chart is what this addendum removes.

## Design: views

- **The `view` entry.** The science log gains an entry type `view` with the fields `surface_id`, `title` and `purpose` (why the view exists and what to look for), linked to the question or hypothesis it serves.
  - It is written with `log_entry` by the session that owns the surface, so its branch is the owner.
  - The surface must exist in that session when the entry is written; otherwise the write fails with `view_surface_missing`.
  - The agent removes a view with `set_status(withdrawn)`. Revising the surface needs no new entry.
- **Reading views.** `GET /v1/sessions/{sid}/science-log` returns `views`: for every standing `view` entry of the study, its entry, owner session, branch and the owner's current surface record. They are ordered with the root's views first, then by branch.
- **The Views mode.** It lists the study's views as titled panels with their purpose and owner branch, and renders each surface as its owner's surface: remote actions go to the owner session.
  - It refreshes when an entry arrives, and polls the attached surfaces while any branch of the study is running.
  - It shows only when the study has at least one view.
- **Export.** The offline export captures each view's surface and the study sources it reads, so the views show the data as it stood at export time.

## Design: live study sources

- **Sources.** Three new data sources, valid wherever a `dataUri` is accepted:
  - `study://<study_id>/measurements`: the measurement rows, plus `observation_status`, `recorded_at` and `branch_title`.
  - `study://<study_id>/entries`: one row per entry, with id, type, branch, status, time and its summary text.
  - `study://<study_id>/records/<view>`: every plugin record row with that `view` name across the study's sessions, with `branch` and `session_id`.
- **Serving.** `POST /v1/studies/{study_id}/table-query` with `{source, ...the table-query request}` builds the source as an Arrow table on demand and runs the same filter, aggregate, downsample, sort and page pipeline as artifacts. A study id that the server does not know is `study_not_found`.
- **Validation.** `create_a2ui_surface` and the dashboard compiler check a study source the way they check an artifact:
  - the study exists and the field names exist in the source's current columns;
  - a source with no rows yet has no columns to check against, so field names are accepted and the tool result says so (`study_source_empty`) rather than refusing.
- **The catalog.** The `dataUri` pattern in clio-schemas accepts `study://<id>/<source>`. clio-agent pins the clio-schemas branch commit by git URL until clio-schemas releases. gact-tui accepts the scheme in `table-query-rows.ts`.
- **Live client.** A study-source query is not immutable. The client invalidates it when `ScienceEntryMerger` merges an entry of that study, and polls `records` sources while a branch is running.
- **The current law.** A chart over `study://…/measurements` can draw the study's current law: the newest standing hypothesis whose formula's left side is a measured metric and whose right side uses the rows' condition names. The rows gain `law_value` and `law` columns when such a hypothesis exists. This replaces the plugin-row shadow of FD34, with the same rules: the parser has no `eval`, and the view names the hypothesis in words.

## Design: plugins

- **What goes.** Plugins no longer declare charts. The Wordle `BoardWall` tally spec and the bench `Readings` spec and shadow layer are deleted, and so are the client's plugin-view surfaces (`plugin-view-model.ts`, `use-plugin-views.ts`, `PluginViewList`) and the Views canvas tab outside a study.
- **What stays.** Plugins keep sending records in `ui_event` (`{view, key, group, rows}`), which become the `records` source: Wordle's `board` (every game's tiles) and the bench's `readings`.
- **No tile-board component.** A surface uses one catalog, and a plugin catalog holds only its own components, so a Wordle board component could not sit in the same dashboard as the agent's charts. The agent draws boards with `clio.chart.v1` over `study://<id>/records/board`; the records carry turn, position, letter and state per tile.

## Design: guidance

- **`run-a-study`** gains a short section, informative and with no fixed layout:
  - views are where the scientist watches the study and checks its conclusion;
  - build them around the question, from the study sources;
  - revise them as the study changes course, and add one when it takes a new direction;
  - attach each with a `view` entry.
- **The `science-log.method` fragment** mentions views in one clause.

## Changes to the plan

- **Phase 4 (UI).** The Science log's Views lists the study's attached views; the plugin views it showed are gone.
- **FD34.** Superseded by FD39: the current law is drawn from study sources by any chart, not by a plugin layer.
- **F13 criterion.** "Screenshots read during the run show the board wall filling across branches" becomes "show the study's views updating across branches".
- **F15 criterion.** "Show the shadow drifting…" becomes "show the current law drawn against the readings in a study view, drifting while a law is wrong and settling once the merged law holds".

## Defaults

| Question | Default |
| --- | --- |
| Who edits a view | The session that made the surface; only it |
| Who attaches overview views | The study's root conversation; branches may attach drill-down views |
| Refresh | On every merged entry of the study; records sources poll every 4 s while shown |
| Empty source | Accepted, reported as `study_source_empty` |
| Export | Views and the sources they read, captured at export time |

## Phase 3c: study views

1. Add the `view` entry type (fields, validation, `view_surface_missing`) and return `views` from the science-log route.
2. Add the study sources, the `/v1/studies/{study_id}/table-query` route, and validation in the producers and the dashboard compiler.
3. Change the clio-schemas `dataUri` pattern on a clio-schemas branch, and pin it from clio-agent by commit.
4. In gact-tui:
   - feed Views from the attached views and render each as its owner's surface;
   - add study sources to `useTableQueryRows`, with invalidation and polling;
   - draw the current law from measurement rows;
   - capture views and sources in the export;
   - delete the plugin-view surfaces.
5. In the marketplace:
   - delete the plugin chart specs;
   - keep the records;
   - add the guidance.
6. Tests at every layer, then the scenarios below.

## Scenarios

Add to the science ledger:

| ID | Scenario | Prompt | Passes when |
| --- | --- | --- | --- |
| F26 | The root attaches two views, one over `study://…/measurements` and one over `study://…/entries`; a branch then logs a new Observation | Mechanism | Both views appear in Views with their titles and purpose; the measurement view shows the new row without the root calling a tool; a branch's attempt to write a `view` for a surface it does not own is `view_surface_missing`; withdrawing a view removes it |
| F27 | A view over the measurements of a law-shaped study, with the law changing | Mechanism | The chart draws `law_value` from the newest standing formula hypothesis, names it in words, and redraws when a hypothesis is added or refuted |
| F28 | Export a study with views | No prompt | Offline, every view renders with the data it showed at export, and no request leaves the page |
| F29 | The Wordle study, plain request, after this addendum | Judgement | Plus F13's criteria: the agent attaches at least one view of its own design built from the study sources, and revises or adds one as the study progresses |

## Done

The plan's done list gains:

1. F26 to F29 pass with the evidence the Verification section requires.
2. No plugin declares a chart, and the generic code (CLIO and gact-tui) has no plugin-specific view code; the domain grep covers it.
