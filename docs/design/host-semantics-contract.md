# Host semantics contract (HS1 to HS5)

Status: wire contract, Wave 0. Owner: clio-agent (W3). Readers: gact-tui (W12), plugin
authors (W14 and any other).

Five generic capabilities CLIO gives any MCP server that a plugin declares. None of them
names a particular server or plugin. A server that ignores all five still works with
CLIO and with every other MCP host; these only add what CLIO can show or hand over.

Terms used below:

- **Plugin**: an Agent Plugins 1.0.0 folder with `plugin.json` and, optionally,
  `mcp.json`. CLIO settings live under `plugin.json` `extensions["ai.iowarp.clio"]`.
- **Server**: one entry of the plugin's `mcp.json` `mcpServers`. A server CLIO
  **launches** is a `stdio` entry.
- **Namespace**: the gateway prefix of the server's tools in a session (the `ns` in
  `ns_tool`). The host uses it to route calls; a server never needs to know it.

## O9: a plugin's own server needs no connector entry

Settled by experiment on Ares (fixture `tests/fixtures/plugins/own_server/`). A plugin
declares its server in `mcp.json` beside `plugin.json`:

```json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
  "mcpServers": {
    "echo": {
      "type": "stdio",
      "command": "python3",
      "args": ["${PLUGIN_ROOT}/server.py"],
      "env": {"OWN_SERVER_DATA": "${PLUGIN_DATA}"}
    }
  }
}
```

`clio-agent plugin install` then `plugin trust` is enough. CLIO spawns the server from the
installed snapshot, `${PLUGIN_ROOT}` is the snapshot folder and `${PLUGIN_DATA}` the
plugin's persistent data folder. No `catalogs/connectors/` entry and no new manifest
field is needed. The server command does not go in `plugin.json`; Agent Plugins puts it
in `mcp.json`.

## Manifest additions

All new fields are in `extensions["ai.iowarp.clio"]`. Unknown fields stay fatal, as
today.

```json
{
  "extensions": {
    "ai.iowarp.clio": {
      "catalogs": ["<a2ui catalog id>"],
      "tabs": [
        {
          "id": "status",
          "title": "Status",
          "icon": "activity",
          "server": "echo",
          "resource": "a2ui://status/tab"
        }
      ],
      "accounts": [
        {"provider": "globus", "scopes": ["<scope string>"]}
      ],
      "elicitation": {
        "loopback": true,
        "origins": ["https://signin.example.org"]
      }
    }
  }
}
```

| Field | Rule | Fatal when |
| --- | --- | --- |
| `tabs[].id` | `[a-z0-9][a-z0-9-]{0,31}`, unique in the plugin. The host id is `<plugin>/<id>` | Missing, bad shape, duplicate |
| `tabs[].title` | Text, 1 to 40 characters | Missing or empty |
| `tabs[].icon` | A Lucide icon name. An unknown name renders a generic icon | Not text |
| `tabs[].server` | A server name in this plugin's `mcp.json`. May be omitted when the plugin has exactly one server | Unknown, or omitted with several servers |
| `tabs[].resource` | An `a2ui://` URI | Other scheme |
| `accounts[].provider` | One of `github`, `google_drive`, `globus`, `ndp` | Unknown provider, duplicate |
| `accounts[].scopes` | List of scope strings, may be empty | Not a list of text |
| `elicitation.loopback` | Boolean, default `false` | Not a boolean |
| `elicitation.origins` | List of `https://host[:port]` origins, no path | Other scheme, a path, a wildcard |

`catalogs` is unchanged. HS1 uses it as the server's producible set.

## HS1 and HS1a: A2UI from a server

### The client capability (HS1a)

CLIO advertises A2UI as an MCP extension in its client capabilities. Under protocol
revision 2026-07-28 the MCP SDK stamps the client capabilities, including `extensions`,
into every request's `_meta["io.modelcontextprotocol/clientCapabilities"]`, so a server
reads it **per request**:

