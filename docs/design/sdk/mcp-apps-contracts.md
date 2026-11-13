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
- **Refinements by `conn-be`** (all additive):
  - `GET /v1/connectors` also returns `diagnostics: [{code, message, file, entry}]` for entries that did not load, and takes `?refresh=true` to check each enabled server again. `GET /v1/connectors/{id}` returns one row. `DELETE /v1/connectors/{id}` removes a `custom` connector.
  - A need row also has `options: [str]` (a `value` need offered as a picker; empty otherwise). `plugin` is the installed plugin that follows the connector, else the entry's `plugin`, else null.
  - A disabled connector is never started: its `state` says only what is missing (`ready` when nothing is), and `tool_count` is null. `tool_count` is set only while `ready`.
  - The setup card's payload also carries `connector_name` and `logo_url`. Its interaction id is `connector:<id>`; the respond body field is `values` (secrets included; applied, never stored on the interaction). A `submit` that leaves a required row empty answers 422 `connector_setup_incomplete` with `details.missing`, and the card stays pending. The interaction's status is `answered` after submit, `denied` after decline, `cancelled` when the turn stopped.
  - `connector.changed` is broadcast to every open event stream (`session_id: ""`) with `{connector_id, sessions}`: `sessions` are the open conversations whose agent was pinned again, so the UI refetches their session agent (the plugin-change notice).
  - A plugin row (`GET /v1/plugins`, `/v1/plugins/inspect`) and each plugin of a resolution carry `follows` (the connector id, or ""). A connector with no installed following plugin is carried by one CLIO builds, id `connector-<id>`, marketplace `clio-connectors`, which also carries `follows`.
  - Errors are the usual envelope with typed codes: `connector_not_found` (404), `connection_name_taken` (409), `connector_scope_invalid`, `connector_need_unknown`, `connector_sign_in_not_a_value`, `connector_secret_not_agent_settable`, `connector_need_not_agent_settable`, `connector_value_invalid` (422).
- **Entry field `tools`** (added by `grounds`): an optional allowlist of the server's tool names the connector offers, `{"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1, "uniqueItems": true}`. Absent means every tool. A listed name the server does not have is a typed diagnostic, not a silent drop. The loader keeps only the listed tools in the connection's catalog, so neither the agent nor `search_tools` sees the others. The `burette` entry uses it to offer only the PDB-ID and Ketcher tools (plan, "Burette in detail").
- **As read by `conn-ui`** (in addition to the refinements above):
  - `scope.agents` lists agent **refs** and is never empty; the card sends refs.
  - A saved MCP connection is not a connector. The UI's "Add by URL or command" calls `POST /v1/connectors` (`{url | command, args, name, scope: "all"}`) when the form holds only an address or a command; any advanced setting (headers, SSE, an authentication mode, environment, every conversation) keeps it a raw MCP connection.
  - Every `connector.*` event refreshes the connectors, servers, tools, plugins, agents, agent resolutions and session agents, so the plugin-change notice follows `sessions`; `connector.setup.*` refreshes pending interactions.

## Display mode and the tab (owner: `host`)

- The `mcp_app` part and the descriptor gain `display_mode: "inline" | "fullscreen"`.
- `PUT …/display-mode` takes `{mode}` and returns `{display_mode}`. It also pins or unpins the registry record (MA6).
- The workbench tab kind is `mcp-app`, with fields `appInstanceId`, `dataRef`, `sessionId`, `resourceUri`, `sourceServer`, `toolName` and `label`. Tab id: `mcp-app:<appInstanceId>`.
- Live set: the session-level store `useMcpAppLiveSet` replaces `use-active-mcp-app.ts`.
- `McpAppSurface` props:
  - `placement: "inline" | "tab" | "surface"`.
  - `onModelContext?(context)`: called after a successful `ui/update-model-context`, used by MA21.
  - An imperative handle `{callAppTool(name, args) → result, sendToolInput?}`, used by MA13 and MA21.
- A surface placement pins the record through `PUT …/display-mode` with `mode: "fullscreen"`, or through `PUT …/pin {pinned}` if the owner chooses that. The choice is recorded in this file.

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
