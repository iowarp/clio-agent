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
      "title": "Status board",
      "catalogs": ["basic"],
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
| `title` | Optional. The plugin's readable name, text of 1 to 60 characters, sent as `plugin_title` on its tabs, its surface owners, its `a2ui.server_surface.refused` events and its URL questions. It lives here because Agent Plugins 1.0.0 has no display-name field (a top-level `title` is ignored with a warning). Without it CLIO sends no `plugin_title` and the client shows the id read as words | Not text, or empty |
| `tabs[].id` | `[a-z0-9][a-z0-9-]{0,31}`, unique in the plugin. The host id is `<plugin>/<id>` | Missing, bad shape, duplicate |
| `tabs[].title` | Text, 1 to 40 characters | Missing or empty |
| `tabs[].titles` | Optional, the title in other languages: an object mapping BCP 47 tags (`es`, `pt-BR`) to text of 1 to 40 characters. A listing answers in the request's `X-Clio-Locale`: the exact tag (any case), then the tag with its last subtags dropped (`pt-BR`, then `pt`), else `title` | Not an object, a key that is not a tag, or a value that is not 1 to 40 characters of text |
| `tabs[].icon` | A Lucide icon name. An unknown name renders a generic icon | Not text |
| `tabs[].server` | A server name in this plugin's `mcp.json`. May be omitted when the plugin has exactly one server | Unknown, or omitted with several servers |
| `tabs[].resource` | An `a2ui://` URI | Other scheme |
| `navigation` | Optional, one page in the host's primary navigation (see HS3 plugin pages): a tab's fields, `id` optional (default `page`) and different from every tab id | Not an object, or a field fails a tab's rule |
| `accounts[].provider` | One of `github`, `google_drive`, `globus`, `ndp` | Unknown provider, duplicate |
| `accounts[].scopes` | List of scope strings, may be empty | Not a list of text |
| `elicitation.loopback` | Boolean, default `false` | Not a boolean |
| `elicitation.origins` | List of `https://host[:port]` origins, no path | Other scheme, a path, a wildcard |
| `elicitation.audience` | Optional, only `"person"`: what this plugin's servers ask (an approval, a spend, a review) needs the person. Their questions go straight to the person, even when a server tags one for the agent (see "Questions that need the person"). Untagged questions already go to the person by default | Any other value |
| `setup.files` | Optional list of paths relative to the plugin's `PLUGIN_DATA` folder: the plugin's own state files that say the person has set it up (see "Set-up plugins join new conversations") | Not a list, another field beside `files`, an absolute path, a backslash, a drive or `..` |