```json
"extensions": {
  "org.a2ui/a2ui": {
    "versions": ["v0.9", "v0.9.1"],
    "clientCapabilities": {
      "v0.9": {"supportedCatalogIds": ["<catalog id>", "..."]}
    }
  }
}
```

- The identifier is `org.a2ui/a2ui`. The A2UI project publishes no MCP extension
  identifier yet. This one follows MCP's reverse-DNS `vendor/name` rule for the A2UI
  project's domain. If A2UI publishes one, CLIO advertises both.
- `clientCapabilities` is the A2UI v0.9.1 client-capabilities object, verbatim, as the
  A2UI-over-MCP guide places it under `capabilities.a2ui`.
- `supportedCatalogIds` is the set of catalogs the receiving server's own plugin declares
  in `catalogs`. A server with no plugin, or a plugin with no `catalogs`, gets no A2UI
  extension entry. CLIO never advertises `1.0`.
- A server must send A2UI only on a request whose stamped capabilities carry this
  extension, and must always send the full text result beside it. It never infers
  support from the client's name or caches it for the process.

### Recognising a surface

A content item is an A2UI payload when it is one of:

- a tool-result content item `{"type": "resource", "resource": {...}}` whose embedded
  resource has `mimeType: "application/a2ui+json"`;
- an item of a `resources/read` result's `contents` with that `mimeType` (tabs, HS3).

The item's `text` is JSON: one A2UI server-to-client message object or an array of them,
applied in order as one batch. `blob` is refused. `resource_link` items are never
followed. The `uri` should be `a2ui://...`; CLIO does not require it.

A tool result may carry several A2UI items. Each is its own batch, in content order.

### Validation and refusal

Each batch is validated as CLIO validates any surface, plus:

| Check | Typed reason |
| --- | --- |
| Every message `version` is `v0.9` or `v0.9.1` | `a2ui_version_unsupported` (a `1.0` payload lands here) |
| `text` is JSON of the right shape, no `blob` | `a2ui_payload_invalid` |
| `createSurface.catalogId` is installed | `a2ui_catalog_unknown` |
| `createSurface.catalogId` is in the server's plugin `catalogs` | `a2ui_catalog_not_producible` |
| An update or delete names a surface this same server created | `a2ui_surface_not_owned` |
| A create reuses a live surface id owned by someone else | `a2ui_surface_owner_conflict` |
| Everything else in the existing validator | `a2ui_validation_failed` |

A refused batch changes nothing. The tool result still reaches the model as text. CLIO
records the reason on the session and publishes `a2ui.server_surface.refused` (below).

### What the model sees

The model never receives A2UI JSON. Each recognised item is replaced in the model's text
by `[A2UI surface <surfaceId> shown to the user]`, or by
`[A2UI payload refused: <reason>]`. An item annotated `audience: ["user"]` is handled the
same way; the annotation is honoured, not required.

### Storage and the owner

A surface from a tool result is stored like any surface: a transcript part of type
`a2ui` in the conversation, placed where the tool result is. It gains an owner, which is
kept on the part's `metadata.owner` and on the surface record:

```json
"owner": {"kind": "server", "plugin": "<plugin id>", "server": "<server name>", "namespace": "<ns>"}
```

A surface with no `owner` is CLIO's own, and keeps today's behaviour. The owner is fixed
at `createSurface` and never changes. Replay of an old surface whose server is gone
renders it read-only (actions refused with `a2ui_server_unavailable`).

A task-augmented tool call (MCP tasks) applies A2UI from its terminal result, when the
task completes. Progress notifications carry no A2UI.

## HS2: actions on a server's surface go to that server

The client sends the same envelope as for any surface, to the same route
`POST /v1/sessions/{sid}/a2ui/actions`:

```json
{"version": "v0.9.1",
 "action": {"name": "refresh", "surfaceId": "s1", "sourceComponentId": "btn",
            "timestamp": "...", "context": {"row": "a", "token": "..."}}}
```

When the surface has a server owner:

