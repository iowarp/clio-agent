---
title: "Write a connector entry and a following plugin"
description: "The connector entry format, the plugin field that ties a plugin to a connector, and the CLIO-specific parts of an MCP App."
source: https://clio.iowarp.ai/docs/sdk/connectors/
---

# Write a connector entry and a following plugin

The connector entry format, the plugin field that ties a plugin to a connector, and the CLIO-specific parts of an MCP App.

> **Note: Development build**
>
> Connectors and the app features on this page are in the development build. v0.9.6.6
> does not have them. See [Features and versions](docs/versions.md).

A [connector](docs/connectors.md) is data: one entry in a YAML file and, optionally, a
logo. Adding one needs no Python or TypeScript. A plugin can follow a connector, to give
the agent skills for its tools. This page covers both, and then what an MCP App server
author can use in CLIO beyond the MCP Apps standard.

Under the hood, CLIO carries every connector into a conversation as an Agent Plugin:
the plugin that follows it, or else a plugin with no content that only binds its
connection (below). People never see that plugin; they add the connector.

## Where entries live

```text
catalogs/connectors/
  connector.schema.json the JSON Schema every file is checked against
  burette.yaml
  burette.svg
  earthdata.yaml
  geo.yaml
  home-assistant.yaml
  paraview.yaml
  paraview.svg
  pdf.yaml
  pdf.svg
  spice.yaml
```

CLIO ships the entries in `catalogs/connectors/` of its repository. Each file is a
list of entries, and each logo sits beside its file.

To try an entry without changing CLIO, put it in a folder of your own and name that
folder in `connectors.catalog_dirs`, or in `CLIO_CONNECTOR_CATALOG_DIRS` (several
folders separated like `PATH`). A later folder cannot replace an entry an earlier
folder already defines. Restart CLIO and open **Settings → Extensions**.

An entry that does not validate is left out and the rest load. `GET /v1/connectors`
lists each problem under `diagnostics`, with the file, the entry and a code:
`connector_entry_invalid`, `connector_entry_duplicate` or `connector_file_unreadable`.

## An entry

```yaml title="catalogs/connectors/paraview.yaml"
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
    - key: address
      kind: address
      label: Where ParaView runs
      required: false
      agent_settable: true
      help: Leave empty for this machine. Otherwise an SSH host name.
    - key: via
      kind: value
      label: Jump host
      required: false
      agent_settable: true
    - key: prepare
      kind: value
      label: Command to run first
      required: false
      agent_settable: true
      help: For example "module load paraview".
  plugin: paraview
```

A remote server is a URL, with optional headers:

```yaml
- id: burette
  name: Burette
  logo: burette.svg
  description: See molecular structures from the Protein Data Bank, and draw or edit molecules
  server:
    url: https://burette-plugin.vercel.app/mcp
  needs: []
  tools: [preview_pdb_structure, render_molecular_scene, open_ketcher, control_ketcher]
```

| Field | Required | Meaning |
|---|---|---|
| `id` | Yes | Lowercase letters, digits and hyphens, up to 63 characters. It is also the saved connection's name and the tool prefix: tool `open_view` becomes `paraview_open_view`. |
| `name` | Yes | The name people see, up to 80 characters. |
| `description` | Yes | One line, up to 300 characters. |
| `server` | Yes | `url` with optional `headers`, or `command` with optional `args` and `env`. |
| `needs` | No | What the person or the agent supplies. See below. Default: none. |
| `logo` | No | An SVG file name beside the entry. Without one, CLIO shows a monogram. |
| `plugin` | No | The plugin that follows this connector, shown on the card as **Agent guidance** with the plugin's title. |
| `tools` | No | The server's tools this connector offers. Absent means all. The others are hidden from the agent and from `search_tools`. A listed name the server lacks is reported, not silently dropped. |
| `hide_tools` | No | Glob patterns of the server's tools this connector does not offer, such as `sim_*`. The connector's proxy lists none of them and refuses each by name, so they reach no agent. Use it to leave out what another connector does better, without changing the server. |

