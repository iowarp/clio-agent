# Connectors and apps in a tab: cross-stream contracts

Working seams between the parallel streams implementing [mcp-apps-surfaces-plan.md](mcp-apps-surfaces-plan.md). The plan governs; this file fixes the wire shapes several streams share, so they can build at the same time. The stream that owns a section may refine it. When it does, it edits this file in its own branch and says so in its report. The integrator reconciles the file at merge time.

All app routes are under `/v1/sessions/{sid}/mcp-apps/{app_id}` and are bound to the app by the existing capability (`data_ref` query parameter, checked by `mcp_app_routes.resolve`).

## Connectors (owner: `conn-be`, consumer: `conn-ui`)

- `GET /v1/connectors` returns `{connectors: [Connector]}`.
- A `Connector` has these fields:
  - `id`, `name`, `description`, `logo_url` (null for an entry with no logo, which shows a monogram), `source` (`catalog` | `custom`), `enabled`, `scope`, `plugin`, `tool_count`;
  - `state: {status: "ready" | "needs" | "unreachable", needs: [key], reason: str | null}`;
  - `needs: [{key, kind: address|sign-in|secret|value, label, help, required, agent_settable, default, value, has_value}]`. `value` is always null for a secret; `has_value` says whether one is set.
- `GET /v1/connectors/{id}/logo` serves the SVG.
- `PUT /v1/connectors/{id}/values` takes `{values: {key: str | null}}` and returns the `Connector` after CLIO reconnects (CN7).
- `POST /v1/connectors/{id}/enable` takes `{scope: "all" | {agents: [...]}}`. `POST /v1/connectors/{id}/disable`.
- `POST /v1/connectors` takes `{url}` or `{command, args}` and creates a `custom` connector (AQ11).
- A sign-in need uses the existing `/v1/mcp/configuration/{name}/sign-in` flow. The connector's connection name equals its `id`.
- **In-conversation card (CN5):**
  - The blocked tool call publishes a pending interaction with `kind: "connector_setup"`, using the same pending-interaction path the UI already renders for permissions and questions: `{interaction_id, connector_id, missing: [need rows], tool_name}`.
  - The UI answers at `POST /v1/sessions/{sid}/interactions/{interaction_id}/respond` with `{action: "submit", values: {...}}` or `{action: "decline"}`. A sign-in row completes through the sign-in flow and then `submit`s.
  - A decline returns the typed refusal `connector_declined` naming the connector.
