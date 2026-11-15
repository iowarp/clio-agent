# KiCad in CLIO: implementation plan

This plan makes KiCad a connector in CLIO, built the way the ParaView connector is built. CLIO's agent designs the board through the tools of Jaime's fork of `kicad-mcp-pro`. The fork gains MCP Apps views for the board, the schematic, the BOM, the checks and the stackup. Those views show the design in a tab, take the person's operations and comments, and send them back to the agent. KiCad runs where the server runs, with or without a display. A plugin that follows the connector carries the skills for the whole pipeline, from parts to a board house package, and records placement passes as a study.

The plan has nine workstreams. They are not ordered. The work is one goal that runs until every item under Done holds, and the human reviews the finished branches.

1. **The connector and its plugin.** A `kicad` entry with a logo and optional sourcing keys, and a `kicad` plugin that follows it.
2. **The KiCad instance.** The server attaches to a running KiCad or starts one itself, on any machine, with or without a display.
3. **MCP Apps in the fork.** The fork serves five views and the tools that open and feed them.
4. **The views.** Board, schematic, BOM, checks, stackup and parts, each with the operations the person can do from it.
5. **The workbench.** One A2UI surface links the views by selection, and a dashboard follows the placement passes.
6. **Feedback to the agent.** What the person selects reaches the agent, and a mark drawn on a view arrives with the parts and nets under it.
7. **The pipeline skills and the placement study.**
8. **Tool changes in the fork.** The tools the views and the pipeline need that the fork lacks, and the removal of its duplicate simulator.
9. **Qualification.**

## Contract

This plan is the contract for the nine workstreams. When code and this plan disagree, change the code. If the plan turns out wrong, choose the fix that best keeps its intent, update this document in the same change, and record the decision in the ledger.

| Document | Governs |
| --- | --- |
| This plan | The KiCad connector, the instance, the fork's views and tools, the workbench, app marks, the plugin and its skills |
| `docs/design/sdk/kicad-ledger.md` in clio-agent (new) | This plan's decisions and acceptance evidence |
| `plans/mcp-apps-surfaces-plan.md` (project) | Connector entries (CN1 to CN12), tabs, app context (MA8), app tools (MA11 to MA14), open-link (MA15), reopening (MA18, MA19), the app component and its channels (MA20 to MA22). This plan uses them and changes none of them |
| `plans/comments-and-visual-feedback-plan.md` and its addendum A (project) | The comment panel and region capture. This plan adds only the case of a mark that lands on an app (KF2) |
| `plans/remote-connectors-plan.md` (project) | Secrets storage and the long list of services |
| MCP Apps specification 2026-01-26 and the draft in `modelcontextprotocol/ext-apps` | The wire protocol. The draft governs app tools |
| KiCad IPC API documentation (dev-docs.kicad.org) | What KiCad 9, 10 and 11 can do over IPC |

**Base.** `JaimeCernuda/kicad-mcp-pro` `main` at `d679d9ee` (release 4.16.0). clio-agent-marketplace `develop` at `cea9142`. clio-agent and gact-tui at the heads of the branches that carry the work of `plans/mcp-apps-surfaces-plan.md`. The session writes their names and commits in the ledger header before it starts.

Decisions use the prefix `KC` for the connector, `KI` for the instance, `KV` for the views, `KW` for the workbench, `KF` for feedback, `KP` for the plugin and `KT` for tool changes. Qualification scenarios use `KQ`.

## Rules

The megachange rules and the seven rules of the MCP Apps plan carry over. Four more apply here.

1. **The design work is the agent's.** CLIO's own agent places, routes and checks through the fork's tools. No service whose own AI does the layout is used (Quilter, DeepPCB, Flux).
2. **KiCad lives in the fork.** No KiCad name, file format or tool name appears in Python or TypeScript in clio-agent or gact-tui. CLIO knows a connector entry, a plugin and a surface definition.
3. **The view has no edit logic.** Every operation a view offers is a call to one of the server's tools, committed by the live KiCad instance. If an operation has no tool, the fork gets the tool.
4. **One simulator.** Circuit simulation is the spice connector's. The fork's own simulation tools are deleted.

## Out of scope

Do not implement, stub or reserve fields for these:

- KiCad on another machine, reached over SSH or a scheduler. That is clio-relay's. Here the server and its KiCad run on the machine CLIO starts the server on, which can be a node, a container or a cloud session.
- Interactive push-and-shove routing and interactive length tuning in a view. KiCad offers them only in its own window.
- Live schematic editing over IPC. KiCad 10's IPC API has no schematic commands, so schematic edits stay on the fork's guarded file path.
- A PCB viewer package in gact-tui. The views arrive inside the app.
- A separate Nexar, DigiKey or Mouser connector.
- Changes to the Fusion, spice or slicer connectors.
- Panelization, impedance and SI views. Their tools stay available to the agent without a view.

