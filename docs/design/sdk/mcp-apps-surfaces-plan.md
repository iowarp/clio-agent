# Connectors and apps in a tab: implementation plan

This plan makes a connector the thing a person enables in CLIO, and lets a connector's view open in a workbench tab that the agent can see and drive. It delivers three connectors that have a view: Burette for molecules, ParaView for 3D scientific data on this machine or on another one, and ltspice-mcp for circuit simulation. ParaView's server is rebuilt from scratch for it.

The plan has eleven workstreams. They are not ordered. The work is one goal that runs until every item under Done holds, and the human reviews the finished branches.

1. **Connectors.** A connector has a logo, says what it needs, and is set up through one card, in Extensions and in the conversation. It can bring a plugin that follows it.
2. **Display modes.** `fullscreen` is a workbench tab, and `inline` is the card.
3. **Lifetime.** An app in a tab stays live until its tab closes.
4. **The app's context reaches the agent.** What a view reports is in the next turn the person sends.
5. **App tools.** A view's own tools are offered to the agent and routed to the open view.
6. **Links and downloads.** `ui/open-link` opens the in-app browser, and a download becomes a workspace file.
7. **Reopening.** An app's opening call is saved, so a closed tab or a restarted CLIO brings the view back.
8. **An app inside an A2UI surface.** One catalog component mounts any app in a surface and ties its state to the surface's data model.
9. **A view's own service.** A view that needs a live service behind it gets that service through CLIO, wherever the browser is.
10. **The ParaView connector.** Rebuilt: a small front that CLIO starts, and a half that runs where ParaView is, joined by one SSH connection.
11. **The test grounds.** Burette, ParaView and ltspice-mcp, enabled as connectors and qualified end to end.

## Contract

This plan is the contract for the eleven workstreams. When code and this plan disagree, change the code. If the plan turns out wrong, choose the fix that best keeps its intent, update this document in the same change, and record the decision in the ledger.

| Document | Governs |
| --- | --- |
| This plan | Connectors and their setup, apps in tabs, app context, app tools, reopening, the A2UI component, a view's service, the ParaView connector, the test grounds |
| `docs/design/sdk/mcp-apps-ledger.md` (new) | This plan's decisions and acceptance evidence |
| MCP Apps specification, revision 2026-01-26 and the draft in `modelcontextprotocol/ext-apps` at `ea3e9d0` | The wire protocol. The draft governs app tools and downloads |
| `tool-calling-plan.md` | Deferred tools, `search_tools`, `loaded_tools` |
| `direct-summoning-plan.md` | `@server` summoning, which also loads a server's app tools |
| `plans/scientific-visualization-plan.md` (project) | The view contract: state channels, capture, introspection, provenance, export |
| `plans/remote-connectors-plan.md` (project) | The Desktop OAuth redirect, scopes, CLIO's client identity, and the long list of services. Its catalog format and its add-connection UI (RC9 to RC11, RC14) are replaced by the connector model here, and its services are added later as entries in this format |
| `plans/clio-relay-plan.md` (project) | Later work on top of this plan: sites that need a scheduler, multi-factor sign-in or a guarded policy, and waking the agent on a job's events |
| `browser-python-control-plan.md` (project) | The in-app browser. This plan touches it only for `ui/open-link` |

**Base.** clio-agent `develop` at `54ae350af`, gact-tui `develop` at `d13393f13`, clio-kit at `c22c7e4`, clio-agent-marketplace at the submodule pin.

Decisions use the prefix `CN` for connectors, `MA` for the app host and `PV` for ParaView. Qualification scenarios use `AQ`.

## Rules

The megachange rules carry over: delete and replace, the model decides, no silent fallback, one mechanism per concept, all context lives in clio-core, evidence over claims. Seven more apply here:

1. **What exists is not a limit.** If part of CLIO, of clio-kit or of the current ParaView server is wrong for this design, it is deleted and rebuilt. If CLIO lacks something this design needs, CLIO gets it. "CLIO cannot do that today" is never a reason to bend the design.
2. **One way to ask.** A connector says what it needs in one form, and CLIO asks for it with one card. No server invents its own way to get an address, a sign-in or a secret.
3. **A connector is data.** No service name appears in Python or TypeScript in clio-agent or gact-tui. Adding a connector is adding an entry and a logo.
4. **The view is the app author's.** CLIO adds no viewer library for an app's domain and never reads or edits a view's DOM. A server or a view IOWarp writes lives in clio-kit, so any MCP Apps host can use it.
5. **Private data stays off the model.** The full tool result an app receives is UI data, as PR #947 established. A secret typed into a setup card is never in model text, a transcript, a record or a log.
6. **No timers on an open view.** A view the person has open is not expired or capped. If the agent waits on a view that does not answer, the person cancels the turn.
7. **Linking is an A2UI semantic.** Views link by sharing A2UI data model paths. An app joins by being mounted in a surface, and never by a private channel between two apps.

## Out of scope

Do not implement, stub or reserve fields for these:

- clio-relay, JARVIS, schedulers and guarded sites. Nothing in this plan depends on them.
- A remote machine that asks for a password or a one-time code on every SSH connection. That case is relay's. Here it gives a clear message.
- Waking the agent on a job's events. It belongs with jobs, in the relay plan.
- The Desktop OAuth redirect, per-connection scopes, CLIO's client identity document, and the long list of services. They stay in `plans/remote-connectors-plan.md`.
- Picture-in-picture. CLIO advertises `inline` and `fullscreen` only.
- The embedded first-party tools Claude shows for Atlassian, Box and Adobe.
- Entry points (opening an app with no tool call), and WebMCP mirroring from the draft spec.
- A2UI 1.0. CLIO speaks v0.9.1.
- Views for `hdf5`, `adios` or any other clio-kit server.
- A vtk.js or Mol* package in gact-tui. Both arrive inside apps.
- Servers that send no view: Open Targets, Autodesk help, Fusion, KiCad and the like.
- BAMCP.

## Current state

Read at the base.

**Connections, plugins and Extensions.**

