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

## Display mode and the tab (owner: `host`)

- The `mcp_app` part and the descriptor gain `display_mode: "inline" | "fullscreen"`. The part carries the mode it was made in; the descriptor carries the record's current mode and `pinned` (true in a tab or a surface).
- `PUT …/display-mode` takes `{mode}` and returns `{display_mode, pinned}`. `fullscreen` pins the registry record (MA6); any other mode is a 422.
- **Surface placement (decided):** an A2UI surface pins its record through `PUT …/pin {pinned: bool}`, which returns `{display_mode, pinned}`. It does not use `display-mode`, so a reloaded UI never opens a tab for an app that lives in a surface. In the UI, call `registerMcpAppSurface(target, repository)` on mount and `releaseMcpAppSurface(appInstanceId)` on unmount (`mcp-app-live-set.ts`); release closes the app (MA7).
- `POST …/downloads` takes the `ui/download-file` params `{contents: [...]}` and returns `{files: [{name, path, artifact_id, artifact_skipped?}]}` (workspace-relative `path`). Refusals are 422 error envelopes whose `error.error` is the typed reason: `invalid_content`, `download_too_large`, `download_link_unsupported`, `no_workspace`, `download_name_taken`.
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

- `PUT …/model-context` keeps its shape. Storage moves to clio-core, and `record.model_context` is deleted.
- `PUT …/app-tools` takes `{tools: [{name, description, inputSchema}]}`. An empty list removes them.
- The session event `mcp_app_tool_call` carries `{session_id, app_instance_id, call_id, name, arguments}`.
- `POST …/app-tool-results/{call_id}` takes `{result: CallToolResult}` or `{error: {message}}`.
- The agent-facing name is `app__<server>__<tool>`, where `<server>` is the source namespace.

## Saved call and reopening (owner: `reopen`)

- Session state key `mcp_app_calls`, keyed by `app_instance_id`: `{server, tool, arguments, display_mode, resource_uri, label}`. The private result and the HTML are not saved.
- `POST /v1/sessions/{sid}/mcp-apps/reopen` takes `{app_instance_id}` and replays the call. It returns `{part}` with a fresh `mcp_app` part (new `app_instance_id` and `data_ref`), or `{error: {reason, detail}, saved_call}`.

## A2UI component (owner: `a2ui`)

- `clio.mcp-app.v1` takes `{server, tool, arguments, state: [{key, path, tool?}], height}`.
- It opens through the reopen path. A surface placement is pinned.

## A view's service (owner: `service`)

- A CLIO-launched server declares, in the private `_meta` of the result that opens the view, `_meta["ai.iowarp.clio/service"] = {port, token}`. The port must be loopback.
- CLIO strips the token before the result reaches the view or the model. It gives the view `service_url` through the descriptor, and in host context as `hostContext["ai.iowarp.clio/service"] = {url}`.
- The proxy route is `…/service/…`. The owner decides how the capability travels so that relative URLs and WebSocket upgrades keep it (a path segment or a cookie), and records the choice here.