## Current state

Read at the base.

**The fork.**

- Python 3.13, `mcp` 2.2.0. `KiCadFastMCP` in `src/kicad_mcp/server.py` raises `ValueError` for any protocol extension passed to it ("MCP protocol extensions are not enabled in KiCad MCP Pro").
- `mcp` 2.2.0 ships the MCP Apps extension as `mcp.server.apps.Apps`. A server opts in with `extensions=[apps]`, binds a tool with `@apps.tool(resource_uri=...)`, can mark a tool visible to the app only (`_meta.ui.visibility`), and branches with `client_supports_apps(ctx)`.
- The local Python server registers no `ui://` resource. The only MCP App in the repository is in the hosted service, `apps/cloud/src/project-review-app.ts`, on `@modelcontextprotocol/ext-apps` 2.0.4. It shows a project's name and connection state and refreshes through `callServerTool`.
- Profiles include `default` (25 read-only tools, per the README), `build`, `release`, `expert` and `full`. Operating modes are `readonly`, `write`, `manufacturing` and `experimental`. In `operating_modes.py`, `write` allows no manufacturing tool and `manufacturing` allows no write tool. Only `experimental` allows both.
- `adapter_matrix.py` routes each tool to a backend: `kicad-gui-ipc`, `kicad-11-headless-ipc`, `kicad-cli`, `guarded-schematic-file`, `transactional-pcb-file`, `freerouting`, `ngspice` and others. `ipc/capabilities.py` detects a reachable KiCad and whether `kicad-cli api-server` is available. Nothing in the fork starts KiCad.
- Tools the views use and that exist: `pcb_get_board_as_string`, `pcb_get_selection`, `pcb_highlight_net`, `pcb_move_footprint` (position and rotation), `pcb_refill_zones`, `pcb_get_ratsnest`, `pcb_get_stackup`, `pcb_set_stackup`, `pcb_stackup_consistency_gate`, `run_drc`, `run_erc`, `sch_set_dnp`, `export_glb`, `export_step`, `export_dxf`, `export_spice_netlist`, `lib_get_datasheet_url`, `lib_find_alternative_parts`, `lib_bind_part_to_symbol`, `lib_get_bom_with_pricing`, `pcb_auto_place_by_schematic`, `pcb_auto_place_force_directed`, `route_autoroute_freerouting`.
- Missing: no tool flips a footprint to the other side or locks it. `pcb_set_board_outline` draws a rectangle only, and nothing imports an outline.
- Sourcing defaults to `jlcsearch`, which needs no key. Nexar, DigiKey and Mouser are read from `NEXAR_CLIENT_ID`, `NEXAR_CLIENT_SECRET`, `DIGIKEY_CLIENT_ID`, `DIGIKEY_CLIENT_SECRET` and `MOUSER_API_KEY`.
- FreeRouting runs from a JAR (`freerouting_jar`) or the image `ghcr.io/freerouting/freerouting:2.1.0`, with a 900 second default timeout.
- Simulation tools `sim_*` in `tools/simulation.py` run ngspice on the KiCad schematic.
- Skills in `skills/`: `pcb-design`, `drc-check`, `fabrication-output`, `schematic-review`, `kicad-design-review`, `wired-subcircuit-design`, `visual-excellence`.
- Fixtures in `packages/kicad-fixtures/fixtures`, including `clean-led-kicad10`, `large-board`, `drc-courtyard-error` and `multi-sheet-schematic`.

**KiCad.** In KiCad 9 and 10 the IPC API talks only to a running KiCad GUI and cannot plot or export. KiCad 11 adds a headless server, `kicad-cli api-server`, and export over IPC. A KiCad 9 or 10 GUI runs without a screen on a virtual display.

**ecad-viewer** (`Huaqiu-Electronics/ecad-viewer`, MIT), an extension of KiCanvas. It selects a net on double click and greys the rest, shows a BOM from the schematic or the board, and renders 3D with three.js from models kicad-cli generates. It is a viewer. Its README promises the latest KiCad formats and names no version.

**The marketplace.** A plugin is `plugins/<name>/` with `plugin.json`, `mcp.json`, `skills/` and an `ai.iowarp.clio/` folder. `extensions.ai.iowarp.clio.requires` pulls in other plugins. `documents`, `visuals`, `science-log` and `manufacturing-processes` exist.

## Design: the connector and its plugin

**KC1. The entry.**