- **Events:** `connector.changed {connector_id}` triggers a UI refetch. `connector.setup.requested` and `connector.setup.resolved` carry `{session_id, interaction_id, connector_id}`.
- The native tool is `set_connector_value(connector, key, value)`.
- **Connector discovery (owner: `discover`).** `offer_connector(connector, reason)` raises the same `connector_setup` interaction with payload `intent: "enable"`, plus `description`, `reason`, `agent_ref`, `agent_name`, `asks_sign_in`; `missing` holds every need but a sign-in, required and optional, and `tool_name` is `offer_connector`. Its title is "Turn on <name>", its prompt "The agent offers <name>: <reason>". The UI answers `{action: "submit", values, metadata: {scope: "all" | "agent"}}` or `{action: "decline"}`; the respond route now forwards `metadata` to connector cards. `agent` scopes the connector to `{agents: [...existing, agent_ref]}`; anything else is 422 `connector_scope_invalid`; a required row left empty is 422 `connector_setup_incomplete`. The tool's result: `{connector, connector_name, enabled, already_on, scope, state, tool_count, tools, plugin, message, reason, join_reason?, instructions?}` on Turn on, `{refusal: "connector_offer_declined", connector, connector_name, reason, message}` on Not now, `{error: "connector_offer_cancelled", outcome: "cancelled", …}` when the turn stopped, `{error: "connector_not_found", offerable}`, `{offerable: [{id, name, description}]}` with no connector.
- **Refinements by `conn-be`** (all additive):
  - `GET /v1/connectors` also returns `diagnostics: [{code, message, file, entry}]` for entries that did not load, and takes `?refresh=true` to check each enabled server again. `GET /v1/connectors/{id}` returns one row. `DELETE /v1/connectors/{id}` removes a `custom` connector.
  - A need row also has `options: [str]` (a `value` need offered as a picker; empty otherwise). `plugin` is the installed plugin that follows the connector, else the entry's `plugin`, else null.
  - A disabled connector is never started: its `state` says only what is missing (`ready` when nothing is), and `tool_count` is null. `tool_count` is set only while `ready`.
  - The setup card's payload also carries `connector_name` and `logo_url`. Its interaction id is `connector:<id>`; the respond body field is `values` (secrets included; applied, never stored on the interaction). A `submit` that leaves a required row empty answers 422 `connector_setup_incomplete` with `details.missing`, and the card stays pending. The interaction's status is `answered` after submit, `denied` after decline, `cancelled` when the turn stopped.
  - `connector.changed` is broadcast to every open event stream (`session_id: ""`) with `{connector_id, sessions}`: `sessions` are the open conversations whose agent was pinned again, so the UI refetches their session agent (the plugin-change notice).
  - A plugin row (`GET /v1/plugins`, `/v1/plugins/inspect`) and each plugin of a resolution carry `follows` (the connector id, or ""). A connector with no installed following plugin is carried by one CLIO builds, id `connector-<id>`, marketplace `clio-connectors`, which also carries `follows`.
  - Errors are the usual envelope with typed codes: `connector_not_found` (404), `connection_name_taken` (409), `connector_scope_invalid`, `connector_need_unknown`, `connector_sign_in_not_a_value`, `connector_secret_not_agent_settable`, `connector_need_not_agent_settable`, `connector_value_invalid` (422).