`catalogs` is unchanged: a built-in catalog's name (`"basic"`), and any catalog the
plugin ships under `ai.iowarp.clio/a2ui/<name>/`. HS1 uses the `catalogId`s these
resolve to (Basic is `https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json`)
as the server's producible set.

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
- `supportedCatalogIds` holds the `catalogId`s of the catalogs the receiving server's own
  plugin declares in `catalogs` (full ids, not the manifest's short names). A server with
  no plugin, or a plugin with no `catalogs`, gets no A2UI
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
"owner": {"kind": "server", "plugin": "<plugin id>", "server": "<server name>", "namespace": "<ns>",
          "plugin_title"?: "<the plugin's title>"}
```

`plugin_title` is present only when the plugin declares a `title`. It is for display; the
owner's identity is `kind`, `plugin` and `server`.

A surface with no `owner` is CLIO's own, and keeps today's behaviour. The owner is fixed
at `createSurface` and never changes. Replay of an old surface whose server is gone
renders it read-only (actions refused with `a2ui_server_unavailable`).

A task-augmented tool call (MCP tasks) applies A2UI from its terminal result, when the
task completes. Progress notifications carry no A2UI.

## HS2: actions on a server's surface go to that server

The client sends the same request as for any surface, to the same route
`POST /v1/sessions/{sid}/a2ui/actions`, with header `x-a2ui-version: 0.9.1` (else 406;
the header has no `v`, which is only the spelling of the envelope's `version`)
and the existing wrapper body `{"message", "correlation"?, "metadata"?}` (any other
top-level field is 422 `validation_error`):

```json
{"message": {"version": "v0.9.1",
             "action": {"name": "refresh", "surfaceId": "s1", "sourceComponentId": "btn",
                        "timestamp": "...", "context": {"row": "a", "token": "..."}}}}
```

When the surface has a server owner:

1. `data_source/login/<provider>` is a host action, not a server call (HS4). CLIO fills
   its context (`clio_id`, `workspace_id`) itself; the server sends an empty context.
2. Any other `name` is the server's tool name (without the namespace). CLIO resolves
   `<namespace>_<name>` on the owner and refuses with `a2ui_server_tool_not_app_visible`
   (403) unless the tool exists and its `_meta.ui.visibility` is absent or includes
   `"app"`. A tool listed with visibility `["app"]` only is never shown to the model.
   "Exists" means the owner's own `tools/list` lists it now, read over the namespace's
   connection (starting the server if it is not running). It does not depend on any
   turn having run, so a tab action works straight after an agent restart.
3. CLIO sends `tools/call` with `arguments` = the action's `context`, verbatim. No model
   turn runs. The call is recorded as a view call (it never splits an open turn). Before
   any turn has run (for example straight after an agent restart), CLIO first builds the
   session's host from the session's own model, as that turn would; the same applies to a
   tab's `open` and its re-reads. Every call has an owning task: in a session that has
   never run a turn, CLIO records an already finished root assignment to own it (no
   message, no turn), and the session's first message starts the next one as usual. The
   agent's own calls are unchanged; a turn always creates their owner. The call goes
   through the normal tool approval: a tool
   that needs it raises the usual `permission.requested` (kind `external_mcp`), and the
   HTTP request waits until the user answers it (`POST /v1/permissions/{id}`). For the
   server's view-only tools this is one question per server, not one per tool; see
   "One consent per server surface" below.
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
(the result had `isError`; `error_text` holds its text). A `task_pending` response also
carries `task_handle`, the CLIO task the call became. Refusals are typed HTTP errors:
`a2ui_server_tool_not_app_visible` (403), `a2ui_server_unavailable` (503, the owner is
not mounted in this session), `a2ui_surface_not_owned` and the HS1 reasons (422).
Duplicate submissions are deduplicated as today.

A server may ask the client to show a short notice for the action by returning, in the
result's `_meta`, `"io.iowarp/toast"`:

```json
{"message": "Saved the site", "tone": "positive",
 "undo": {"label": "Undo", "action": {"event": {"name": "undo_save", "context": {"id": "a"}}}}}
```

CLIO checks it and passes it through as `"toast"` on the action response, on every
`outcome` (and on the tab and page `actions` routes, which answer the same way).
`message` is 1 to 300 characters. `tone` is `neutral` (the default), `positive`,
`warning` or `negative`. `undo` is optional: `label` is 1 to 40 characters and
`action.event` is the A2UI action the client sends to the same surface's server when the
person clicks it (`name` required, `context` an object when present). A toast that does
not fit is dropped and logged; the response is unchanged otherwise.

A client names the language it shows CLIO in with the request header
`X-Clio-Locale: <BCP 47 tag>` (for example `es` or `pt-BR`, at most 35 characters).
While that request runs, every `resources/read` and `tools/call` CLIO sends to a plugin's
server for it (a tab or page `open` and its read, and an action) carries the request
`_meta` `{"io.iowarp/locale": "<tag>"}`. A tab keeps the tag of its last `open` for the
re-reads a server's update triggers. A missing or malformed header sends no key, and a
server may ignore it.

A `data_source/login/<provider>` action answers as any client sign-in action does, and
the client then runs its own sign-in for `provider`, which it takes from the action name it
sent:

```json
{"name": "data_source/login/globus", "status": "accepted", "action_id": "...",
 "state": "consumed", "delivery": "client_ui", "destination": "client"}
```

In the conversation the reply also carries `"surface"`, as every conversation action
reply does. On a tab (HS3) it also carries `"provider"` and the `"context"` CLIO filled. A
client may treat any 200 as "start sign-in" and needs no field of the body. A 422
`a2ui_source_login_out_of_scope` means sign-in is not available here (no account service,
or the session is gone). GitHub has no
declared scopes to request (its App's permissions are fixed), so a GitHub account only
needs to be connected.

The context is opaque to CLIO. A server that needs to bind an action to what it rendered
(for example a single-use token) puts that in the context it renders.

### One consent per server surface

A surface usually drives several of its server's view-only tools: tools whose
`_meta.ui.visibility` lets apps call them and hides them from the model. Opening a form,
checking it and saving it are three such tools. The model can never call them, so CLIO asks
one question per server: may this server act on what the person does on its surface?

When an action on a server-owned surface (a page, a tab, or the server's surface in a
conversation) reaches a view-only tool that needs approval, the `permission.requested` it
raises (kind `external_mcp`) covers all of that server's view-only tools at once:

```json
{"kind": "external_mcp", "tool_name": "srv_open_form",
 "summary": "Let Example act on what you do in its Settings page",
 "consent": {"scope": "server_surface", "namespace": "srv", "server_title": "Example",
             "place": "page", "place_title": "Settings",
             "tools": ["srv_open_form", "srv_check_form", "srv_save_form"]}}
```

`place` is `page`, `tab` or `conversation`. For a conversation the summary reads "Let
<server> act on what you do in its view". A client shows `summary` as the question and may
list `tools` under it. An `allow_session` or `allow_workspace` answer grants every tool in
`consent.tools` at that scope, so the next action on any of the server's controls runs
without asking. An `allow_once` or `deny` answer applies to the one call, as before. A tool
the model can also see keeps its own per-tool question, and the agent's own calls are
unchanged. The question goes to the session the action runs in; for a page, that is the
page's context session. Nothing here names a particular server.

On the plugin's own page (`place: "page"`, the plugin's entry in the primary navigation)
the person's click is the consent: an action there that reaches one of the plugin's
view-only tools runs without a question, recorded as `auto_approved` with reason
`surface_page_click`. A deny from a rule or the tool's default still wins, an `ask` rule
or default still asks, and a tool the model can also see keeps its own question. Tabs and
conversation surfaces ask as above.

## HS3: plugin tabs

A tab is listed for a session when the session's agent mounts the tab's plugin. Its
server runs in that session's server set. Tabs are per session: the client builds a
session's tab strip from that session's `GET .../plugin-tabs`, never from tabs it
remembers from another session. Opening a tab the session does not list is
`plugin_tab_not_found` (404); a tab the session does list is fully live there, with its
own read and watch, whichever session first opened it.

```text
GET  /v1/sessions/{sid}/plugin-tabs
     -> {"tabs": [{"id": "<plugin>/<tab>", "plugin": "...", "plugin_title"?: "...",
                   "title": "...", "icon": "...", "server": "...", "resource": "a2ui://...",
                   "placement": "tab", "available": true, "reason": ""}]}
POST /v1/sessions/{sid}/plugin-tabs/{plugin}/{tab}/open
     -> {"tab": {...}, "surfaces": [<surface wire>, ...], "revision": 3}
POST /v1/sessions/{sid}/plugin-tabs/{plugin}/{tab}/close
     -> {"closed": true}
POST /v1/sessions/{sid}/plugin-tabs/{plugin}/{tab}/actions
     header x-a2ui-version: 0.9.1 (else 406)
     body {"message": {"version": "v0.9.1", "action": {...}}, "correlation"?, "metadata"?}
          (the same body as /v1/sessions/{sid}/a2ui/actions)
     response as HS2
```

- An unknown tab is `plugin_tab_not_found` (404). `actions` on a tab the host does not
  hold open (a server restart, or a `close` that raced the client's reopen) reads the
  tab again first, then acts; it is `plugin_tab_not_open` (404) only when that read
  opens nothing, or 409 with the tab's reason when the read failed.
- `open` and `close` may race: a client that remounts sends `open`, `close`, `open` on
  separate connections, and they can arrive in any order. An `open` that answers after
  a `close` keeps the tab open and watched, so the last answer the client got holds.
- `open` sends `resources/read` for the tab's resource and starts watching it for
  updates. The read result is the tab's whole state: its A2UI items replace every
  surface the tab held. The first batch must start with `createSurface`.
- Watching: CLIO opens a `subscriptions/listen` stream with
  `resourceSubscriptions: [<tab resource>]` (2026-07-28). A server that cannot listen
  (an older protocol version, or `-32601`) may instead send
  `notifications/resources/updated` for the URI unsolicited on its connection. CLIO
  never sends `resources/subscribe`, which 2026-07-28 removed.
- Server obligation at 2026-07-28: an update reaches CLIO only on the listen stream. The
  `mcp` SDK drops a bare `notifications/resources/updated` sent on a 2026-07-28
  connection (`send_resource_updated` is a no-op there), so a server that only sends it
  never refreshes the tab. A server must serve `subscriptions/listen` and publish
  `ResourceUpdated(uri)` to it. FastMCP 4.1 does not serve listen by itself. Register
  the SDK's handler on its low-level server and publish on the bus:

  ```python
  from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler
  from mcp.shared.subscriptions import ResourceUpdated

  bus = InMemorySubscriptionBus()
  server._mcp_server.add_request_handler(
      "subscriptions/listen", mcp_types.SubscriptionsListenRequestParams, ListenHandler(bus)
  )
  ...
  await bus.publish(ResourceUpdated(uri=PANEL_URI))
  ```

  `tests/fixtures/plugins/host_semantics/server.py` does exactly this, and also sends the
  bare notification for clients on an older protocol.
- On `notifications/resources/updated` for that URI, either way, CLIO reads again and
  replaces the tab's surfaces. A listen stream that drops is reopened, and then read
  again once. CLIO does not poll. One update is one re-read: an update stamped with a
  listen subscription id is taken from that stream only, and only an unstamped one is
  taken as a bare push.
- The watch belongs to the server process it was opened on. When the server is mounted
  again (the session's agent removes and re-adds its plugin, a connector change
  replaces it, or CLIO reconnects it), CLIO ends the old stream, opens one on the
  server mounted now, and reads the tab again once. A server an idle reap stopped is
  not restarted for a watch; the tab's next `open` starts it.
- An installed plugin update reaches the sessions open now, within about two seconds of
  resolving (after `clio-agent plugin trust`, when the update changed executable parts).
  CLIO pins each open session that holds the older copy again, exactly as
  `PATCH /v1/sessions/{sid}/agent` does: its servers mount from the new snapshot, each
  open tab re-watches and reads again as above (its `owner`, `plugin_title` included,
  follows the new manifest), and the session's stream gets `session.updated` with
  `reason: "plugin_updated"` and `plugins: [<ids>]`. When the update changed executable
  parts (a server command or env), the running server processes are replaced: CLIO
  restarts the workspace's server fleet at once, or at the end of a turn that is running
  in that workspace. A text-only update keeps them. A client re-reads `GET .../plugin-tabs`
  on that event. An update that does not resolve yet (not yet trusted) leaves the
  session on its copy until it does. A session that is not open moves when it is next
  opened.
- `close` drops the tab's surfaces and, when no session still has the tab open, ends the
  listen stream.
- Tab surfaces are not part of the transcript and are not seen by the model. They live
  in memory for the open tab; after a server restart the next `actions` or `open` reads
  the tab again. Every tab surface carries the tab
  server's `owner`. Actions on them follow HS2; their results are applied to the tab.
- `available: false` with a typed `reason` (`plugin_tab_server_unavailable`,
  `plugin_tab_resource_failed`) when the server is not mounted or the read failed. A read
  that returns refused A2UI opens the tab empty with the HS1 reason.

## HS3: plugin pages (primary navigation)

A plugin may also declare one page in the host's primary navigation (the left bar,
below New session and Search). A page has a tab's fields and behaves as a tab, but it is
global: it belongs to no conversation and the same page is shown from every session.

```json
"navigation": {"id": "compute", "title": "Compute", "icon": "server",
               "titles": {"es": "Cómputo", "el": "Υπολογισμός"},
               "resource": "a2ui://example/page"}
```

- `id` is optional (default `page`), matches the tab id pattern and differs from every
  tab id of the plugin. `title` is 1 to 40 characters, and `titles` words it in other
  languages as a tab's does. `server` defaults to the plugin's
  only server, as for a tab. One page per plugin.
- Context: the host keeps one hidden session per page, its context. It is created on the
  first `open` and reused after that (and across restarts). Its agent is the default
  agent with the plugin added, its `session_kind` is `page`, and it has no messages. It
  is left out of `kinds=conversation,branch` lists; a client that lists sessions another
  way leaves out `session_kind: "page"`. No turn runs in it. Its model is the session
  default model; a context created before any model was chosen takes the default on its
  next `open` or action. With no model at all the host cannot read the page, and `open`
  returns it with `available: false`.
- Everything a tab has, a page has, through its context: the resource watch, rising
  revisions, HS2 actions, the first-click assignment anchor, the `external_mcp`
  permission prompt, refused-surface and URL questions, and the remount on a plugin
  update.

```text
GET  /v1/plugin-pages
     -> {"pages": [{"id": "<plugin>/<page>", "plugin": "...", "page": "...",
                    "plugin_title"?: "...", "title": "...", "icon": "...",
                    "server": "...", "resource": "a2ui://...", "placement": "primary",
                    "available": true, "reason": "",
                    "context_session_id": "sess_..." | null}]}
POST /v1/plugin-pages/{plugin}/{page}/open      body {}
     -> {"page": {...}, "context_session_id": "sess_...",
         "surfaces": [<surface wire>, ...], "revision": 1}
POST /v1/plugin-pages/{plugin}/{page}/close
     -> {"closed": true}
GET  /v1/plugin-pages/{plugin}/{page}/status
     -> {"state": "idle" | "starting" | "ready" | "failed",
         "steps": [{"id": "env" | "server" | "page",
                    "state": "pending" | "active" | "done" | "failed",
                    "elapsed_ms": 0}],
         "error"?: "..."}
POST /v1/plugin-pages/{plugin}/{page}/actions
     header x-a2ui-version: 0.9.1 (else 406)
     body and response as a tab's actions
```

- `status` reports the page's latest `open` while it runs, so a client can poll it
  (every few hundred milliseconds) and show progress instead of a blank page. `env` is
  preparing the plugin's environment (provisioning, such as a `uv` build), `server` is
  starting its MCP server, and `page` is reading the page's resource. The steps always
  come in that order. A page not opened since CLIO started is `idle` with every step
  `pending` and `elapsed_ms` 0. A failed open is `failed`, its step in progress
  `failed`, and `error` says why; the next `open` starts a new record. Opens that
  overlap share one record. The route reads only the record, so polling is cheap.

- The list covers every installed plugin that declares a page. An untrusted plugin's
  page is `available: false, reason: "plugin_not_trusted"`, and opening it is 409 with
  that reason. A page whose last read failed carries the tab reasons
  (`plugin_tab_server_unavailable`, `plugin_tab_resource_failed`).
- An unknown page is `plugin_page_not_found` (404). `actions` before the first `open`
  (no context yet) is `plugin_page_not_open` (404). After a server restart, or a `close`
  that raced a reopen, `actions` reads the page again first, as for a tab.
- Events arrive on the context's stream, `GET /v1/sessions/{context_session_id}/events`:
  `plugin_page.surface.upserted {page_id, revision, surface}`,
  `plugin_page.surface.deleted {page_id, revision, surface_id}`,
  `plugin_page.refreshed {page_id, revision, surface_ids}` and
  `plugin_page.unavailable {page_id, reason, detail}`, with a tab event's rules.
  Permission prompts and questions arrive there with their usual types and are answered
  through the context's routes. `session.updated` with `reason: "plugin_updated"` tells
  the client to re-read `GET /v1/plugin-pages`.
- A page's resource is never listed as a tab: `GET .../plugin-tabs` shows only
  `placement: "tab"` rows, and opening the page as a tab of an ordinary session is
  `plugin_tab_not_found`.

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

The question CLIO shows carries where the origin came from, so the UI can say it. In the
question's `metadata.elicitation`, beside the existing url-mode fields:

```json
{"mode": "url", "url": "http://127.0.0.1:41234/ask/...", "origin": "http://127.0.0.1:41234",
 "origin_trust": "plugin_loopback", "plugin": "<plugin id>", "server": "<server>",
 "plugin_title"?: "<the plugin's title>"}
```

`origin_trust` is `configured`, `plugin_origin` or `plugin_loopback`. `plugin` is "" for a
server no plugin declares. `plugin_title` is present only when that plugin declares a
`title`.

In the pending interaction (`GET /v1/sessions/{sid}/interactions` and its events), the
question's `metadata.elicitation` fields are also spread flat into `payload`, as every
url-mode field already is: `payload.origin`, `payload.origin_trust`, `payload.plugin`,
`payload.server` and, when present, `payload.plugin_title` sit beside `payload.url`.
`payload.metadata` keeps the nested copy.

## Questions that need the person

Any server can ask through elicitation, from a plain tool call or from a task's
`input_required`. How CLIO handles such a question:

- **Who answers.** A form question goes to an agent helper only when the server tags it
  `_meta["x-clio-agent/audience"] = "agent"`. Every other question, an untagged one
  included, is the person's from the start: the agent never sees or answers it. A server
  of a plugin that declares `elicitation.audience: "person"` is the person's even when
  tagged. A deployment can opt in to sending untagged form questions to the helper first
  (`tools.mcp.elicitation.agent_audience.default_unhinted`, off by default), because the
  MCP SDK drops the `_meta` tag on a task's `input_required` round trip.
- **The turn waits.** A plain tool call that asks already blocks until the answer comes,
  like a permission ask. A task-augmented call now behaves the same while its task waits
  for input. After the server accepts the task, the agent's call waits up to 2 s for the
  server's first status report. If the task then waits for input, the call holds until
  the task stops waiting (answered, declined, settled or cancelled) or the turn is
  stopped, and only then returns its receipt. The agent continues with the answer in the
  same turn instead of ending it with "please review the pending request". A task that
  does not ask returns its receipt as before. Page actions (HS2) are not held.
- **Title.** The pending interaction's `title` (`GET /v1/sessions/{sid}/interactions`)
  names who asks and what: the plugin's `title`, or the server name when it has none,
  then the first sentence of the question, bounded to one line of at most 90
  characters for the ask, for example `Compute: Submit the job to cluster-a?`. A
  question from no server keeps `Question`; a plan review keeps `Review the plan`.

## Set-up plugins join new conversations

A plugin the person has set up is part of their work, so a new conversation has it from
the first message, without the agent having to find and offer it.

- **Set up.** The plugin declares `setup.files`. It counts as set up when any of those
  files, under its `PLUGIN_DATA` folder, holds a value: JSON or YAML that parses to a
  non-empty list or scalar, or to a mapping with at least one non-empty value, or any
  other file with text in it. A missing or empty file, `[]`, `{}`, `null`, a mapping
  whose values are all empty (`sites: {}`), or a path that leaves the folder through a
  link does not count. A plugin empties the file, or removes it, when nothing is set up.
  Owner: `plugins/setup_state.py`.
- **Joining.** `POST /v1/sessions` adds each set-up plugin of the person's library to the
  new conversation's delta (`add`, as if the person had added it), so its servers, their
  instructions and its skills are there. It does not when the request already removes or
  adds it, when the agent already has it, when the person left it out of the plugin list
  in Settings, or when `plugins.join_set_up` (`CLIO_PLUGINS_JOIN_SET_UP`, default `true`)
  is off. Subagents and plugin pages are not new conversations and are unaffected. The
  person removes it from a conversation like any added plugin. A join the agent cannot
  resolve is left out (trace `plugin_join_set_up_unresolved`), never a failed
  conversation; a join traces `plugin_joined_set_up`. Owner: `gact/plugin_join.py`.
- **Finding a plugin that is not in the conversation.** Every live `instructions` text a
  plugin's server sends at `initialize` is kept by plugin id in
  `<plugin store>/server_instructions.json` (rewritten only when it changes; a server that
  now sends none forgets it). `offer_plugin` search and the tool-search note match a
  plugin on those words too, so a request naming a site, dataset or package a plugin's
  server reaches finds the plugin while none of its servers runs. A search result row
  matched that way carries `mentions`, the line of the instructions that holds the word
  (at most 240 characters), shown as "Its server says: ...". The kept text follows the
  server as it changes: each turn re-reads every kept-alive server's text when it starts,
  and an action on a plugin's page or tab (a site added, edited or removed on a page, say)
  or a `notifications/resources/updated` for a resource an open tab or page watches
  re-reads that server's text at once (`server/discover` over the same connection,
  bounded by `tools.instructions_refresh_s`), so a search later in a running turn finds
  what the server reaches now. Owners:
  `plugins/recorded_instructions.py`, `gact/offer_search.py`, `gact/plugin_offer.py`,
  `gact/server_instructions_refresh.py`.
- **A plugin that is already here.** The same words find the plugins already in the
  conversation (core ones aside). `offer_plugin` search and a tool search that finds no
  tool name them (`in_this_conversation`) with their skills. `offer_plugin` also loads
  the skill the search points to through the agent's own `load_skill`: the one whose id,
  title or description holds the search, else the plugin's only skill. It returns that
  skill as `skill_loaded`. A search that finds nothing at all says that a plugin is found
  by what it does, so a name only the person uses should be searched as the kind of thing
  it is too. Owners: `gact/plugin_offer.py`, `gact/skill_commands.py`,
  `gact/search_tools.py`.

## Events for the UI

On `GET /v1/sessions/{sid}/events` (SSE):

| Event | Payload |
| --- | --- |
| `a2ui.surface.upserted` | The surface wire, now with `owner` when server-owned |
| `a2ui.surface.deleted` | `{"surface_id"}` |
| `a2ui.server_surface.refused` | `{"plugin", "plugin_title"?, "server", "tool", "uri", "reason", "detail"}`; `plugin_title` only when the plugin declares a `title` |
| `plugin_tab.surface.upserted` | `{"tab_id", "revision", "surface": <surface wire>}` |
| `plugin_tab.surface.deleted` | `{"tab_id", "revision", "surface_id"}` |
| `plugin_tab.refreshed` | `{"tab_id", "revision", "surface_ids"}` after a re-read replaced the tab |
| `plugin_tab.unavailable` | `{"tab_id", "reason", "detail"}` |
| `plugin_page.surface.upserted`, `.surface.deleted`, `.refreshed`, `.unavailable` | As the `plugin_tab.*` events, with `page_id` for `tab_id`; only on a page's context stream |

Tab revisions, per open tab (a page's are the same, on its `plugin_page.*` events):

- `open` returns the tab's whole state and its `revision`; the client replaces what it
  held with it.
- Every `plugin_tab.surface.upserted` and `plugin_tab.surface.deleted` adds exactly 1.
  An event with `revision <= current` is stale (it was folded into an `open` reply) and is
  ignored. An event with `revision > current + 1` is a gap: call `open` again.
- A re-read publishes `upserted` for every surface it holds and `deleted` for every
  surface it dropped, then `plugin_tab.refreshed`. `refreshed` adds nothing: its
  `revision` equals the last surface event's, and `surface_ids` are the tab's whole live
  set (ids only). The client drops any tab surface not in `surface_ids`; a
  `revision != current`, or an id it does not hold, means it missed events: call `open`.
- Each surface's own `surface.revision` only rises while the tab is open, across re-reads
  too. A surface whose content changed since the host last published it takes the next
  revision; an unchanged one keeps its revision (and its record). A client may key a
  surface's rendering on `surface.revision`.
- `plugin_tab.unavailable` carries no revision and leaves the surfaces in place. The next
  `refreshed` for the tab means it is available again.
- After the SSE stream reconnects, or on any `stream.gap`, call `open` again for each open
  tab. The stream's replay is bounded and does not report every overflow, so it is not
  enough on its own. `open` on an open tab re-reads it and keeps its watch.

## Decisions (confirmed)

1. The A2UI extension identifier is `org.a2ui/a2ui`. If A2UI publishes an official
   identifier, CLIO advertises both.
2. Tabs are per session: a tab shows when the session's agent mounts the plugin, because
   servers run in a session's server set. A plugin whose tab should show by default goes
   in CLIO's default agent, or declares `setup.files` so that it joins new conversations
   once the person has set it up. There is no host-wide server set.