```yaml
- id: kicad
  name: KiCad
  logo: kicad.svg
  description: Design circuit boards in KiCad and see the board, schematic, BOM and checks in a tab
  server:
    command: uvx
    args: [--from, "git+https://github.com/JaimeCernuda/kicad-mcp-pro@<pinned commit>", kicad-mcp-pro, --transport, stdio]
    env:
      KICAD_MCP_PROFILE: full
      KICAD_MCP_OPERATING_MODE: experimental
      KICAD_CLI_PATH: ${need.kicad_cli}
      NEXAR_CLIENT_ID: ${need.nexar_id}
      NEXAR_CLIENT_SECRET: ${need.nexar_secret}
      DIGIKEY_CLIENT_ID: ${need.digikey_id}
      DIGIKEY_CLIENT_SECRET: ${need.digikey_secret}
      MOUSER_API_KEY: ${need.mouser_key}
  needs:
    - {key: kicad_cli, kind: value, label: Where KiCad is, required: false, agent_settable: true,
       help: Leave empty to find KiCad on this machine. Otherwise the path to kicad-cli.}
    - {key: nexar_id, kind: value, label: Nexar client ID, required: false}
    - {key: nexar_secret, kind: secret, label: Nexar client secret, required: false}
    - {key: digikey_id, kind: value, label: DigiKey client ID, required: false}
    - {key: digikey_secret, kind: secret, label: DigiKey client secret, required: false}
    - {key: mouser_key, kind: secret, label: Mouser API key, required: false}
  plugin: kicad
```

- An empty optional need leaves its variable unset, so the fork falls back to `jlcsearch`.
- `full` with `experimental` gives the agent editing and the release package in one session. CLIO's session permission rules are what governs the agent, not the server's mode.
- This replaces the line in `plans/mcp-apps-and-connectors-checkpoint.md` that made KiCad a marketplace plugin and not a connector. That line held because the server sent no view. It now sends five.

**KC2. The logo** is KiCad's own mark from its brand assets. The ledger records the source and terms (CN11).

**KC3. No KiCad, no failure at enabling.** As with ParaView (AQ35), the connector is ready and lists its tools on a machine without KiCad. The first tool that needs KiCad says it was not found, where to install it, and that `kicad_cli` can point at an install the server did not find.

**KC5. Asking for keys.** *Added (ledger KA7).* On enable, the offer and setup cards show the optional needs, grouped as "Part catalogs", each with what it adds. At run time, when a tool's result says a key would help (KT1b), the agent calls `offer_plugin(kicad, reason, needs=[...])`, which raises the setup card with only those rows on a connector that is already on. A secret goes to the credential store (CN8), the server restarts (CN7) and the agent calls again. Not now is a typed refusal, and the agent goes on with what it has. Not MCP elicitation: the specification forbids form-mode elicitation for secrets, and URL mode would keep the keys outside CLIO's credential store.

**KC4. The plugin.** `plugins/kicad` in clio-agent-marketplace:

- `plugin.json` with `extensions.ai.iowarp.clio`: `follows: kicad`, `requires: [documents, science-log]`.
- `mcp.json` binds the server name `kicad` to the connector's connection.
- `skills/` holds the skills in KP1 and the fork's skills, copied from the fork at the pinned commit. A test in the plugin checks the copies match the pin.
- `ai.iowarp.clio/surfaces/pcb-workbench.json` holds the workbench surface (KW1).

## Design: the KiCad instance

**KI1. The server owns its KiCad.** Starting, finding and stopping KiCad is in the fork, in a new `kicad_mcp/instance/` module, so any MCP host gets the same behaviour. CLIO starts the server as it starts any connector's command and knows nothing of KiCad.

**KI2. Attach or start.** On the first tool that needs IPC, and again after `kicad_set_project` names another project, the server takes the first of these that works and records which:

| Case | What the server does |
| --- | --- |
| A KiCad is running with its API server on and this board open | Attaches to it. The person's own window shows every edit |
| KiCad 11 or later is installed | Starts `kicad-cli api-server` for the project |
| KiCad 9 or 10 on Linux with no display | Starts `pcbnew <board>` under `xvfb-run` |
| KiCad 9 or 10 with a display (Linux, macOS, Windows) | Starts `pcbnew <board>`. Its window appears, and the person can use it or leave it |

**KI3. The started instance has its own settings.** A KiCad the server starts runs with `KICAD_CONFIG_HOME` set to a folder of the server's own, with the API server enabled in it. The person's KiCad settings are never written. The ledger records the settings key and the socket path the instance reports.

**KI4. Lifetime.** A started instance lives as long as the server. It is not stopped on idle, because a view on it may be open (MCP Apps rule 6). When the server exits, it stops the instances it started and never one it attached to.

**KI5. `kicad_session_status`.** A read-only tool, in every profile: the case taken from KI2, KiCad's version, the instance's process, the project and the backend that served the last edit. Every failure to start or attach says what is missing (KiCad, `xvfb-run`, a display, a board file) and what to do. None is a stack trace.

**KI6. The file path is the fallback, not the default.** The fork's `transactional-pcb-file` backend is used only when no instance can be started. The board view then shows a line saying edits are going to the file and why.