1. `data_source/login/<provider>` is a host action, not a server call (HS4). CLIO fills
   its context (`clio_id`, `workspace_id`) itself; the server sends an empty context.
2. Any other `name` is the server's tool name (without the namespace). CLIO resolves
   `<namespace>_<name>` on the owner and refuses with `a2ui_server_tool_not_app_visible`
   (403) unless the tool exists and its `_meta.ui.visibility` is absent or includes
   `"app"`. A tool listed with visibility `["app"]` only is never shown to the model.
3. CLIO sends `tools/call` with `arguments` = the action's `context`, verbatim. No model
   turn runs. The call is recorded as a view call (it never splits an open turn).
4. If the call is task-augmented, CLIO follows the task. An `input_required` becomes a
   question in the session, like any task input; its answer goes back to the task.
5. A2UI in the result is applied as in HS1, but only to surfaces this server owns. New
   surfaces it creates join the same place (conversation or tab).

The response is HTTP 200:

```json
{"status": "delivered", "destination": "server", "action_id": "...",
 "tool": "refresh", "outcome": "applied", "surface_ids": ["s1"]}
```

`outcome` is `applied` (A2UI changed something), `no_update` (no A2UI in the result),
`task_pending` (the task is still running; changes arrive as events), or `tool_error`
(the result had `isError`; `error_text` holds its text). Refusals are typed HTTP errors:
`a2ui_server_tool_not_app_visible` (403), `a2ui_server_unavailable` (503, the owner is
not mounted in this session), `a2ui_surface_not_owned` and the HS1 reasons (422).
Duplicate submissions are deduplicated as today.

The context is opaque to CLIO. A server that needs to bind an action to what it rendered
(for example a single-use token) puts that in the context it renders.

## HS3: plugin tabs

A tab is listed for a session when the session's agent mounts the tab's plugin. Its
server runs in that session's server set.

```text
GET  /v1/sessions/{sid}/plugin-tabs
     -> {"tabs": [{"id": "<plugin>/<tab>", "plugin": "...", "title": "...",
                   "icon": "...", "server": "...", "resource": "a2ui://...",
                   "available": true, "reason": ""}]}
POST /v1/sessions/{sid}/plugin-tabs/{plugin}/{tab}/open
     -> {"tab": {...}, "surfaces": [<surface wire>, ...], "revision": 3}
POST /v1/sessions/{sid}/plugin-tabs/{plugin}/{tab}/close
     -> {"closed": true}
POST /v1/sessions/{sid}/plugin-tabs/{plugin}/{tab}/actions
     body and response as HS2
```

- `open` sends `resources/read` for the tab's resource and, when the server declares
  `resources.subscribe`, `resources/subscribe`. The read result is the tab's whole state:
  its A2UI items replace every surface the tab held. The first batch must start with
  `createSurface`.
- On `notifications/resources/updated` for that URI, CLIO reads again and replaces the
  tab's surfaces. CLIO does not poll.
- `close` drops the tab's surfaces and, when no session still has the tab open, sends
  `resources/unsubscribe`.
- Tab surfaces are not part of the transcript and are not seen by the model. They live
  in memory for the open tab. Actions on them follow HS2; their results are applied to
  the tab.
- `available: false` with a typed `reason` (`plugin_tab_server_unavailable`,
  `plugin_tab_resource_failed`) when the server is not mounted or the read failed. A read
  that returns refused A2UI opens the tab empty with the HS1 reason.

## HS4: account grants

A plugin's `accounts` names a provider and the scopes its server needs.

- **Consent.** Each declared scope is added to that provider's sign-in request. For
  Globus, a plugin's scopes are requested beside the transfer scope and `offline_access`.
  Adding a scope makes an existing sign-in that lacks it count as needing consent again.
- **Grant file.** For each declared provider and each server CLIO launches from the
  plugin, CLIO writes the user's grant to a private file and sets
  `CLIO_ACCOUNT_<PROVIDER>_FILE` (provider upper-cased, `google_drive` becomes
  `GOOGLE_DRIVE`) in the server's environment. `${CLIO_ACCOUNT_<PROVIDER>_FILE}` is also
  expanded in the server's `args` and `env` values in `mcp.json`, so a plugin can hand the
  path to a server under the server's own setting name.