- Settings has an Extensions page: "Plugins, marketplaces, MCP connections and skills for your agents", one list with a detail pane (`extension-library.tsx`). Its Add button adds a plugin source, a skill or an MCP connection.
- "Add an MCP connection" (`mcp-connection-dialog.tsx`) asks for a name, a URL or a command with arguments, the transport, request headers and authentication. A saved connection can be made available in every conversation.
- `settings-mcp-services.tsx` lists every MCP service a conversation gets, each with its state in words and Reconnect, Disconnect and Remove.
- A plugin is resolved for one agent, for one user, when a session starts (`plugins/resolve.py`): packs are expanded, the user's additions and removals are applied, requirements are pulled in. Changing plugins in a running conversation reaches the agent as an injection, and its prompt catches up at the next compaction (`plugin-change-pending-notice.tsx`).
- A plugin can bind one of its server names to a saved connection with `connections` (`plugins/loader.py`). When the connection is missing the server is unavailable and CLIO names the connection.
- A plugin's own server gets its environment from `mcp.json`, with three placeholders CLIO fills in (`plugins/servers.py`). A plugin cannot declare a value the person must supply.
- Built-in plugins are `core`, `fleet`, `memory`, `orchestration` and `streaming`. The product marketplaces are `clio-builtin` and `clio` (`plugins/compose.py`).
- Nothing has a logo. Neither the plugin manifest nor a connection carries one.
- When a remote server needs a sign-in during a conversation, the call fails with "MCP sign-in required; connect this service in CLIO" (`tools/mcp_durable_oauth.py`, `tools/mcp_runtime.py`). The person leaves the conversation, signs in under Settings, and asks again.
- Neither questions nor A2UI have a secret field. A value typed into either is recorded.
- `tests/fixtures/oauth_mcp_server.py` is an MCP server with an OAuth authorization server.

**The app host.**