- **Entry field `tools`** (added by `grounds`): an optional allowlist of the server's tool names the connector offers, `{"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1, "uniqueItems": true}`. Absent means every tool. A listed name the server does not have is a typed diagnostic, not a silent drop. The loader keeps only the listed tools in the connection's catalog, so neither the agent nor `search_tools` sees the others. The `burette` entry uses it to offer only the PDB-ID and Ketcher tools (plan, "Burette in detail").
- *Amended* **Connector values per conversation (owner: `conn-values`, plan CN7 amended, ledger CV).** All additive:
  - Session metadata key `connector_values: {<connector id>: {<need key>: str}}` holds a conversation's own `address` and `value` needs; never secrets or sign-ins. It is server-owned: it is checked on every read, and a value a client patched in that fails the check is dropped (trace `connector_overlay_value_refused`). Clients do not write it; they answer a card.
  - `GET /v1/connectors/{id}?session_id=<sid>` returns the `Connector` as that conversation sees it: `needs[].value` is the value in use there, and each `address`/`value` row adds `default_value` (the person's default, or null) and `conversation` (whether this conversation set its own). It also adds `session_id`. 404 for an unknown conversation.
  - A connector card in a conversation (setup or offer) answers `{action: "submit", values, metadata: {save_as_default: [need key], scope?}}`. `address`/`value` keys not in `save_as_default` are this conversation's; keys in it become the person's defaults and the conversation's own value for them is cleared. Secrets are always the person's. `save_as_default` that is not a list of strings is 422 `connector_default_invalid`; a key with no value given is 422 `connector_default_unknown`.
  - A call waiting on a setup card whose answer gave the conversation values of its own returns the typed refusal `connector_values_set_for_conversation` (call again; the next call reaches the conversation's server).
  - `set_connector_value` writes the conversation's values. Its result adds `scope: "conversation"`; `message` says "in this conversation"; its row shows "Applies to: This conversation". With no conversation it returns `connector_no_conversation`.
  - `PUT /v1/connectors/{id}/values` (Extensions) still sets the defaults.
  - `connector.changed` after a conversation's change carries `sessions: [that sid]`.
- **As read by `conn-ui`** (in addition to the refinements above):
  - `scope.agents` lists agent **refs** and is never empty; the card sends refs.
  - A saved MCP connection is not a connector. The UI's "Add by URL or command" calls `POST /v1/connectors` (`{url | command, args, name, scope: "all"}`) when the form holds only an address or a command; any advanced setting (headers, SSE, an authentication mode, environment, every conversation) keeps it a raw MCP connection.
  - Every `connector.*` event refreshes the connectors, servers, tools, plugins, agents, agent resolutions and session agents, so the plugin-change notice follows `sessions`; `connector.setup.*` refreshes pending interactions.

## Display mode and the tab (owner: `host`)

- The `mcp_app` part and the descriptor gain `display_mode: "inline" | "fullscreen"`. The part carries the mode it was made in; the descriptor carries the record's current mode and `pinned` (true in a tab or a surface).
- `PUT …/display-mode` takes `{mode}` and returns `{display_mode, pinned}`. `fullscreen` pins the registry record (MA6); any other mode is a 422.
- **Surface placement (decided):** an A2UI surface pins its record through `PUT …/pin {pinned: bool}`, which returns `{display_mode, pinned}`. It does not use `display-mode`, so a reloaded UI never opens a tab for an app that lives in a surface. In the UI, call `registerMcpAppSurface(target, repository)` on mount and `releaseMcpAppSurface(appInstanceId)` on unmount (`mcp-app-live-set.ts`); release closes the app (MA7).
- `POST …/downloads` takes the `ui/download-file` params `{contents: [...]}` and returns `{files: [{name, path, artifact_id, artifact_skipped?}]}` (workspace-relative `path`). Refusals are 422 error envelopes whose `error.error` is the typed reason: `invalid_content`, `download_too_large`, `download_link_unsupported`, `no_workspace`, `download_name_taken`.
- **Sandbox origin (decided):** `sandbox_url` (and so `service_url`) is on `s<sha256(server namespace)[:16]>.mcp-apps.localhost:<CLIO port>`, one host per originating server. That host serves only the app's sandbox document and its service route; every other route answers `403 sandbox_origin_refused`. The view frame has `allow-scripts allow-same-origin allow-forms`. A browser that reached CLIO by a non-loopback name gets its sandbox host from `gact.mcp_apps.sandbox_host` (`*.apps.example.lan` per server, or one plain name); without one the descriptor answers `409 sandbox_host_unconfigured`.
- The workbench tab kind is `mcp-app`, with fields `appInstanceId`, `dataRef`, `sessionId`, `resourceUri`, `sourceServer`, `toolName` and `label` (`McpAppTabTarget` in `workbench-shared.ts`). Tab id: `mcp-app:<appInstanceId>`. Open one with the window event `OPEN_MCP_APP_TAB_EVENT` (detail: `McpAppTabTarget`) or `openMcpAppInTab(target, repository)`.
- Live set: the zustand store `useMcpAppLiveSetStore` in `web/src/components/clio/mcp-app-live-set.ts` replaces `use-active-mcp-app.ts`. `useMcpAppLiveSet(sessionId, messages, repository)` keeps a conversation's part; `useMcpAppPresence(appInstanceId, sessionId)` gives `{placement: inline|tab|surface|ended, latest}`.
- `McpAppSurface` props (`mcp-app-surface.tsx`):
  - `placement: "inline" | "tab" | "surface"`. A surface answers every display-mode request with `inline` and advertises only `inline`.
  - `onModelContext?(context)`: called after a successful `ui/update-model-context`, with the params the view sent. Used by MA21.
  - `onAppInitialized?(appCapabilities)`: after `ui/notifications/initialized`, with the view's declared capabilities (MA11 reads `tools`).
  - `onViewNotification?(method, params)`: a view notification the host does not consume itself (`notifications/tools/list_changed`, MA11).
  - `onDisplayModeChange?(mode)`, `onRecordedDisplayMode?(mode) → boolean`, `onEnded?()`, `onBrowserPage?`, `onDownloaded?`, `actions` (header controls).
  - An imperative handle (`forwardRef`, `McpAppSurfaceHandle`): `{callAppTool(name, args) → Promise<result>, sendToolInput(args), request(method, params) → Promise<result>}`. Requests wait for the view's initialization and have no timeout (rule 6); they reject when the frame unloads. Used by MA11, MA13 and MA21.

## App context and app tools (owner: `ctx-tools`)

- `PUT …/model-context` keeps its shape. Storage moves to clio-core: `session.metadata["mcp_app_context"][app_instance_id] = {server, tool, resource_uri, context, sha256, bytes, reported_at}`. `record.model_context` is deleted. Over 128 KiB is a 413.
- `PUT …/app-tools` takes `{tools: [{name, description, inputSchema, annotations?}], holder?}` and returns `{offered: [agent names], skipped: [{name, reason}]}`, or `{offered: [], reason, message}` when the list is over a limit (64 tools, 64 KiB per schema). An empty list removes them. `holder` names the mount of the view that posted; an empty list from a mount other than the one that last registered tools is ignored. Registering re-publishes the app's waiting calls.
- The session event `mcp_app_tool_call` carries `{session_id, app_instance_id, call_id, name, arguments}`. It is transient (live only, never replayed); `name` is the view's own tool name.
- `POST …/app-tool-results/{call_id}` takes `{result: CallToolResult}` or `{error: {message}}`. 404 when no call of this app waits under that id.
- The agent-facing name is `app__<server>__<tool>`, where `<server>` is the source namespace. A call when no surface holds the app returns at once, naming the opening tool.
- `ui/initialize` answers with `hostCapabilities.updateModelContext = {text: {}, structuredContent: {}}` and `message = {text: {}}` (draft spec).

## Looking at a view and comments on it (owner: `comments`)

- `PUT …/shown` takes `{holder, shown: bool}` and returns `{shown}` (whether any mount still holds the view). Each mount sends it once the view is initialized and again with `shown: false` when its frame unloads; an unload removes only its own `holder`. Showing re-sends every look still waiting on the app. A look also needs a window subscribed to the session's events, else it is refused (`mcp_app_view_not_shown`).
- The session event `mcp_app_look` carries `{session_id, app_instance_id, call_id, max_side}`. It is transient. The surface asks CLIO's sandbox document for a snapshot with the request `clio/view-snapshot` `{scale, max_side, background}` (host ids, answered by the sandbox document, never forwarded to the view) and posts the answer to `POST …/looks/{call_id}`: `{image: <PNG data URL>, width, height, scale, painted: {canvas, image, svg, video, text}, skipped: [{what, reason, count?}]}` or `{error: {reason, message}}`. 404 when no look of this app waits; 422 when the image is not a PNG.
- The native tool is `look_at_view(view)`. Its result is a `clio.workspace_image.v1` descriptor plus `view`, `pixels`, `context_lines`, `coverage`, `artifact_id` and `model_text`; the model reads the image, then `model_text`. Refusals are `{ok: false, reason, text}` with `mcp_app_view_none`, `mcp_app_view_ambiguous`, `mcp_app_view_not_shown`, `mcp_app_view_closed` or the snapshot's own reason.
- A comment on a view is a `comment_session` part with `kind: "mcp-app"`. Each mark is a `region` anchor (`rect` normalized to the view, `natural_width`/`natural_height` the view's CSS size) whose `source` holds `app: {app_instance_id, server, tool, title, resource_uri}`, `context` (the `ui/update-model-context` params last reported, or null), `captured_at`, and optionally `point: [x, y]`, `snapshot_refused: {reason, message}` and `snapshot_skipped`. `images` are the overview (no label) and a crop per mark at twice its size.
- `McpAppSurface` takes `comments` (the frame shows the Comments icon and the region layer).

## Saved call and reopening (owner: `reopen`)

- Session state key `mcp_app_calls` (the session's own clio-core record, `Session.metadata`), keyed by `app_instance_id`: `{server, tool, arguments, display_mode, resource_uri, label, saved_at}`, plus `reopened_from` on a call saved by a reopen and `unsaved_reason: "arguments_not_saved"` (with `arguments: null`) when the arguments exceed 64 KiB. `tool` is the bare tool name; the executor name is `<server>_<tool>`. The private result and the HTML are not saved.
- Every admitted view saves its call (`gact/mcp_app_calls.py::record_opening_call`, called by the MCP App observer). `PUT …/display-mode` also sets `display_mode` on the saved call and on the calls it was reopened from.
- A server may declare what a reopen replays. The result that opens the view carries, in its private `_meta`, `"ai.iowarp.clio/reopen": {"arguments": {...}}`. CLIO saves those arguments instead of the call's, and records `arguments_from: "server_reopen_declaration"` on the saved call.
  - ParaView's `open_view` declares `{"scene": "<id>"}`, so a call that started a fresh scene (`new: true`) reopens the scene the person built, not an empty one.
  - The declaration is taken off the result at admission (`MCPAppRegistry.register`, `gact/mcp_app_reopen_declaration.py`), like the service declaration, so the view, the transcript and the model never see it.
  - A declaration whose `arguments` is not an object is ignored. The call's own arguments are saved, with the typed reason `reopen_declaration_invalid` (stream audit `mcp_app_reopen_declaration`).
- `POST /v1/sessions/{sid}/mcp-apps/reopen` takes `{app_instance_id}` and replays the call through the gated executor path (the session's permission rules apply). It answers 200 with `{part, saved_call}`: a fresh `mcp_app` part (new `app_instance_id` and `data_ref`, `display_mode` from the saved call, `metadata.stream_source: "reopen"`) and the new view's own saved call. A failed replay answers 200 with `{error: {reason, detail}, saved_call}`; `reason` is one of `no_saved_call`, `arguments_not_saved`, `tool_unavailable`, `tool_has_no_view`, `permission_denied`, `call_failed`, `call_cancelled`, `tool_error`, `view_not_opened`. `call_cancelled` answers a replay whose request went away (or that CLIO shut down under) before the service answered: a call outside a turn is bound to its request (`gact/view_call_cancel.py`), and it is retryable. An unknown session is 404; a missing `app_instance_id` is 422. The replay does not append a card to the transcript: the caller mounts the returned part. It also takes `{call: {server, tool, arguments, display_mode?}, save?}`: a call the caller holds, opened as a first opening (`reopened_from` empty). With `save` (default true) the new view's call is saved and answered as `saved_call`; the A2UI component sends `save: false`, so its call is not saved (the surface holds it) and `saved_call` echoes the call. Every answer that opens a view also lists `server_tools`, the server's tools the view may call, without the server's prefix. A malformed `call` or a non-boolean `save` is 422.
- `GET /v1/sessions/{sid}/mcp-apps/{app_id}/live?data_ref=` answers `{live: bool}` (404 only for an unknown session) without touching the record's idle time, so a card can tell a closed view from a live one without a failed descriptor request.

## A2UI component (owner: `a2ui`)

- `clio.mcp-app.v1` takes `{server, tool, arguments, state: [{key, path, tool?}], height}`.
  - `server` is the tool namespace (the `mcp_app` part's `source_server`), `tool` the server's own tool name. Both match `^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$`. At most 16 channels; keys and paths are distinct.
  - The producer refuses a `server`/`tool` that is not a view-opening tool with `a2ui_mcp_app_tool_not_found`, listing the server's view tools. A channel `tool` is not checked there, since it may be a view app tool.
- It opens through the reopen route's call form: `POST /v1/sessions/{sid}/mcp-apps/reopen` with `{call: {server, tool, arguments}, save: false}` (one replay path, `mcp_app_calls.replay_call`). The call is not saved in `mcp_app_calls` and no `mcp_app` part is added to the transcript. The answer carries `server_tools`, which the bridge uses to send a channel's tool to the server or to the view.
- A surface placement is pinned with `PUT …/pin {pinned: true}` through the live set's `registerMcpAppSurface`. The last unmount closes the app through `releaseMcpAppSurface`.
- State bridge (MA21, MA22):
  - View to surface: a `structuredContent` key of `ui/update-model-context` that a channel names is written to the channel's path, at most once per 100 ms per channel, with the latest value.
  - At a `/selection/<key>` path the value is written as `SelectionState {field: <channel key>, values: [value] (or the list), source: <component id>}`, and read back as the one value (or the list). A cleared selection calls nothing.
  - Surface to view: a path change the bridge did not write, whose value the view does not already show, calls the channel tool once with `{[key]: value}`. A name in `server_tools` goes to `POST …/tools/call`, and the view then receives `ui/notifications/tool-input` and `ui/notifications/tool-result` for it, as in the specification's interactive phase. Any other name goes to the view as an app tool (`callAppTool`).
  - The host's `McpAppSurfaceHandle` gains `sendToolResult(result)` for that notification.

## A view's service (owner: `service`)

Final shape (MA23). The ParaView front and any other server with a live view use it as written here.

**Declaring a service.**

- The result of the tool call that opens the view carries, in its private `_meta`, `"ai.iowarp.clio/service": {"port": <int 1..65535>, "token": "<single-line string, at most 4096 characters>"}`.
- The service listens on `127.0.0.1:<port>` on the machine CLIO runs on. CLIO always connects to `127.0.0.1`; there is no host field.
- CLIO carries it only when the view's server is a stdio server CLIO launched: the source namespace has a `stdio` spec on the session's tool executor (`_clio_namespace_specs`). A URL server's declaration is refused with `service_server_not_launched`. A port equal to CLIO's own listener is refused with `service_port_is_clio`. A malformed declaration is refused with `service_declaration_invalid`.
- The service must refuse every HTTP request and WebSocket upgrade that lacks `Authorization: Bearer <token>`. CLIO adds that header on the way through; the browser, the view, the transcript and the model never see the token or the port.
- A declaration in any other result (a tool the view calls through `tools/call`) is removed and ignored, with reason `service_declaration_ignored`.

**What CLIO strips.** `MCPAppRegistry.register` takes the declaration off the stored result, so the descriptor's `tool_result` and the replayed `ui/notifications/tool-result` never hold it. `call_tool_result_to_observer` and the model text never carried `_meta`.

**The route.** `/v1/sessions/{sid}/mcp-apps/{app_id}/service/{data_ref}/{path}` for any HTTP method and for WebSocket upgrades.

- The capability travels as the `data_ref` path segment, so relative URLs and WebSockets opened from a page under the route keep it. It is checked by the same `resolve` (`MCPAppRegistry.get`: session, app id and constant-time `data_ref` match) as the other app routes. A wrong session, app or capability, or a closed app, gets the same non-oracular 404 `not_found`.
- `…/service/{data_ref}` without the slash redirects (307) to `…/service/{data_ref}/`.
- The service receives the path and query below the prefix, unchanged: `…/service/{data_ref}/a/b?q=1` reaches `http://127.0.0.1:<port>/a/b?q=1`.
- A service's pages use relative URLs. An absolute path (`/static/x.js`) leaves the route. A redirect to an absolute path, or to the service's own `127.0.0.1`/`localhost` address, is rewritten under the route.
- CLIO drops the browser's `Authorization`, `Cookie`, `Origin` and `Referer`, and the service's `Set-Cookie` and `Access-Control-*`.
- A view is a sandboxed frame (`Origin: null`). CLIO answers its CORS: `Access-Control-Allow-Origin: null` on responses, and preflights for this route answered by CLIO without reaching the service. The token-less origin guard does not apply its state-changing-method check to this route; the capability in the path is the guard.
- WebSocket subprotocols the browser offers are offered to the service, and the service's choice is returned. Text and binary frames are relayed both ways. There is no read timeout and no keep-alive ping on an open connection; only connecting has a 10 second limit.
- Errors: 403 with the typed reasons above; 502 `service_unreachable` when nothing answers on the port. A refused WebSocket is rejected before the upgrade (HTTP 403). An unreachable one is accepted and closed with 1011 `service_unreachable`.

**What the view is told.**

- The descriptor gains `service_url` (an absolute URL ending in `/`, on the sandbox origin, or null), `service_refusal` (`{reason, message}` or null) and `csp_refused` (`[{directive, source, reason: "csp_loopback_source_refused"}]`).
- The host puts `hostContext["ai.iowarp.clio/service"] = {url}` in `ui/initialize` and in every `ui/notifications/host-context-changed`, only when `service_url` is the app's own route on the sandbox origin.
- A view reaches its service only at that URL. The sandbox CSP drops every loopback source a resource declares (`127.0.0.0/8`, `localhost`, `*.localhost`, `::1`, `0.0.0.0`) and adds the app's service route, `http(s)://<sandbox origin>/…/service/{data_ref}/` and its `ws(s)://` form, to `connect-src` and `frame-src`.
- Frames nested in the view inherit its sandbox, so a framed service page is also `Origin: null` and is served the same way.

**Closing.** Closing the app (`DELETE …/mcp-apps/{app_id}`), deleting the session or shutting CLIO down removes the record, so the route answers 404, and ends the app's open WebSockets with close code `4410` and reason `mcp_app_closed`, and its in-flight HTTP responses. A WebSocket whose record expired closes at its next frame.

**Where the browser is.** The service URL is on the sandbox origin, so it works wherever the sandbox works: the browser never addresses the service's port.

## A tool's path arguments (owner: `fix2`)

A server declares which arguments of a tool are paths, and on which machine they live, in the private `_meta` of the tool definition (the `tools/list` entry, not a result):

```json
"_meta": {
  "ai.iowarp.clio/paths": {
    "path": {"machine": "remote", "access": "read", "host": "node08"},
    "out":  {"machine": "local",  "access": "write"}
  }
}
```

- `machine`: `local` (the machine CLIO runs on), `remote` (another machine the server reaches; `host` optionally names it for display, at most 255 characters, one line) or `none` (path-shaped but not a file, such as a name inside a dataset).
- `access`: `read` (the default) or `write`.
- An argument not declared keeps CLIO's existing reading (its own contract table for built-in tools; a path-named argument of an unknown tool is `external_input_contract_unknown`).
- A malformed declaration, or a malformed entry, is ignored with the log reason `path_declaration_invalid`; the call is never refused for it.
- A server whose machine is a setting (an SSH host) computes the declaration when it lists its tools. CLIO restarts the server when that setting changes, so the listing names the current machine.

**What CLIO does with it** (`clio_agent.tools.tool_path_contract`, bound per call by the execution boundary):

| Declared | Provenance (`TransformRecord.used`) | Tool row | Sandbox note on a confined server's failed call |
| --- | --- | --- | --- |
| `local` `read` | Inside the workspace: the usual hash/stat edge. Outside: `external:<path>`, note `external_input_not_hashed` | Input link block | No |
| `local` `write` | Not an input | Unchanged | Yes, when the path is outside the fence's territory and the person could write there (hedged `possible`, naming the folder); also a `prevented`/`detected` policy violation |
| `remote` `read` | On success, `remote:<host>:<path>` (`remote:<path>` without a host), evidence `schema-arg`, note `remote_input_not_observed`; never resolved, read or hashed here | Unchanged, never `degraded` | No |
| `remote` `write`, `none` | Nothing | Unchanged | No |

The sandbox note for a confined stdio server's failed call never reads the error's text: it needs a confined child that served the call and a local write target outside the territory, from the declared `local` `write` arguments or the designated output arguments (`output_path`, `out`, …).