`${VAR}` in a server field is filled from CLIO's environment, as in
[`mcp.yaml`](docs/mcp-servers.md#environment-variables).

## Needs

| Field | Meaning |
|---|---|
| `key` | Lowercase letters, digits and underscores, starting with a letter. Used as `${need.<key>}`. |
| `kind` | `address`, `value`, `secret` or `sign-in`. |
| `label` | The row's label on the card. |
| `help` | A line under the field. |
| `required` | Default `true`. An empty required need holds the connector at "needs". An empty optional one does not. |
| `default` | Used when the person gives nothing. Not for a secret or a sign-in. |
| `agent_settable` | Default `false`. `true` lets the agent set it with `set_connector_value`. Not for a secret or a sign-in. |
| `options` | A list of choices, shown as a picker. Only for a `value`. |
| `group` | A heading the card shows this need under, once, with the others of the same group, such as `Part catalogs`. |

Each kind is shown and placed differently:

| Kind | On the card | Where `${need.<key>}` may appear |
|---|---|---|
| `address` | A text field | `url`, `command`, `args`, `headers`, `env` |
| `value` | A text field, or a picker with `options` | `url`, `command`, `args`, `headers`, `env` |
| `secret` | A masked field, never shown again | `headers` and `env` only |
| `sign-in` | A **Sign in** button using the server's OAuth | Nowhere: CLIO handles the sign-in |

The schema cannot say everything, so CLIO also checks:

- every `${need.<key>}` names a need of the entry;
- a secret appears only in `headers` or `env`, never in arguments or the URL;
- a sign-in is only for a URL server, and an entry has at most one;
- `options` appear only on a `value`.

An optional need left empty is filled as an empty string, after its `default`. Your
server should treat an empty value as "not set". When a value changes, CLIO restarts a
command server, or reconnects to a URL server, and lists its tools again. Read values
when the server starts.

Address and value needs are filled per conversation: a conversation's own
values over the person's defaults. Two conversations with different values run two
copies of your command server (or two connections to your URL), each started with its
own environment and arguments, side by side in the same workspace folder. Do not
assume one copy per machine: key any state you keep on disk by the values it belongs
to, and expect a reopen to replay a call against whichever values the conversation
has at that moment.

A secret reaches the server only in its environment or a header. CLIO keeps it in its
credential store, and the saved connection holds only the placeholder.

### Logos

Use the vendor's own mark, as an SVG, and record where it came from and its terms in
the pull request. CLIO serves the file with a policy that runs no script. A
raster-only mark can be wrapped unaltered in an SVG `<image>`.

## A plugin that follows a connector

A plugin that is only useful with a connector, such as skills for working with its
tools, declares `follows` in its `ai.iowarp.clio` extension, and binds the connector's
connection under the same name with `connections`:

```json title="plugins/paraview/plugin.json"
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "paraview",
  "version": "1.0.0",
  "description": "Work with scenes in the ParaView connector.",
  "extensions": {
    "ai.iowarp.clio": {
      "follows": "paraview",
      "connections": {"paraview": "paraview"}
    }
  }
}
```

- Enabling the connector adds the plugin to no conversation. While the connector is
  enabled, the plugin is offered under **Connectors** in a conversation's **Add
  plugin** picker, by the connector's name and logo, and in the agent editor. The
  person adds it to the conversations that need it, or to an agent as a default. The
  agent can also offer it with `offer_plugin`, by the connector's id or name.
- When the connector is turned off, a conversation that has the plugin keeps it in its
  delta, but the plugin leaves the conversation's resolution with the typed reason
  `connector_off`; it comes back when the connector is enabled again. A connector
  that is off is not offered in the pickers.
- People see one entry. When the plugin brings anything beyond the connector's server,
  such as skills, the entry is shown as a **Plugin** with what it brings ("2 skills,
  1 MCP server (27 tools)") and the connector's needs and state; otherwise it is shown
  as a **Connector**. The plugin is never listed a second time on its own.
- `connections` must map the connector's id to itself. A plugin that follows a
  connector without that binding fails to load.
- The plugin must be installed from a marketplace the person trusts. IOWarp's
  following plugins ship in the `clio` marketplace. Set the entry's `plugin` to its
  name so the card can name it.

A connector with no following plugin is added the same way: for every enabled
connector CLIO lists a plugin with no content, `connector-<id>` in the
`clio-connectors` marketplace, that only binds the connection. Name it in nothing you
ship; it is CLIO's, and people see the connector.

Write the plugin's skill to tell the agent how to use the tools well, and which values
it may change with `set_connector_value`. See [Plugin reference](docs/sdk/plugins.md).

## How the agent finds a connector or plugin

In a conversation, the agent sees the connectors and plugins it does not have yet:
every connector first, then the plugins of the marketplaces the person added, the most
recently added marketplace first, up to `plugins.offer_listed` (default 20) with the
total. The person can change that list (Settings, "Plugins CLIO can offer", or
`GET`/`PUT`/`DELETE /v1/plugins/offer-list`): their ticks are kept as an overlay on the
default, `exclude` inside the first 20 and `include` beyond them, in the user
`config.yaml` under `plugins.offer`. Each is listed by name with the start of its
description, about 100 characters.
Put what it does in those first words, so the agent can tell when a request needs it.
The facts say how many more there are, and how many of those the person left out.
`offer_plugin` with `search` (words matched against the id, name, summary, category and
keywords) or `category` (a connector's Discover group, a plugin's marketplace category)
finds the others, each marked whether it is listed and whether the person left it out;
with nothing named it gives the categories and counts. Give a plugin `keywords` and a
`category` so it can be found. The agent offers one with `offer_plugin`; the person's
**Add** is the consent.

A connector can work without an optional need and better with it: a parts search with
no key, and more catalogs with one. `offer_plugin` with no plugin also lists
`optional_needs`, the empty optional needs of the connectors already in the
conversation. When a tool's result shows a key would help, the agent calls
`offer_plugin` with the connector, its reason and `needs` set to those keys. The person
sees the connector's card with only those rows and types the values there, never in
the chat. A secret goes to the credential store and the server restarts with it; the
agent then calls the tool again. **Not now** is a refusal, and the same needs are not
asked again in that conversation. So say in your tool's failure what a key would add
and by which variable, and give the need a `help` line that says the same.

## For MCP App authors

CLIO hosts MCP Apps by the [MCP Apps specification](https://github.com/modelcontextprotocol/ext-apps)
revision 2026-01-26, plus the draft's app tools and downloads. An app written for
another host works unchanged. The rest of this section is what CLIO adds or decides.

### What CLIO tells the view

`ui/initialize` answers with these host capabilities:

| Capability | What it means in CLIO |
|---|---|
| `openLinks` | `ui/open-link` opens an `http` or `https` URL in CLIO's browser. Other schemes are refused with `data.reason: "unsupported_scheme"`. |
| `downloadFile` | `ui/download-file` saves embedded resources into the conversation's workspace folder and lists them as artifacts, up to 64 MB. A `resource_link` is refused. |
| `serverTools`, `serverResources` | The view may call its server's tools and read its resources. Calls follow the person's permissions. |
| `updateModelContext` | `{text, structuredContent}`. The last report, up to 128 KiB, reaches the agent with the person's next message. |
| `message` | `{text}`. A `ui/message` becomes a message in the conversation. |
| `logging`, `sandbox` | As in the specification. |

**Display modes.** CLIO offers `inline` (the card) and `fullscreen` (a tab). It
honours `ui/request-display-mode` only for a mode your app lists in
`appCapabilities.availableDisplayModes`, when you list any. A `pip` request returns the
current mode. In a dashboard only `inline` is offered. Moving between card and tab
reloads the frame and replays `ui/initialize`, the tool input and the tool result, so
render from those.

**Sandbox.** The view runs on an origin of its own, one per server, with
`allow-scripts allow-same-origin allow-forms`, so `localStorage` works. It cannot call
CLIO's API. Its content security policy comes from the resource's declared domains;
loopback sources are dropped. To reach a service on the person's machine, use a
declared service (below).

**Theme.** `hostContext.theme` and `hostContext.styles.variables` carry the
specification's theme variables, resent in `ui/notifications/host-context-changed`
whenever the person switches theme: `--color-background-*`, `--color-text-*`,
`--color-border-*`, `--color-ring-*`, `--font-sans`, `--font-mono`, `--font-weight-*`,
`--font-text-*-size` and `-line-height`, `--font-heading-*-size` and `-line-height`,
`--border-radius-*`, `--border-width-regular` and `--shadow-*`. A variable the theme
lacks is left out, so keep your own fallback.

### Context and linking

Report state with `ui/update-model-context`. Put the values another view could use in
`structuredContent`, one key each: `time`, `selection`, `threshold`. A dashboard binds
those keys to its data paths, and can set them back through a tool of your server or
of your view. Report settled values, not every frame of a drag; CLIO keeps at most ten
writes a second per key, the last one winning.

### App tools

Declare `appCapabilities.tools` (with `listChanged` if your list changes). CLIO lists
them and offers each to the agent as `app__<server>__<tool>`, loaded when needed. Calls
reach the frame the person has open, with no timeout. At most 64 tools, each input
schema at most 64 KiB; over either limit the view offers none. A tool whose full name
does not fit `[A-Za-z0-9_-]{1,64}` is skipped. Send `notifications/tools/list_changed` when the list changes.

### CLIO keys in `_meta`

CLIO reads three private keys. It removes the first two from the result before the
view, the transcript or the model sees it.

**`ai.iowarp.clio/service`**, in the `_meta` of the result that opens the view. A live
service behind the view, such as a trame or Jupyter server:

```json
"_meta": {"ai.iowarp.clio/service": {"port": 41873, "token": "<random string>"}}
```

- The service listens on `127.0.0.1:<port>` on the machine CLIO runs on, and must
  refuse any request or WebSocket without `Authorization: Bearer <token>`. CLIO adds
  that header; the browser and the agent never see the port or the token.
- Only a server CLIO started (a command server) may declare one.
- The view gets the URL in `hostContext["ai.iowarp.clio/service"].url`, ending in `/`.
  Frame it or connect to it there, with relative URLs only. HTTP and WebSockets both
  work, with no idle timeout.
- Closing the view closes the route and its WebSockets.

**`ai.iowarp.clio/reopen`**, in the same result. The arguments a reopen should replay
instead of the call's own:

```json
"_meta": {"ai.iowarp.clio/reopen": {"arguments": {"scene": "pulse-series-4eec1176"}}}
```

ParaView declares the scene's id, so a view first opened with `new: true` reopens the
scene the person built. Make your opening tool restore from these arguments; CLIO
keeps no copy of the view's state.

**`ai.iowarp.clio/paths`**, in a tool's definition (`tools/list`), not a result. Which
arguments are paths, and on which machine:

```json
"_meta": {"ai.iowarp.clio/paths": {
  "path": {"machine": "remote", "access": "read", "host": "node08"},
  "out":  {"machine": "local", "access": "write"}
}}
```

`machine` is `local` (where CLIO runs), `remote` (`host` optional, for display) or
`none` (path-shaped but not a file). `access` is `read` (default) or `write`. CLIO
records a remote read in the result's provenance without trying to read the file
here. A server whose machine is a setting computes this when it lists its tools; CLIO
restarts it when the setting changes.

### Looks and comments

The agent's `look_at_view` and people's comments on a view use a picture CLIO takes
of the frame itself. It needs nothing from you. It paints canvases (WebGL included),
images, SVG and one-line text. Frames or pixels from another origin cannot be
painted, and the result says so; keep nested frames on your own origin or your
declared service.

### What lies under a mark: `describe_region`

When a person draws a mark on your view, CLIO's picture shows where it is but not what
is there. If your view offers an app tool named `describe_region`, CLIO calls it once
per mark with the mark's rectangle in your view's CSS pixels:

```json
{"x": 412, "y": 160, "width": 220, "height": 140}
```

Return a PNG of that region drawn by your view, and what lies under it as data:

```json
{
  "content": [
    {"type": "image", "data": "<base64 PNG>", "mimeType": "image/png"},
    {"type": "text", "text": "{\"footprints\": [...], \"nets\": [\"GND\"]}"}
  ],
  "structuredContent": {"footprints": [{"reference": "U1", "layer": "F.Cu"}], "nets": ["GND"]}
}
```

Your picture replaces CLIO's crop for that mark, and the agent reads the data as
"Under the mark", one line per key: say what the region holds in your domain's terms
(the KiCad board view returns the footprints, pads, tracks and nets with layers and
millimetre coordinates). A view without the tool gets marks with their rectangle and a
note that it could not describe them. This is CLIO's convention; other hosts ignore the
tool.

## Next steps

- [Connectors](docs/connectors.md): What people see when they turn a connector on.
- [Apps](docs/apps.md): How people use an app in a card, a tab or a dashboard.
- [Plugin reference](docs/sdk/plugins.md): Every field of a plugin's CLIO extension.