- The variable is always set when declared. While the user has no grant, the file does
  not exist.
- The file is in a directory only the user can read (mode 0700, file 0600 on POSIX; the
  user's ACL on Windows), under CLIO's state root. It is written atomically (write, then
  rename). CLIO rewrites it when it refreshes the token, at least 5 minutes before
  `expires_at`, and deletes it when the user disconnects the account.
- The token is never put in the environment or in tool arguments.

Grant file, version 1:

```json
{
  "version": 1,
  "provider": "globus",
  "written_at": "2026-07-28T12:00:00Z",
  "tokens": [
    {"resource_server": "transfer.api.globus.org", "access_token": "...",
     "token_type": "Bearer", "expires_at": "2026-07-28T14:00:00Z",
     "scopes": ["urn:globus:auth:scope:transfer.api.globus.org:all"]}
  ]
}
```

One entry per resource server the grant covers (Globus issues one token per resource
server; other providers have one entry with `resource_server` empty). The server picks the
entry whose `scopes` hold what it needs, and re-reads the file when it changes. Refresh
tokens are never written.

- **Missing grant.** A server that finds no file, or no entry with its scope, shows an
  A2UI surface with an action named `data_source/login/<provider>` and an empty context.
  CLIO routes that click to its own sign-in (HS2 step 1), then writes the file. The
  server's text result says the account is not connected.

## HS5: URL elicitation origins

For URL-mode elicitation (`elicitation/create` with `mode: "url"`), from a tool call or
from a task's `input_required`:

- Every server: `https` origins in `tools.mcp.elicitation.url_trusted_origins` are
  accepted, as today.
- A server of a plugin that names `elicitation.origins`: those `https` origins are
  accepted too, from that server only.
- A server CLIO launches, of a plugin with `elicitation.loopback: true`:
  `http://127.0.0.1:<port>` and `http://localhost:<port>` are accepted, from that server
  only. Any port; no other host, no `[::1]`, no `https` on loopback needed.
- Everything else is refused, with the existing reasons:
  `elicitation_url_insecure_scheme`, `elicitation_url_untrusted_origin`,
  `elicitation_url_not_declared`. Loopback from a remote (HTTP) server is
  `elicitation_url_insecure_scheme`.

The question CLIO shows carries where the origin came from, so the UI can say it:

```json
"metadata": {"url": "http://127.0.0.1:41234/ask/...", "origin": "http://127.0.0.1:41234",
             "origin_trust": "plugin_loopback", "plugin": "<plugin id>", "server": "<server>"}
```

`origin_trust` is `configured`, `plugin_origin` or `plugin_loopback`.

## Events for the UI

On `GET /v1/sessions/{sid}/events` (SSE):

| Event | Payload |
| --- | --- |
| `a2ui.surface.upserted` | The surface wire, now with `owner` when server-owned |
| `a2ui.surface.deleted` | `{"surface_id"}` |
| `a2ui.server_surface.refused` | `{"plugin", "server", "tool", "uri", "reason", "detail"}` |
| `plugin_tab.surface.upserted` | `{"tab_id", "revision", "surface": <surface wire>}` |
| `plugin_tab.surface.deleted` | `{"tab_id", "revision", "surface_id"}` |
| `plugin_tab.refreshed` | `{"tab_id", "revision", "surface_ids"}` after a re-read replaced the tab |
| `plugin_tab.unavailable` | `{"tab_id", "reason", "detail"}` |

A tab's `revision` increases on every change; a client that sees a gap calls `open`
again.

## Decisions to confirm

1. The A2UI extension identifier `org.a2ui/a2ui` and its settings shape.
2. Tabs are per session: a tab shows when the session's agent mounts the plugin, because
   servers run in a session's server set. A tab that shows in every session needs the
   plugin in the default agent, or a host-wide server set, which is a larger change.