**KI7. A task on a node.** Nothing here assumes the person's desktop. When CLIO runs on a node, in a container or in a cloud session, the server starts KiCad there under KI2, and the tab shows it through CLIO as any app does.

## Design: MCP Apps in the fork

**KV1. The Apps extension.** `KiCadFastMCP` accepts `mcp.server.apps.Apps` and still refuses every other extension. View tools branch on `client_supports_apps(ctx)` and return a useful text result to a host without apps.

**KV2. One bundle, six views.** A new package in the fork, `packages/kicad-views`, builds one self-contained HTML file with Vite: TypeScript, `@modelcontextprotocol/ext-apps` at the version `apps/cloud` uses, and ecad-viewer vendored at a pinned commit. The server registers it five times, each with its view name:

`ui://kicad/board`, `ui://kicad/schematic`, `ui://kicad/bom`, `ui://kicad/checks`, `ui://kicad/stackup`, `ui://kicad/parts` (*Amended*, ledger KA4).

Each resource declares an empty CSP. The page loads nothing from the network.

**KV3. ecad-viewer is the one library.** It draws the board, the schematic, the BOM table and the 3D board. Its option that converts files through a kicad-cli server is not used. The 3D model comes from the server (KV5). KiCad 10 files are a work item. The vendored copy is tested against the fork's KiCad 10 fixtures and every board the qualification produces, parser fixes are made in the vendored copy, and each fix is offered upstream.

**KV4. The opening tools.** Read-only, in every profile, each bound to its view:

| Tool | Opens | Arguments |
| --- | --- | --- |
| `view_board` | `ui://kicad/board` | `project`, optional `layers`, optional `focus` (a reference, a net or a point) |
| `view_schematic` | `ui://kicad/schematic` | `project`, optional `sheet`, optional `focus` |
| `view_bom` | `ui://kicad/bom` | `project`, optional `quantity` for pricing |
| `view_checks` | `ui://kicad/checks` | `project`, `kind` (`drc`, `erc` or both) |
| `view_stackup` | `ui://kicad/stackup` | `project` |
| `view_parts` | `ui://kicad/parts` | `query` or `category`, optional `filters`, optional `candidates` (the agent's shortlist). *Amended*, ledger KA4 |

The text result tells the model what was opened and a short summary: part count, net count, layer count, unrouted connections, violations. The view asks for `fullscreen` on open, except the checks view, which stays inline until the person moves it.

**KV5. Data reaches the view through app-only tools.** These tools are visible to the app and not to the model, so the board file never enters model text (MCP Apps rule 5):

- `view_data_board`: the current board text from the live instance (`pcb_get_board_as_string`), and its revision.
- `view_data_schematic`: the schematic files of the project, and their revision.
- `view_data_glb`: the board's GLB from `export_glb`, for the 3D toggle.
- `view_data_bom`: the BOM rows with sourcing when keys or `jlcsearch` allow.
- `view_data_checks`: the last DRC and ERC results with positions.
- `view_data_parts`: the parts view's rows with their `specs` (*Amended*, ledger KA4).

**KV6. Revisions, not reloads.** The fork keeps a revision counter per project. Every committed edit raises it, whoever made it: the agent, a view or the person in KiCad's own window. A view waits on the app-only tool `view_wait_revision(since)`, which returns when the revision passes `since`. The view then reads again. Nothing reads the board from disk while an instance is live.

**KV7. Reported context.** Each view reports through `ui/update-model-context`, at most ten times a second per key (MA22):

- `selection`: the kind (`footprint`, `pad`, `track`, `net`, `symbol`, `bom_row`, `violation`), the reference, the net, the layer, and the position in millimetres.
- `layers`: the visible layers (board view).
- `revision`: the revision the view shows.

Per MA8, the agent's next turn carries it.

**KV8. App tools for the agent.** Registered by the views under the draft's app tools (MA11 to MA14), so the agent can steer what the person sees:

| App tool | View | What it does |
| --- | --- | --- |
| `focus` | board, schematic | Centres and highlights a reference, a net or a point |
| `set_layers` | board | Sets the visible layers |
| `select_row` | bom, parts | Selects the rows for a set of references or parts |
| `capture` | every view | Returns a PNG of what the view shows now |
| `describe_region` | board, schematic | See KF2 |

**KV9. Reopening.** The opening call carries the project and the view's arguments, so a replay (MA19) restores the view from the live instance. Zoom is not restored.

## Design: the views

Each operation below is one call through `callServerTool` to the tool named. The view holds no edit logic (rule 3).

**KV10. Board.** Layer toggles, net highlight, selection, 2D and 3D.

| Operation | Tool |
| --- | --- |
| Drag a footprint | `pcb_move_footprint` |
| Rotate | `pcb_move_footprint` with `rotation_deg` |
| Move to the other side | `pcb_set_footprint_side` (new, KT1) |
| Lock or unlock | `pcb_set_footprint_locked` (new, KT1) |
| Highlight a net in KiCad's own window too | `pcb_highlight_net` |
| Refill zones | `pcb_refill_zones` |
| Run DRC | `run_drc` |

A drag sends one call when it ends, never one per pointer move. A locked footprint does not drag. A call the server refuses leaves the footprint where the server says it is.

**KV11. Schematic.** Sheets, symbol selection, cross-probing with the board through the workbench (KW1). Operation: mark not fitted, through `sch_set_dnp`.

**KV12. BOM.** Columns: references, value, footprint, manufacturer part number, stock, price at the chosen quantity, lifecycle, datasheet, and the key specs from `specs` (*Amended*, ledger KA4).

| Operation | Tool |
| --- | --- |
| Show alternates for a row | `lib_find_alternative_parts` |
| Choose an alternate | `lib_bind_part_to_symbol` |
| Mark not fitted | `sch_set_dnp` |
| Reprice at a quantity | `lib_get_bom_with_pricing` |
| Open the datasheet | `ui/open-link` into the in-app browser (MA15) |

**KV13. Checks.** DRC and ERC violations grouped by rule, with severity and position. Operation: run again, through `run_drc` or `run_erc`. Selecting a violation reports it as the selection, and the workbench focuses the board on it.

**KV14. Stackup.** Layer order, materials, thicknesses and the net classes' widths and clearances. Operations: edit through `pcb_set_stackup` and the net class tools, check through `pcb_stackup_consistency_gate`.

**KV15. Parts.** *Added (ledger KA4).* A spec table with filters, column choice and side-by-side compare. It reports the selected part and the compared parts as `selection` (KV7).

| Operation | Tool |
| --- | --- |
| Search | `lib_search_parametric` |
| Compare | `lib_compare_parts` |
| Pick for the chosen symbol | `lib_bind_part_to_symbol` |

## Design: the workbench

**KW1. One surface, linked by selection.** `pcb-workbench.json` in the plugin is an A2UI surface with three `clio.mcp-app.v1` components: the board view, the BOM view and the checks view. The schematic opens in its own tab. Their channels (MA20, MA21):

| Data model path | Reported by | Set through |
| --- | --- | --- |
| `/pcb/selection` | board, BOM, checks, schematic, parts | board `focus`, BOM `select_row`, schematic `focus` |
| `/pcb/revision` | board | report-only |

Selecting a BOM row highlights its footprints on the board. Selecting a violation focuses the board on it. Selecting a footprint selects its BOM row. The skill tells the agent to open the workbench when a design session starts.

**KW2. The placement dashboard.** The placement study (KP2) writes its pass metrics to a live artifact. The workbench has a chart bound to it, the existing Vega-Lite component, showing for each pass the unrouted connections, the ratsnest length, the DRC count and, after routing, vias and track length. This is CLIO's existing dashboard semantics. Nothing here is new to CLIO.

## Design: feedback to the agent

**KF1. Selection is context.** "Move this closer to J1" works because the selection the board view reported (KV7) is in the turn. No comment is needed for that.

**KF2. A mark on an app.** This is the one change in clio-agent and gact-tui, and it names no domain.

- Addendum A's region capture draws marks over any surface, and an app's frame is opaque to the host (comments plan rule 5). So for a mark that lands on an app, the host asks the app.
- If the app registered an app tool named `describe_region`, the host calls it once per mark with the mark's rectangle in the view's CSS pixels. The tool returns a PNG of that region drawn by the view and a JSON list of what lies under it.
- The host puts both in the mark's context, the same way chart rows and mesh nodes go in for built-in components today.
- An app without the tool gets a mark with its rectangle and a note that the app could not describe it.
- The developer guide documents `describe_region` as CLIO's convention for apps. Other hosts ignore the tool.

The board view's `describe_region` returns the footprints, pads, tracks and nets under the region, with layer and millimetre coordinates. The schematic view's returns symbols, pins and nets.

**KF3. Built on what is there.** KF2 is built on the region capture on the base branches. It does not change the comments plan or its addendum.

## Design: the plugin's skills and the placement study

**KP1. The skills.** Generic KiCad method goes into the fork's `skills/`, where any agent can use it. What is about CLIO goes into the plugin.

| Skill | Lives in | What it covers |
| --- | --- | --- |
| `pcb-placement` | the fork | Fix connectors, holes and keepouts from the enclosure first. Group by function from the schematic hierarchy. Place ICs, then decoupling as close to the pins as the datasheet asks, then the rest. Use the auto-place tools only as a seed. Look at a capture and the ratsnest before calling a pass better |
| `pcb-routing` | the fork | Route power widths, differential pairs and decoupling loops with the explicit routing tools. FreeRouting for the rest. Refill zones. Run DRC until it is clean |
| `datasheet-pin-check` | the fork | Before layout, compare each symbol's and footprint's pin map with the datasheet's pin table, and stop on a mismatch. *Amended (KA3):* a value the design needs that the distributor specs lack (thermal resistance, absolute maximum ratings, a dropout curve) comes from the datasheet ingested into clio-core, with the page recorded. A part with no KiCad symbol gets one from `lib_generate_symbol_from_pintable` and the extracted pin table |
| `pcb-pipeline` | the plugin | The whole path and which connector does each step. Parts from the fork's sourcing. Datasheets fetched from `lib_get_datasheet_url` and ingested into clio-core through `documents`. Schematic and ERC. Simulation of power stages and filters through `export_spice_netlist` and the spice connector. The board outline from the enclosure: Fusion exports a DXF, `pcb_import_outline_dxf` takes it, `pcb_add_mounting_holes` places the holes. Placement as a study. Routing. DFM and the release package. The STEP back to Fusion for a fit check. The shell to the slicer. When to open the workbench |

The fork's `pcb-design` skill is replaced by `pcb-placement` and `pcb-routing`, which cover its placement and routing sections. Its other sections move into them.

**KP2. Placement passes are a study.** Each pass is a science log entry with values measured from tools: unrouted connections and ratsnest length (`pcb_get_ratsnest`), DRC count (`run_drc`), courtyard overlaps, and after routing, completion, vias and track length. The agent compares passes with its own code, as the science log intends, and writes the metrics to the live artifact KW2 reads. A pass that the agent calls better cites the capture it looked at.

## Design: tool changes in the fork

**KT1. New tools.**

| Tool | What it does | Backend |
| --- | --- | --- |
| `pcb_set_footprint_side` | Moves a footprint to the front or the back | IPC |
| `pcb_set_footprint_locked` | Locks or unlocks a footprint | IPC |
| `pcb_set_board_outline_shape` | Draws an outline on Edge.Cuts from points and arcs | IPC |
| `pcb_import_outline_dxf` | Reads an outline and hole positions from a DXF and draws them | IPC |
| `lib_get_component_specs` | One part's full spec sheet, merged across the configured sources, with the source on each value. *Amended*, KA2 | sourcing |
| `lib_search_parametric` | `category`, `filters` (`{name, op, value, unit}`), `package`, `min_stock`, `sources`. Maps to jlcsearch's category endpoints, and to Nexar's or DigiKey's filters when their keys are set. *Amended*, KA2 | sourcing |
| `lib_compare_parts` | Parts side by side on the attributes given. *Amended*, KA2 | sourcing |
| `kicad_session_status` | KI5 | none |
| `view_*`, `view_data_*`, `view_wait_revision` | KV4 to KV6 | IPC, then CLI for exports |

**KT1a. Part specs.** *Added (ledger KA2).* `ComponentRecord` gains `specs`: every attribute the source returns, each with its name, value as shown, number, unit and source. Units are normalized, so 100nF and 0.1µF compare equal. This covers Nexar `specs`, DigiKey `Parameters`, Mouser's attributes, and the typed columns of jlcsearch's category endpoints (`/<category>/list.json`). `lib_check_derating` reads ratings from `specs` instead of the description.

**KT1b. The sourcing hint.** *Added (ledger KA7).* When the primary search fails or hits a limit and no other source is configured, the result says why and names the sources that could be set up by the variables they read. Every sourcing result names the sources that answered. The fork asks nobody for keys; the host does (KC5).

Each new tool gets its entry in the capability registry, the adapter matrix, the operating-mode tables, the parity matrix and the generated tools reference, as the fork's own contributing rules require.

**KT2. The simulator is not offered to CLIO's agent.** *Amended (ledger KA6): the fork's simulation code stays as upstream has it, to keep the fork's diff small; the entry hides the `sim_*` tools from CLIO. The original text follows for the record.* The simulator is deleted: `tools/simulation.py`, the `sim_*` tools, their router entries, their parity rows, their prompts and their tests are deleted. The `ngspice` backend goes with them if nothing else uses it. `export_spice_netlist` stays. The parity matrix is regenerated.

## Defaults for the open questions

Take these and record each in the ledger.

| Question | Default |
| --- | --- |
| How CLIO gets the fork | `uvx` from the fork at a pinned commit |
| Profile and mode in the entry | `full`, `experimental` |
| Sourcing with no keys | `jlcsearch` |
| Attach order | Running KiCad, then KiCad 11 headless, then Xvfb on Linux, then a window |
| A started instance's settings | Its own `KICAD_CONFIG_HOME` |
| Idle stop of a started instance | None. It stops with the server |
| View library | ecad-viewer, vendored at a pinned commit |
| 3D | Inside the board view, from `export_glb` |
| Revision wait | Long poll on `view_wait_revision` |
| Drag commits | One call when the drag ends |
| Context report rate | Ten a second per key |
| Views opened fullscreen | All but checks |
| The app tool for marks | `describe_region` |
| FreeRouting | The fork's JAR backend when Java is present, else its image when Docker is |
| Simulation | The spice connector, through `export_spice_netlist` |

## What the session installs

The session sets up its own machine and asks the person for nothing in the first qualification tables.

- **KiCad 10** from the KiCad PPA, with its 3D models.
- **KiCad nightly** from the KiCad nightly PPA, for the `kicad-cli api-server` case. If the nightly has no `api-server`, the ledger says so and KQ7 is run in the local phase.
- **Xvfb.**
- **Java 21 and the FreeRouting 2.1 JAR.**
- **ngspice and ltspice-mcp**, for the spice connector.
- **Node and pnpm**, for the views bundle.
- **Chromium with WebGL** for the UI checks, with the software-rendering flag if needed, as in the MCP Apps plan.

## What the ledger records

- The base refs of every repository.
- The KiCad settings key that enables the API server, and the socket path each case of KI2 reports.
- The ecad-viewer commit vendored, and each parser fix made to it with the file that needed it.
- The ext-apps version in the bundle, and the bundle's size.
- The source and terms of the KiCad logo.
- Every `sim_*` reference removed.
- Which case of KI2 each scenario ran under.

## Qualification

Run focused tests in the fork and `ruff` on touched paths while working, plus the fork's own checks for registry, parity and generated docs. Then run these against a freshly restarted stack (`claude_code` provider), driving the real UI, and record each in the ledger with commands, handles and the screenshots read. UI checks run at 1440 by 900 and 390 by 844, with `consoleErrors` and `failedRequests` empty.

Fixtures: `clean-led-kicad10`, `large-board`, `drc-courtyard-error` and `multi-sheet-schematic` from the fork, a DXF of an enclosure's board outline with four holes, and one design brief: a USB-C powered board with an ESP32-C3, a temperature sensor and a status LED, inside the DXF's outline.

**The connector and the instance.**

| ID | Scenario | Passes when |
| --- | --- | --- |
| KQ1 | Open Extensions | KiCad is listed with its logo and its state. Its card shows the optional needs and none is required |
| KQ2 | Enable it on a machine without KiCad, ask for the board | The connector is ready. The first call says KiCad was not found, where to get it and how to point at an install |
| KQ3 | Enable it with no keys and price the BOM | Prices come from `jlcsearch` |
| KQ4 | Add a Nexar key and secret, price again | Nexar results appear. The secret is in no model text, transcript, record or log |
| KQ5 | Linux, no display, KiCad 10: open `clean-led-kicad10` and move a footprint | `kicad_session_status` reports Xvfb. The move is committed over IPC |
| KQ6 | The person already has KiCad open on the board, with its API server on | The server attaches, the edit appears in KiCad's window, and on server exit that KiCad keeps running |
| KQ7 | KiCad nightly with `api-server` | `kicad_session_status` reports the headless case. No display server is running |
| KQ8 | After KQ5, read the person's KiCad settings folder | Unchanged |
| KQ9 | Stop the server | Every instance it started is gone |

**The views.**

| ID | Scenario | Passes when |
| --- | --- | --- |
| KQ10 | "Show me the board" on `clean-led-kicad10` | A tab opens with the board. Layers toggle. Double-clicking a net greys the rest |
| KQ11 | Drag a footprint in the view | One `pcb_move_footprint` call. The board in KiCad's file after save matches. The view shows the server's position |
| KQ12 | Flip a footprint, lock another, try to drag the locked one | Both calls succeed. The locked one does not move |
| KQ13 | The agent moves a footprint while the view is open | The view updates with no reload and its revision rises |
| KQ14 | The person moves a footprint in KiCad's own window (KQ6 setup) | The view updates |
| KQ15 | 3D toggle | The board renders in 3D from `export_glb` |
| KQ16 | Open the BOM, choose an alternate for a row, mark one part not fitted | The schematic carries both changes. The view shows them |
| KQ17 | Open a datasheet from the BOM | It opens in the in-app browser |
| KQ18 | Checks on `drc-courtyard-error`, select the violation | The board focuses on it in the workbench |
| KQ19 | Stackup: change a dielectric thickness | `pcb_set_stackup` is called and the consistency gate result is shown |
| KQ20 | `multi-sheet-schematic` in the schematic view | Every sheet renders and navigates |
| KQ21 | `large-board` in the board view | It renders and pans. The ledger records the time to first draw |
| KQ22 | Every board and schematic the qualification produces | ecad-viewer parses it. A failure is a parser fix in the vendored copy, not a skipped case |
| KQ23 | Read the transcript and model text after KQ10 to KQ21 | No board or schematic file text is in either |
| KQ24 | Close the board tab and reopen it. Restart CLIO and open the session | Both times the view returns at the live revision |

**Workbench and feedback.**

| ID | Scenario | Passes when |
| --- | --- | --- |
| KQ25 | Open the workbench. Select a BOM row | Its footprints are highlighted on the board |
| KQ26 | Select a footprint on the board | Its BOM row is selected |
| KQ27 | Select U1 and type "move this closer to J1" | The agent's call names U1 without asking which part |
| KQ28 | "Show me the decoupling for U1" | The agent calls `app__kicad__focus` and the view shows it |
| KQ29 | Draw two marks on the board with a comment each and send them | The agent receives a crop for each and the references and nets under each |
| KQ30 | A mark on an app with no `describe_region` (the MCP Apps fixture) | The mark carries its rectangle and the note |
| KQ31 | Search clio-agent and gact-tui for KiCad, `kicad_pcb` and the view tool names | None in Python or TypeScript outside `catalogs/connectors/` |

**The pipeline.**

| ID | Scenario | Passes when |
| --- | --- | --- |
| KQ32 | The design brief, end to end, with the outline DXF | The board takes the DXF's outline and holes. ERC and DRC are clean. The release package is in the workspace. The agent never asked the person to place or route |
| KQ33 | During KQ32, the datasheets | Each IC's datasheet is in clio-core and the pin check ran before layout |
| KQ34 | During KQ32, a simulation | The regulator stage was simulated through the spice connector from `export_spice_netlist` |
| KQ35 | During KQ32, placement | Each pass is a science log entry with its metrics, and the workbench chart shows every pass |
| KQ36 | *Amended (KA6):* list the tools that reach CLIO's agent | No `sim_` tool |

**Parts and connection.** *Added (ledger KA5, KA7, KA8).*

| ID | Scenario | Passes when |
| --- | --- | --- |
| KQ37 | "find a 10 µF ≥ 25 V X7R 0805 with stock" with no keys | `lib_search_parametric` returns parts whose `specs` meet every filter, from jlcsearch |
| KQ38 | The same part with Nexar keys set | `lib_get_component_specs` merges both sources, and each value names its source. *Local phase: no keys in the cloud session* |
| KQ39 | The agent opens the parts view with three LDO candidates. The person compares two and picks one | It is bound to the symbol |
| KQ40 | An IC whose distributor specs lack its thermal resistance | The agent's value comes from the ingested datasheet, with the page cited |
| KQ41 | A part with no KiCad symbol | The symbol is generated from the datasheet pin table, and the pin check passes |
| KQ42 | jlcsearch unreachable; "find a 3.3 V LDO for this board" | The result names the sources that could be set up; the agent raises the card with those rows and a reason; Not now gives an honest answer from what is there and no second card; a dummy Nexar key restarts the server and Nexar's refusal comes back typed, the secret in no transcript, record or log |
| KQ43 | KiCad removed; "show me this board" | The agent installs KiCad through the sandbox's folder-access card, then opens the board |

Local phase, run by a person and recorded in the ledger:

| ID | Scenario | Passes when |
| --- | --- | --- |
| KQ-L1 | Windows and macOS with KiCad 10: KQ6, KQ10, KQ11, KQ24 | They pass. On a started instance, KiCad's window appears and the view follows it |
| KQ-L2 | Fusion: design an enclosure, export its board outline as DXF, run KQ32 on it, send the STEP back to Fusion | The board fits the enclosure in Fusion |
| KQ-L3 | Send the KQ32 package to a board house quote | It is accepted without file errors |

Nothing in the first four tables may be blocked. A failing scenario there is fixed, not deferred.

## Done

1. Every decision is implemented. The fork's simulation tools are gone, and its `pcb-design` skill is replaced.
2. The fork's checks pass: tests, the capability registry, the parity matrix and the generated tools reference. `ruff check` and `ruff format --check` are clean on touched paths in every repository.
3. Every scenario in the first four tables passes against a freshly restarted stack, each shown with commands, handles and the screenshots read. Local-phase rows say so.
4. The ledger holds every decision with its evidence and every fact listed above.
5. The site's user guide has a page on designing a board with KiCad in CLIO. The developer guide documents `describe_region`.
6. The branches are pushed in the fork, clio-agent, gact-tui and clio-agent-marketplace. The entry pins the fork's pushed commit. The draft PRs carry a summary and the evidence.

## Working conventions

- Use the branch the cloud session is set up to push to, with the same name in every repository, and write it in the ledger header. Open draft PRs against `develop` in the IOWarp repositories and against `main` in the fork.
- Small Conventional Commits, with test evidence in each PR description. The fork uses its own commit lint rules.
- Keep runtime state in `.cloud-run/`. Stop the stack and every KiCad instance when you finish a work block.