- **Host.** `gact/mcp_apps.py`, `gact/mcp_app_routes.py`, `gact/mcp_app_sandbox.py` and `tools/mcp_extension_registry.py` implement an MCP Apps 2026-01-26 host (PR #947, 2026-07-15). CLIO declares `io.modelcontextprotocol/ui` with `mimeTypes` to every server.
- **Routes.** Under `/v1/sessions/{sid}/mcp-apps/{app_id}`: the descriptor, `/sandbox`, `/tools/call`, `/resources/read`, `/model-context` (PUT), `/messages`, and DELETE to close. They are bound to the app by a capability, and the sandbox is served from a separate origin.
- **View surface.** `web/src/components/clio/mcp-app-surface.tsx` mounts the double-iframe sandbox and answers `ui/initialize`, tool input and result, size change, host context change, teardown, `tools/call`, `resources/read`, `ui/update-model-context`, `ui/message`. It reports `displayMode: 'inline'` and `availableDisplayModes: ['inline']`, and answers every `ui/request-display-mode` with `inline`. `ui/open-link` and downloads are refused as unsupported methods.
- **Sandbox CSP.** `_csp_header` in `gact/mcp_app_sandbox.py` builds the frame's policy from the resource's `resourceDomains`, `connectDomains`, `frameDomains` and `baseUriDomains`.
- **Why only one app is live.** This is a design rule, not a technical limit. The v0.10 program (`docs/design/v010-program-2026-08.md`, `v010-slices-2026-08.md`, slice P3.1, gact-tui issue #324, still open) put MCP Apps, A2UI and Live Web in one viewport and required "mandatory teardown transitions". `use-active-mcp-app.ts` implements it: the latest app in the transcript is live, and the previous one is closed through `closeMcpApp`.
- **Why the registry expires records.** `_REGISTRY_LIMIT = 64` and `_REGISTRY_TTL_S = 3600` in `gact/mcp_apps.py` were set in PR #947 and never revisited. The limit is process-wide. A record holds the private tool result (up to 1 MiB, `_MAX_PRIVATE_RESULT_BYTES`) and the view's HTML. A record whose result declared a cleanup tool is never expired. The others expire one hour after their last use, and `get` refreshes that time on every call from the view. The knob audit (`docs/knob_audit/knob_audit_A.md` lines 73 to 74, `knob_audit_B.md` lines 402 to 403) flagged both constants as hardcoded.
- **The view's context reaches the agent only through the view's own messages.** `PUT .../model-context` stores the body on the in-memory record. `POST .../messages` appends it to that message's model text. A message the person types carries none of it.
- **App tools.** None. The host never sends `tools/list` or `tools/call` to a view.
- **Reopening.** None. A closed app is gone, and no app survives a CLIO restart.
- **A2UI.** CLIO speaks A2UI v0.9.1 (`clio_schemas.a2ui.v0_9_1`), with `@a2ui/react` 0.11.1 and `@a2ui/web_core` 0.11.0 in gact-tui. The built-in components are chart, code, data-table, diff, map, mermaid, mesh-viewport, raster-viewport and workflow. None mounts an MCP App.
- **Tabs.** `workbench-shared.ts` defines `WorkbenchTab` as a typed union. `use-workbench-tab-state.ts` exposes `openTab`.
- **Spec.** The ext-apps repository at `ea3e9d0` has two specification folders, `2026-01-26` and `draft`. Display modes and `frameDomains` are in both. "App-Provided Tools" and file download are only in the draft. The SDK is at 2.1.0, and app tools need 1.7.0 or later.

**ParaView.** clio-kit's `mcp-servers/paraview` (`paraview-mcp` 1.0.0) is one process on the person's machine that attaches to a `pvserver` started with `--multi-clients`, so that the ParaView desktop program can attach beside it and be the display. It has 27 tools, returns screenshots and sends no view. None of it is kept.

## Where this leaves the scientific visualization plan

The component table in `plans/scientific-visualization-plan.md` is not a list of missing work. On the base:

| Component | State |
| --- | --- |
| HDF5 tree (`packages/hdf5-viewer`, `@h5web/app` 17.0.0, h5grove route in clio-agent) | Exists |
| `clio.mesh-viewport.v1` (three.js) | Exists |
| `clio.map.v1` (MapLibre) | Exists |
| `clio.chart.v1` (Vega-Lite) | Exists |
| `clio.image.v1`, `clio.raster-viewport.v1` | Exist |
| Molecules (Mol*) | Missing. Delivered here by Burette |
| 3D scientific data, on this machine and on another | Missing. Delivered here by the ParaView connector |

A2UI keeps what it is for: linked views and live sources. An app takes part in linking through the component in MA20.

## The test grounds

Three connectors, each with a view. Each third-party one was read from source or vendor documentation on the date shown.

| | Burette | ParaView | ltspice-mcp |
| --- | --- | --- | --- |
| Domain | Molecular structures | 3D scientific data | Circuit simulation |
| Who wrote the server | Sergei Nikolenko (`SergeiNikolenko/Burette`, MIT, read at `b1ac8ae`, 2026-11-10) | IOWarp, in clio-kit | `cognitohazard/ltspice-mcp` (GPL-3.0-or-later, read at `9455f38`, 2026-11-06) |
| Server | A hosted URL, `https://burette-plugin.vercel.app/mcp`, no sign-in | A command CLIO starts | A command CLIO starts, `uvx ltspice-mcp`, a separate process because of the licence |
| Needs | Nothing | Where ParaView runs. Optional, and the agent can set it | Nothing. ngspice or LTspice must be installed |
| Brings a plugin | No | Yes: a skill for working with scenes | No |
| View | Mol* and Ketcher (`ui://burette/molecular-viewer-v21.html`, `ui://burette/ketcher-editor-v1.html`) | trame (`ui://paraview/view`) | A chart (`ui://ltspice-mcp/plot`) |
| Proves | A connector with no setup, app tools, context delivery, the A2UI component | A setup card with an optional need, a value the agent sets, a plugin that follows its connector, a long-lived view with a live service, reopening, linking, a remote machine over SSH | The inline card still works beside tabs |

**Burette in detail.** The hosted service is in `apps/burette-public-plugin`, served over Streamable HTTP.

- Tools: `preview_pdb_structure` (by PDB ID), `render_molecular_scene`, `open_ketcher`, `control_ketcher`, `preview_molecular_file`.
- `preview_molecular_file` and the file form of `render_molecular_scene` take a ChatGPT file parameter. CLIO offers the PDB-ID and Ketcher tools only.
- The view uses app tools, `requestDisplayMode` with `fullscreen`, `updateModelContext` (a selection of up to 96 atoms), `ui/message`, `openLink` and `callServerTool`. CLIO answers its fullscreen request with `inline` today.
- If the hosted address cannot be reached from the session, the session runs Burette from its source at the pinned commit as a local server, qualifies against that, and records which one it used.

**ltspice-mcp in detail.** One author, marked work in progress. Python 3.11 or later, stdio. It does not support macOS.

- Tools: `run_experiments`, `jobs`, `analyze_results`, `inspect`, `edit_schematic`, `verify_circuit`, `plot_waveform`, `run_code`.
- `plot_waveform` opens `ui://ltspice-mcp/plot`, a self-contained page with the data inlined and no network. The server shows it only to a client that declares the UI extension, which CLIO does.

## Design: connectors

**CN1. A connector is what a person enables.** It is one entry: who it is, where its server is, what it needs, and the plugin it brings.

```yaml
- id: paraview
  name: ParaView
  logo: paraview.svg
  description: Open scientific data in ParaView, on this machine or another, and see it in a tab
  server:
    command: clio-kit
    args: [mcp-server, paraview]
    env:
      PARAVIEW_ADDRESS: ${need.address}
      PARAVIEW_VIA: ${need.via}
      PARAVIEW_PREPARE: ${need.prepare}
  needs:
    - {key: address, kind: address, label: Where ParaView runs, required: false, agent_settable: true,
       help: Leave empty for this machine. Otherwise an SSH host name.}
    - {key: via, kind: value, label: Jump host, required: false, agent_settable: true}
    - {key: prepare, kind: value, label: Command to run first, required: false, agent_settable: true,
       help: For example "module load paraview".}
  plugin: paraview

- id: burette
  name: Burette
  logo: burette.svg
  description: See and edit molecular structures
  server: {url: https://burette-plugin.vercel.app/mcp}
  needs: []
```

- Entries live in `catalogs/connectors/*.yaml` in clio-agent with a JSON Schema, and the logos sit beside them. `GET /v1/connectors` returns the validated entries with each one's state for the caller.
- This plan ships three entries: `burette`, `paraview` and `spice`.
- "Add by URL or command" keeps working for a server that has no entry. It makes a connector with no logo. CLIO works out its needs: the address is what was typed, and a sign-in is needed when the server's own metadata asks for one.

**CN2. Four kinds of need.**

| Kind | What the card shows | Where the value goes |
| --- | --- | --- |
| `address` | A text field | `${need.<key>}` in the entry's URL, headers, arguments or environment |
| `sign-in` | A button that opens the browser and comes back with the account | The existing MCP sign-in for that connection |
| `secret` | A masked field | `${need.<key>}` in headers or environment only, never in arguments |
| `value` | A text field or a picker | `${need.<key>}`, as for an address |

A need is required or optional, can have a default, and can be marked `agent_settable`.

**CN3. One state.** Each connector has one state for each person: ready, needs something (and what), or cannot be reached (and why). It is shown in words beside the name everywhere the connector appears.

**CN4. The setup card.** The Extensions detail pane shows the logo, the name, the state, and one row for each need. A required need that is empty keeps the connector at "needs something". An optional one does not. Saving a row makes CLIO connect, list the tools and update the state.

**CN5. The same card in the conversation.** When the agent calls a tool on a connector that is not ready, the call does not fail. It waits, the card appears in the conversation with the rows that are missing, and when they are filled the same call continues and returns its result. If the person declines, the call returns a typed refusal that names the connector. This replaces the "MCP sign-in required" failure.

**CN6. The agent can set a value.** A native tool, `set_connector_value`, sets a need that is marked `agent_settable`, when the person gives the value in the conversation or another tool has just produced it. It follows the session's tool permission rules. It is refused for a secret and for a sign-in. The card shows the new value.

**CN7. Applying a change.** When a value changes, CLIO reconnects to a URL server or restarts a command server with the new values, and lists its tools again. A view that was open reconnects through its saved call (MA19).

**CN8. Secrets.** A secret is stored in the same store CLIO uses for connection credentials today, and the ledger records which. It is never returned to the UI after saving, and it is in no model text, transcript, question record or log.

**CN9. A plugin that follows a connector.** The `ai.iowarp.clio` extension gains `follows`, naming a connector.

- When the connector is enabled, the plugin joins the person's additions, so the resolver includes it for their agents. When the connector is disabled, it leaves.
- It is not offered in the plugin pickers, because without its connector it has nothing to talk to.
- It binds its server name to the connector's connection with the existing `connections`.
- Such plugins ship in the `clio` product marketplace. The person enables ParaView. They do not install its plugin.

**CN10. Where it applies.** A connector is enabled for every conversation or for chosen agents, with the controls CLIO has today. In a running conversation the change reaches the agent through the existing plugin-change injection, and its tools are usable at once.

**CN11. Logos.** A logo is an SVG. Each is the vendor's own mark, taken from that vendor's brand kit, and the ledger records the source and its terms for each one shipped. An entry with no logo shows a monogram.

**CN12. Extensions lists connectors first.** Connectors appear in the Extensions list with their logos and states, above plugins and skills. The raw "Add an MCP connection" form stays behind "Add by URL or command".

## Design: display modes and the tab

**MA1.** CLIO advertises `availableDisplayModes: ['inline', 'fullscreen']`. `fullscreen` is a workbench tab of a new kind `mcp-app` in `WorkbenchTab`, carrying the app's identity (`appInstanceId`, `dataRef`, `sessionId`, `resourceUri`, `sourceServer`, `toolName`) and a label from the tool's title.

**MA2. Who changes the mode.**
- The view: `ui/request-display-mode` with `fullscreen` opens the tab and returns `fullscreen`. With `inline` it closes the tab and returns `inline`. A request for `pip` returns the current mode.
- The person: the card has "Open in tab", and the tab has "Return to conversation". Closing the tab returns the app to inline.
- After any change the host sends `ui/notifications/host-context-changed` with the new `displayMode` and container size.

**MA3. One frame per app.** The frame exists in exactly one place. While the app is in a tab, its card in the conversation shows a row that says so and focuses the tab on click. Moving between the two reloads the frame. The host then replays `ui/initialize`, the tool input and the tool result, as it does on first mount.

**MA4. The mode is recorded on the app record.** `PUT /v1/sessions/{sid}/mcp-apps/{app_id}/display-mode` with `{mode}` sets it. The descriptor returns it, so a reloaded UI restores the tab.

## Design: lifetime

**MA5. The live set replaces the one-live-app rule.** The apps kept live in a session are the latest inline app in the conversation, every app whose mode is `fullscreen`, and every app mounted in a visible A2UI surface. `use-active-mcp-app.ts` is replaced by a session-level store of that set. A newer inline app still closes the previous inline app. It never closes an app in a tab or in a surface. gact-tui issue #324's acceptance line "switching surfaces leaves the App frame DOM-absent" is amended to apply to inline apps only.

**MA6. A record in `fullscreen` or in a surface is pinned.** `MCPAppRegistry` does not expire it and does not count it toward `_REGISTRY_LIMIT`. The limit and the one-hour expiry keep applying to inline records. A record with a declared cleanup tool keeps its existing rule.

**MA7. Closing.** Closing the tab while the app has no card left in view, unmounting its surface, ending the session, or shutting down the server closes the app through the existing `claim_close` and cleanup path.

## Design: the app's context reaches the agent

**MA8.** When the person sends a message, the turn's model text includes one block per live app that has reported context: the server name, the tool that opened it, and the context the view last reported. The block is added the same way `post_mcp_app_message` adds it today, moved into one function both paths call.

**MA9.** The reported context is stored with the session's context in clio-core, keyed by app instance, and read from there when a turn is built. The copy on the in-memory record is deleted. The ledger names the clio-core path used and every reader of `record.model_context`.

**MA10.** The 128 KiB cap on one app's context stays (`_MAX_MODEL_CONTEXT_BYTES`). A larger update is refused with the existing 413.

## Design: app tools

Follows "App-Provided Tools" in the draft specification.

**MA11. Discovery.** When a view's `ui/initialize` declares the `tools` capability, the surface sends `tools/list` to the view and posts the result to `PUT .../mcp-apps/{app_id}/app-tools`. It repeats on `notifications/tools/list_changed`. When the frame unloads, it posts an empty list.

**MA12. Offering them to the agent.** Each app tool becomes a deferred tool of the session named `app__<server>__<tool>`, with the view's description and input schema, in the catalog `search_tools` reads. `@<server>` loads them with the server's other tools. They leave the catalog when the list empties. They join the tool list at the next compaction like any loaded tool.

**MA13. Calling one.** The agent's call goes to GACT, which emits a session event `mcp_app_tool_call` with the app instance, a call id, the tool name and arguments. The surface holding that app forwards it to the view as `tools/call` and posts the view's result to `POST .../mcp-apps/{app_id}/app-tool-results/{call_id}`. The agent receives the result's text blocks.
- If no surface holds the app, the call returns at once with a result that says the app is not open and names the tool that opens it.
- There is no timeout on an open view (rule 6).
- Permission follows the session's tool permission rules, keyed by the `app__` name.

**MA14. Limits taken from Claude's published ones** so apps written for it behave the same: at most 64 tools per view and 64 KiB per input schema. A view over either offers no tools, and the surface logs why.

## Design: links and downloads

**MA15.** `ui/open-link` opens the URL in the in-app browser as a new page. `http` and `https` only. Anything else is refused with a typed reason the view receives.

**MA16.** The draft's download request saves the file into the session's workspace folder and marks it as an artifact. The view receives the saved path. The ledger records the method name and parameters from the draft at `ea3e9d0`.

**MA17.** The surface advertises the matching host capabilities, so views enable their controls.

## Design: reopening

**MA18. The opening call is saved.** When a tool result opens a view, CLIO saves the server, the tool, its arguments and the display mode with the session's context in clio-core. The private result and the view's HTML are not saved.

**MA19. Reopening replays the call.** Reopening a closed tab, or opening a session after CLIO restarted, calls the saved tool again with the saved arguments and mounts the new result. If the server is not running, CLIO starts it as it does for any call. The replay follows the session's tool permission rules.

- A server that wants exact restoration makes its opening tool restore from its arguments. The ParaView connector does (PV11).
- A replay that fails leaves the card with the reason and the saved call, so the person can retry.
- CLIO keeps no copy of the view's state. What the view shows after a reopen is whatever the server returns.

## Design: an app inside an A2UI surface

**MA20. One component mounts any app.** `clio.mcp-app.v1` joins CLIO's built-in catalog.

| Prop | Meaning |
| --- | --- |
| `server`, `tool`, `arguments` | The call that opens the app. It is the saved call of MA18 |
| `state` | A list of channels. Each has the key the view reports it under, the data model path it binds to, and optionally the tool that sets it |
| `height` | The frame's height in the surface |

It renders with the existing MCP App host component. Nothing about the app's domain is in it.

**MA21. The state bridge.**

- **View to surface.** When the view reports context through `ui/update-model-context`, each key named in `state` is written to its data model path. Other components bound to that path update, as they do for any data model change.
- **Surface to view.** When a bound path changes from outside the view, CLIO calls the channel's tool with the new value. The tool is one of the server's tools or one of the view's app tools. A channel with no tool is report-only.
- **No echo.** A change that came from the view is not sent back to it.

This is the view contract of `plans/scientific-visualization-plan.md` applied to an app: time, selection and named parameters are channels. Camera stays inside the view unless a surface binds it.

**MA22. Only key states cross.** A channel is something another view can use: a timestep, a selection, a threshold. A view does not report every frame of a drag. The component drops reports for one channel that arrive faster than ten a second and keeps the last.

## Design: a view's own service

**MA23. CLIO carries a view's service to the browser.** Some views are not a self-contained page. They need a live service behind them, over HTTP and a WebSocket. A server declares that service in the UI data of the result that opens the view: a loopback port on the machine CLIO runs on, and a token.

- CLIO serves that port under the app's own route, `/v1/sessions/{sid}/mcp-apps/{app_id}/service/`, for HTTP and for WebSocket upgrades. The route is bound to the app by the same capability as the app's other routes.
- CLIO adds the token on the way through. The browser never sees it, and neither does the agent.
- The view frames or connects to that route and nothing else. It therefore works wherever the browser is: on the same machine, or on another one that reached CLIO over the network.
- Only a loopback port is accepted, and only from a server CLIO launched. Closing the app closes the route.

This replaces any rule that lets a view reach `127.0.0.1` directly. A view never does.

## Design: the ParaView connector

The goal is a ParaView in the agent's tab that the agent controls, whether ParaView runs on the person's machine or on another one.

```
on the person's machine                         where ParaView is
+-------------------------------+               +--------------------------------------+
| CLIO                          |               | the node half, in ParaView's Python: |
|   |  MCP over stdio           |   one ssh     |   scene engine + trame               |
|   v                           |   one port    |   one loopback port, one token       |
| the front (plain Python)  ----+---------------+-->                                   |
|                               |               | a pvserver already running there,    |
| the tab <- CLIO's service     |               | if there is one, becomes its session |
|            route (MA23)       |               +--------------------------------------+
+-------------------------------+
```

When ParaView is on the person's own machine, the right-hand box is on that machine too and there is no `ssh`.

**PV1. Two halves.**

- **The front** is plain Python and speaks MCP over stdio. CLIO starts it with the session, as it starts any connector's command. It does not need ParaView, so the tools are listed before any ParaView is known.
- **The node half** runs inside ParaView's own Python on the machine where ParaView is. It holds the scene engine and the trame application, on one loopback port protected by one token.
- MCP never crosses the network. The front passes each scene tool to the node half over that port.

**PV2. Nothing of the current server is kept.** clio-kit's `mcp-servers/paraview` is deleted and rebuilt: the tools that attach to a `pvserver` started with `--multi-clients`, the capabilities module, `viz.html`, and their tests and documents. The new server never uses `--multi-clients`.

**PV3. Attaching.** The front attaches when a scene tool is first called, and again after the address changes.

- **No address:** it starts the node half with the `pvpython` on this machine.
- **An address:** it runs one system `ssh` to that host, with the person's own keys, agent and `~/.ssh/config`. A `ProxyJump` in the config carries the hops, and the `via` value supplies the jump host when the config has none. Over that one connection it starts or finds the node half, and forwards one local port to the node half's port.
- The `ssh` runs with `BatchMode=yes`. A host that asks for a password or a code fails at once with a message that says interactive sign-in is needed. Nothing hangs.
- `ssh ares; ssh ares-compute-08` is therefore one value: address `ares-compute-08`, via `ares`.

**PV4. The first time on a machine.** The front prepares a folder in the person's home there, `~/.clio/paraview/<server version>/`: an environment built with the Python of that machine's `pvpython`, holding trame and trame-vtk, and the node half's code, copied over the same connection.

- The `prepare` value is run first, so `module load paraview` or an activation line puts `pvpython` on the path.
- When the machine cannot reach the package index, the front builds the environment from the jump host, which shares the home folder on a cluster. When neither can, the message says exactly what is missing.
- ParaView 6.0 or later is required. Its Python is 3.12.
- Later attaches reuse the folder and take seconds.

**PV5. A ParaView that is already running.** If a `pvserver` is listening on the node, on port 11111 or the port given, the node half connects to it as its one client, and what it shows is that session's data. If none is listening, the node half is the ParaView session itself.

**PV6. Staying alive.** The node half does not die with the connection.

- It runs detached and writes its port and token to a file in its folder that only the person can read.
- When the link drops, it keeps the scene. The next tool call attaches again and finds it.
- With no link it shuts itself down after thirty idle minutes, and writes the scene manifest first.

**PV7. The view is trame's.** IOWarp writes no viewer.

- The trame application uses trame-vtk's `VtkRemoteLocalView` bound to the engine's render view.
- In local mode the node half sends geometry and the browser draws it with vtk.js, so the camera runs in the browser. This is the default.
- In remote mode ParaView renders and sends images, with reduced quality while the person drags. The view switches to it when the geometry to send passes ParaView's remote render threshold, 20 MB by default, and the person can switch by hand.
- trame-vtklocal, which draws with VTK compiled to WebAssembly, is the local renderer Kitware intends as its default. Its 1.x line needs VTK 10, which ParaView 6 does not ship. Moving to it later is one widget in the trame application.
- The tab reaches trame through CLIO's service route (MA23). The front declares its forwarded port and the token in the UI data of `open_view`.

**PV8. The tools.**

| Tool | What it does |
| --- | --- |
| `open_view` | Opens a dataset, or restores a saved scene, and returns the view. This is the opening call |
| `create_filter`, `remove_scene_object` | Change the pipeline |
| `set_representation`, `set_timestep` | Change what is shown |
| `set_camera`, `fit_camera` | Move the camera |
| `inspect_selection`, `measure_field` | Exact values: what was picked, a field's statistics and histogram |
| `capture` | A PNG rendered by ParaView at the camera the view last reported |
| `export_artifact`, `export_scene` | A figure, or the scene manifest |
| `scene_state` | Where it is attached, the dataset and the pipeline |

- Each tool is one command to the engine, applied whole: it commits or it rolls back. After a commit the trame view updates over its own connection.
- The agent sees through `capture` and gets numbers through `inspect_selection` and `measure_field`, never from a colorbar.
- Paths are paths on the machine where ParaView is.
- There is no connect tool. "Connect to the ParaView on ares-compute-08" is the agent setting the connector's address (CN6).

**PV9. A dataset that is still being written.** `open_view` can be told the source is still growing. The node half re-reads the reader's time list when the source grows. A view that is following the latest step moves to it. A view the person has parked on a step stays there and shows that newer steps exist. Each new step is reported through `ui/update-model-context`.

**PV10. The view resource.** `ui://paraview/view` is a small page that frames the trame application through the service route and passes state between it and the host. It asks for `fullscreen`, and reports time, selection, representation and camera through `ui/update-model-context`, each at most ten times a second.

**PV11. Reopening restores the scene.** The node half writes the scene manifest after every committed command, in its folder. `open_view` takes a `scene` argument, and the first open returns the scene's id for the agent to keep in the call. A replay of that call (MA19) attaches to the same address and restores the scene from its manifest.

**PV12. Nothing listens on a network.** The node half binds to loopback on its machine and requires its token. The only way in is the person's own SSH connection. The front's forwarded port is loopback on the person's machine, and the browser reaches it only through CLIO (MA23).

**PV13. The plugin.** The `paraview` plugin follows the `paraview` connector (CN9). It carries one skill: how to open data, how to look and measure before making a claim, and how to point the connector at another machine when the person names one.

## Addendum: the agent looks at an app; comments on app views

The plan gave the agent a view's context (MA8) and its tools (MA13), but not its pixels, and the comment system ([comments plan](comments-and-visual-feedback-plan.md)) did not cover app views. Both now rest on one snapshot. Decisions use the prefix `CM`; the ledger rows are in `mcp-apps-ledger.d/comments.md`.

**CM1. The agent looks.** A native tool, `look_at_view(view)`, deferred in the core plugin and withheld from a model without image input, names one of the session's live views (by title, server, tool or instance id; the only one when there is one) and returns an image of what the view shows now, as the person sees it, with the context it last reported (MA9) and its identity. It works as an app tool call does: the session event `mcp_app_look` goes to the surface holding the view, the surface posts the snapshot to `POST …/looks/{call_id}`, and the call waits with no timeout (rule 6); cancelling the turn ends it. A surface says it holds a view with `PUT …/shown`, so a look at a view no surface holds is answered at once (`mcp_app_view_not_shown`), naming how to show it. The image reaches the model as the call's native image part (the `view_image` descriptor path), followed by the identity, the context and the snapshot's gaps in words; the image is also kept as an artifact the transcript links. The app-context block (MA8) tells the agent which open views are on screen and that it can look at them.

**CM2. Where the pixels come from.** CLIO's own outer sandbox document takes the snapshot. A view frame is on its app's sandbox origin with `allow-same-origin`, so the sandbox document is same-origin with the view and with frames the view nests from that origin (a view's service, MA23). It answers the host's `clio/view-snapshot` request itself; the request never reaches the view, and nothing in the view changes. It paints element backgrounds, every visible `canvas`, `img`, `video` and `svg`, and one-line HTML text, each clipped to its frame. A WebGL canvas that keeps no drawing buffer (vtk.js, which trame uses) reads blank once shown, so during a snapshot its context's draw calls are watched, the view's frame shrinks by one pixel for two frames so the app redraws, and the canvas is copied when the app's drawing task ends. Mol* keeps its buffer and is read directly. This works for any app, needs nothing from the app author, and reads what the person sees rather than a server-side re-render.

- Every gap is typed and stated in the result: `frame_cross_origin`, `webgl_frame_not_redrawn`, `cross_origin_pixels`, `not_drawable`, `wrapped_text_not_drawn`; a refused snapshot (`view_not_readable` for a view that dropped `allow-same-origin`, `view_not_laid_out`, `snapshot_tainted`, `snapshot_failed`) answers the look with its reason.
- The specification has no screenshot request (the draft lists screenshot APIs as future work and names host-side capture of raw HTML as a reason for that content type), so the view is not asked.
- A better picture where the app offers one stays the agent's choice: a server tool that renders an image (ParaView's `capture`, at the camera the view last reported) is one of the server's tools, and the snapshot result does not replace it. CLIO does not call it on its own, since which tool renders what is the server's knowledge, not data CLIO holds.

**CM3. Comments on app views.** Every frame of an app (the card, the `mcp-app` tab and `clio.mcp-app.v1` in an A2UI surface) has the one Comments icon. The session is region-only: the person drags a region over the view, or clicks a point (a 64-pixel box centred on it), and types a comment per mark. Each mark takes the CM2 snapshot when it is made, at twice the view's size, and the context the view had last reported then. A view is not versioned, so the marks travel as a `comment_session` attachment of the next message (kind `mcp-app`), like page and surface comments: the overview with every mark drawn, then each mark's crop. The server renders one readable entry per mark: the view's identity, the box in view percentages and pixels, the point, the view's report at that moment (shared reports are not repeated), and a typed note when a mark has no picture or the picture misses parts. No anchor JSON reaches the model. The marks stay pinned on the view while the comment session is open; the agent's reply is the next turn in the conversation.

**CM4. Out of scope, still.** Comments stay on what the screen shows. A comment is not bound to a camera pose or a time step beyond the report it carries, and the view is not driven back to that state.

## Defaults for the open questions

Take these and record each in the ledger.

| Question | Default |
| --- | --- |
| Where connector entries live | `catalogs/connectors/*.yaml` in clio-agent, shipped with the release |
| A connector's state after enabling with an empty optional need | Ready |
| A declined card in the conversation | The call returns a typed refusal. Nothing is retried |
| Who may call `set_connector_value` | The agent, for needs marked `agent_settable`, under the session's permission rules |
| Display modes offered | `inline`, `fullscreen` |
| `pip` requests | Answered with the current mode |
| State on moving a view | The frame reloads, and the host replays input and result |
| Registry rules for an app in a tab or a surface | No expiry, not counted |
| Registry rules for inline apps | Unchanged: 64 records, one hour idle |
| Where reported context and the saved call live | clio-core, with the session's context |
| App tool names | `app__<server>__<tool>`, deferred |
| A call to an app that is not open | Immediate result saying so |
| Timeout on an open view | None |
| Spec revision declared | 2026-01-26, plus the draft features by capability |
| Reopen after a CLIO restart | Tabs are restored and each replays on first focus |
| Channel report rate | Ten a second for each channel |
| ParaView view mode | Local, switching to remote above the remote render threshold |
| ParaView's local renderer | vtk.js through trame-vtk |
| The node half's idle shutdown | Thirty minutes with no link |
| `pvserver` port looked for | 11111 |
| ltspice-mcp distribution | `uvx`, separate process, not bundled |

## What the session installs

The session sets up its own machine. It asks the person for nothing in the first qualification table.

- **Docker.** The session starts the daemon if it is not running, and installs Docker if the image lacks it.
- **ParaView 6, headless,** from conda-forge, once for the session's own machine and once inside the node image.
- **trame and trame-vtk**, in the environment the node half uses.
- **ngspice**, for ltspice-mcp.
- **An SSH server** in each container, and an SSH client on the session's machine.
- **Chromium for the UI checks.** Mol* and trame's local view draw with WebGL. If the browser started by `scripts/cloud/ui_check.mjs` has no WebGL, the session adds the software-rendering flag to that launch and records it.
- **Burette from source**, only if its hosted address cannot be reached.

## The cluster in containers

The remote case is tested as far as a single machine allows, with containers standing in for a cluster's nodes. The session builds this fixture and checks it into the repository so it can be rerun.

| Container | Stands for | Set up as |
| --- | --- | --- |
| `login` | A login node, `ares` | An SSH server reachable from the session's machine. It can reach the package index. It has no ParaView |
| `node08` | A compute node, `ares-compute-08` | An SSH server reachable only from `login`. It has ParaView 6 behind a `prepare` line. It cannot reach the package index |
| `locked` | A site with interactive sign-in | An SSH server that accepts only a password |

- `login` and `node08` share one home folder, as a cluster does.
- One user, one key pair. The session's `~/.ssh/config` has `login`, and `node08` with `ProxyJump login`.
- A second form of `node08` starts with a `pvserver` already running and a dataset loaded in it.
- If containers cannot run in the session, the same three roles are separate SSH servers on loopback ports with separate users and homes. The ledger says which form was used and what it could not model.

What a container cannot stand for is left to the local phase: a real scheduler, a real two-factor site, a GPU node, and network distance.

## What the ledger records

These are facts the work depends on. Each is recorded with its source before the code that relies on it is merged.

- The store that holds a connection's credentials today, used for secrets in CN8.
- Every place the "MCP sign-in required" failure is raised or shown, for CN5.
- How the resolver takes a person's additions, for CN9.
- The source and terms of each logo shipped.
- The output of `plans/mcp-app-probe.py` against ltspice-mcp and Burette: server info, the tools that carry a UI resource, each resource's MIME type, CSP and size, and which host features the view's code uses.
- From the draft specification at `ea3e9d0`: the exact messages and capability names for app tools, downloads and open-link.
- The clio-core path for MA9 and MA18.
- Every use of `useActiveMcpAppId`, `McpAppHistoryLine` and `closeMcpApp`.
- How the app routes' capability is issued and checked, for MA23.
- The trame, trame-vtk and ParaView versions qualified, how headless ParaView was installed, and whether the browser needed the software-rendering flag.
- Which form of the cluster fixture was used.

## Qualification

Run focused tests and `ruff` on touched paths while working. Then run these against a freshly restarted stack (`claude_code` provider), driving the real UI, and record each in the ledger with commands, handles and the screenshots read. UI changes are checked as the megachange plan describes, at 1440 by 900 and 390 by 844, with `consoleErrors` and `failedRequests` empty.

Fixtures the session builds besides the cluster: three fixture connector entries (one with no needs, one that needs an address and a sign-in against `tests/fixtures/oauth_mcp_server.py`, one that needs a secret and has a following plugin); and, on the ext-apps `basic-server-vanillajs` example, one server whose view requests fullscreen, reports context and opens a link, one whose view registers two app tools and changes its list after the first call, one whose view reports a `time` channel and has a server tool that sets it, and one whose view needs a WebSocket service behind it. It also gets one netlist for ltspice, a small VTK dataset with time steps, and a script that writes a time series one step at a time.

**Connectors.**

| ID | Scenario | Passes when |
| --- | --- | --- |
| AQ1 | Open Extensions | Connectors are listed first, each with its logo and its state in words |
| AQ2 | Enable the fixture with no needs | It is ready at once and shows its tool count |
| AQ3 | Enable the fixture that needs an address and a sign-in | The card shows two rows. With the address empty the state says what is needed. After the address and the sign-in it is ready |
| AQ4 | Give the same fixture an address that does not answer | The state says it cannot be reached and why |
| AQ5 | Enable the fixture with a secret, and type one | The server receives it. It is not shown again. A search of the model text, the transcript, the question records and the logs finds nothing |
| AQ6 | Revoke the sign-in fixture's token, then ask the agent for something that calls its tool | The call waits. The card appears in the conversation. After signing in, the same call returns its result with no new message from the person |
| AQ7 | The same, and decline the card | The call returns a typed refusal that names the connector |
| AQ8 | Tell the agent a new address for a need marked `agent_settable` | The agent calls `set_connector_value`, the card shows the value, the server restarts with it |
| AQ9 | Ask the agent to set the secret, and to sign in for you | Both are refused |
| AQ10 | Enable the fixture with a following plugin in a running conversation, then disable it | The plugin joins and leaves with the connector, shown by the plugin-change notice. It never appears in a plugin picker |
| AQ11 | Add a server by URL that has no entry and asks for a sign-in | It appears as a connector with a monogram, and the card shows the address and a sign-in row |
| AQ12 | The Extensions list, the setup card and the card in the conversation at 390 by 844 | They fit |

**The app host.**

| ID | Scenario | Passes when |
| --- | --- | --- |
| AQ13 | The fullscreen fixture: its view requests fullscreen | A tab opens with the view. The card shows the in-tab row. The view receives `displayMode: fullscreen` |
| AQ14 | "Return to conversation", then "Open in tab" | The view is in one place each time. Input and result are replayed. No second frame exists in the DOM |
| AQ15 | With the fixture in a tab, the agent opens a second app inline, then a third | The tab app is still live and answers a tool call. Only the latest inline app is live |
| AQ16 | Leave an app in a tab untouched for longer than the registry expiry (shorten the constant for the test) | Its tool call still succeeds. An inline app left the same time returns the existing not-found |
| AQ17 | Open more pinned apps than `_REGISTRY_LIMIT` (lowered for the test) | All stay live. An inline app is still admitted |
| AQ18 | The fixture reports context, then the person types "what am I looking at?" | The turn's model text contains the reported context, and the answer uses it |
| AQ19 | The context record for a live session | Read from clio-core in a unit test. Nothing reads `record.model_context` |
| AQ20 | The app-tool fixture in a tab. Ask the agent for the change its tool makes | `search_tools` finds `app__<server>__<tool>`. The call reaches the view. The view changes on screen. The agent reports the result |
| AQ21 | The same request after closing the tab | The call returns at once saying the app is not open and naming the tool that opens it |
| AQ22 | The fixture changes its tool list after the first call | The new tool is offered without a restart, and the removed one is gone |
| AQ23 | The fixture's view calls `ui/open-link` with an `https` URL, then with `file:` | The first opens a browser page. The second is refused with a typed reason |
| AQ24 | The fixture's view requests a download | The file is in the workspace and listed as an artifact |
| AQ25 | Close the fixture's tab, then reopen it from its card | The saved tool is called again with the saved arguments, and the view mounts |
| AQ26 | With the fixture in a tab, restart CLIO and open the session | The tab is restored and replays on first focus. A replay against a server that fails shows the reason and the saved call |
| AQ27 | A surface with `clio.mcp-app.v1` holding the channel fixture and a chart bound to the same `time` path. Change time in the view | The chart moves. Nothing is sent back to the view |
| AQ28 | In the same surface, change time from the chart | The channel's tool is called once with the new value, and the view shows it |
| AQ29 | The channel fixture reports time sixty times in one second | At most ten writes reach the data model, and the last value wins |
| AQ30 | The service fixture in a tab. Read the tab's network requests | The view works over its WebSocket. Every request goes to CLIO's own address, none to a loopback port, and the token appears in none |
| AQ31 | Call the service route with another app's capability, and after the app is closed | Both are refused |

**The test grounds.**

| ID | Scenario | Passes when |
| --- | --- | --- |
| AQ32 | spice: enable it, simulate the netlist with ngspice and "show me V(out)" | `plot_waveform` renders the chart in the conversation, not in an external browser |
| AQ33 | burette: enable it, "show PDB 1CRN", open in tab, select residues, ask "what did I select?" | The structure renders in Mol*. The answer names the selected residues from the reported context |
| AQ34 | burette: with the structure in a tab, "focus the ligand and hide water" | The agent acts through the view's app tools or `render_molecular_scene`, and the view changes without reloading |
| AQ35 | paraview: enable it on a machine with no ParaView and start a conversation | The connector is ready and its tools are listed. The first scene call says no ParaView was found here and how to point the connector at one |
| AQ36 | paraview, this machine: "open <dataset> and show an isosurface of <field>" | A tab opens with the trame view. Rotating it is drawn in the browser, and no image is requested during the drag |
| AQ37 | paraview: "what is the field's range, and what did I pick?" after a pick | The answer comes from `measure_field` and `inspect_selection`. `capture` returns a PNG at the camera on screen |
| AQ38 | paraview, the cluster: two hours into a conversation, "connect to the ParaView on node08, through login, and open <dataset on node08>" | The agent sets the address and the jump host. The first attach prepares the folder through `login`, because `node08` has no package index. The tab opens with the view |
| AQ39 | During AQ38, list what is running | On the session's machine: CLIO, the front, one `ssh`. On `login`: nothing of ours. On `node08`: the node half |
| AQ40 | paraview: the `node08` that already runs a `pvserver` with a dataset loaded | The node half attaches as its client, and the tab shows that dataset without opening a file |
| AQ41 | paraview: kill the `ssh` during AQ38, then ask for a new filter | The view shows it lost its link. The call attaches again, and the scene has every earlier filter |
| AQ42 | paraview: leave the node half with no link past its idle limit (shortened for the test) | It is gone from `node08`, and its scene manifest is in its folder |
| AQ43 | paraview: point the connector at `locked` | The first scene call fails at once with a message that interactive sign-in is needed. No process is left waiting |
| AQ44 | paraview: a second attach to `node08` after AQ38 | It takes seconds and builds nothing |
| AQ45 | paraview: load a dataset whose geometry passes the remote render threshold | The view switches to remote mode and says so. Interaction still works |
| AQ46 | paraview: open the file series the fixture script keeps writing, on `node08` | The view follows the latest step with no reload. Parked on an earlier step, it stays and shows that newer steps exist. The agent's next turn knows the latest step |
| AQ47 | paraview: close the tab and reopen it. Then restart CLIO and open the session | Both times the scene returns from `node08` with its filters, representation and timestep |
| AQ48 | paraview: a surface with the app and a chart of a field over time, bound to one `time` path. Scrub the chart | The view's timestep follows. Changing time in the view moves the chart |
| AQ49 | paraview: read the agent-visible text of `open_view`, the transcript, and the tab's network requests | The node half's token is in none of them |
| AQ50 | paraview: from `login`, try the node half's port with no token, and try it from the session's machine directly | The first is refused. The second cannot connect |

**Removal.**

| ID | Scenario | Passes when |
| --- | --- | --- |
| AQ51 | Search both repositories for `use-active-mcp-app`, `availableDisplayModes: ['inline']`, `record.model_context` and "MCP sign-in required; connect this service in CLIO" | Nothing remains outside the ledger and this plan |
| AQ52 | Search clio-kit's ParaView server for `multi-clients`, `viz.html` and the old tool names, and gact-tui's dependencies for `vtk.js` and `molstar` | Nothing |
| AQ53 | Search clio-agent and gact-tui for the words ParaView, Burette and LTspice outside `catalogs/connectors/` | None in Python or TypeScript |

Local phase, run by a person and recorded in the ledger:

| ID | Scenario | Passes when |
| --- | --- | --- |
| AQ-L1 | spice on Windows with LTspice | The chart renders from an LTspice run |
| AQ-L2 | paraview on Windows and on macOS with the vendor's ParaView 6 | AQ36 and AQ47 pass |
| AQ-L3 | paraview on Ares from a laptop: address `ares-compute-08`, via `ares`, with the site's ParaView module in `prepare` | AQ38, AQ41 and AQ47 pass. Record the time of the first attach and of a later one, the time from a filter change to the updated view, and the geometry size of a typical isosurface |
| AQ-L4 | The same from Windows, with the laptop put to sleep for ten minutes in the middle | The scene is intact on return |
| AQ-L5 | paraview attached to a `pvserver` running under MPI on Ares | The tab shows the parallel session's data |

Nothing in the first four tables may be blocked. A failing scenario there is fixed, not deferred.

## Done

1. Every decision is implemented. The one-live-app hook, the inline-only display mode, the in-memory context copy, the "MCP sign-in required" failure and the old ParaView server are gone.
2. Focused tests pass, and `ruff check` and `ruff format --check` are clean on touched paths.
3. Every scenario in the first four tables passes against a freshly restarted stack, each shown with commands, handles and the screenshots read. Local-phase rows say so.
4. The cluster fixture is in the repository with one command that builds it and one that runs the ParaView scenarios against it.
5. The ledger holds every decision with its evidence and every fact listed above.
6. The site's user guide has a page on connectors (enabling one, the setup card, what the agent can change) and a page on apps (tabs, what the agent sees, reopening, putting one in a dashboard). The developer guide has a page on writing a connector entry and a following plugin.
7. The branches are pushed in clio-agent, gact-tui, clio-kit and clio-agent-marketplace, the submodule pins point at the pushed commits, and the draft PRs carry a summary and the evidence.

## Owner work beside this plan

None of these blocks the plan.

| Item | Why |
| --- | --- |
| Allow `burette-plugin.vercel.app` in the cloud environment's network access | It lets the Burette scenarios run against the hosted service. Without it the session runs Burette from source |
| Confirm the logo terms the session recorded for ParaView and Burette | They are other parties' marks |

## Working conventions

- Use the branch the cloud session is set up to push to, with the same name in every repository, and write it in the ledger header. Open draft PRs against `develop`.
- Small Conventional Commits, with test evidence in each PR description.
- Keep runtime state in `.cloud-run/`. Stop the stack and the containers when you finish a work block.
