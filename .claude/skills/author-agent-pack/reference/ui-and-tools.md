# UI and tools for packs

## UI: generic only
- **No UI specific to one instance or source.** `earthscope-stations` is the example to avoid. The catalog should gain **L0 components** that any domain can use.
  - Example: `clio.mesh-viewport.v1` was built for FEA/Factorio, and it renders any `.glb` mesh with fields, including 3D plant surfaces.
- **What packs can do** (sidecar `catalog.clio.json`, `clio_schemas/a2ui/sidecar.py`):
  - alias an existing kernel component (`implements: {kernel: …}`);
  - set **string-only** `presets` for props left unset;
  - declare `events` (destination `agent` / `permission` / `run`, `context_schema`, narration).
- **What packs can't do:**
  - ship renderer code: every kernel must already be in a builtin catalog (`gact/a2ui_catalogs/blueprint.py` rejects others);
  - set non-string presets;
  - change behaviour.
- **So new visual capability is core work.** The recipe, using the mesh-viewport change as the worked example:
  1. **clio-schemas:** `a2ui/v0_9_1/components.py` `_component_model`, `bounded_components.py`, `catalog_render.py` markers, `instructions.py` + `instructions.md`, `catalog.json` / `catalog.clio.json`, `HASHES.json`, version bump, corpus tests.
  2. **clio-agent:** pin bump only.
  3. **gact-tui:** `web/src/components/clio/a2ui-<x>-catalog.ts` (zod mirror + `React.lazy`), the renderer, a `KERNEL_COMPONENT_LIST` entry in `web/src/lib/a2ui/kernel-catalog.tsx`, `query-keys.ts`, fixtures resync.

## Linking views without an agent round trip
- **One data model per surface.**
  - A prop typed DynamicValue/DynamicString accepts a `{path}` binding.
  - web_core's generic binder creates `set<Prop>`, which writes `dataContext.set(path, v)`.
  - Other components bound to the same path re-render locally.
  - The server only sees the data model when an action is sent or `sendDataModel` is set.
- **Precedent:** mesh-viewport's `camera` binding (a debounced write, and it follows external writes).
- **Selection convention** (planned with `clio.chart.v1`): the path `/selection/<key>` = `{field, values[], source?}`. Charts, tables, maps and viewports write and read it. Use `source` to avoid feedback loops.
- **Across surfaces:** there's no shared data model, so use a module-level store like `mesh-viewport-sync.ts` `syncGroup` (it shares camera, bounds and colour range, view state only).
- **Gaps at the time of writing:**
  - `selectData` is a no-op;
  - `clio.data-table.v1` `selection` and `clio.map.v1` `selected` are static and don't write back. The `feat/chart-kernel` work addresses this.

## Charts
- **`clio.time-series.v1`** (recharts): at most 5 `yKeys` (wide format), at most 10k inline rows, and artifact-backed previews sample about 1-2k rows from CSV with at most 6 columns. **Unsuitable for hundreds of entity trajectories.**
- **Planned replacement: `clio.chart.v1`**, a **Vega-Lite** spec in the style of marimo's `mo.ui.altair_chart`:
  - a spec guard: data only via `{name:"source"}`, no URLs, size and view caps, no `eval`;
  - core preset templates (`trajectories`, `heatmap`, `spectra`, `boxplot`, `scatter`), each with a highlight-the-selected-entity param;
  - data from `POST /v1/artifacts/{id}/table-query` (filter, aggregate, per-entity LTTB downsampling).
- **Packs refer to presets by name** through string presets. Don't write huge specs in prompts.

## Tools and MCPs
- **MCPs expose generic verbs**, never format semantics. A format-specific MCP freezes the agent to one version of one source.
- **Before writing a new tool,** check the clio-kit servers (`clio-kit mcp-server` lists them: pandas, parquet, plot, hdf5, geo, ndp, …) and whether a **skill-bundled script** would do. Scripts are deterministic, editable and versioned, like the PDF/Office skills.
- **Declare clio-kit servers the way current packs do** (`clio-kit mcp-server <name>`, with `probe_timeout_retries`). The installed clio-kit version is set by the installer/doctor, not the pack.
- **Shell is often the most flexible path.** When unsure, give the agent both routes (shell + uv scripts, DuckDB) and measure on held-out instances before building anything new.
