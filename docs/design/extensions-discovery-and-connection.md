# Discover, connect and compose extensions

Status: implemented and exercised in real browser and Codex-sign-in journeys,
2026-10-26. Website publication is tracked separately from the implementation PRs.

The product journey is **discover → inspect → add → enable for an agent or
conversation**. Installed content, a configured connection, successful sign-in and
enabled capabilities are different states. Do not label any of them connected until
the actual MCP handshake succeeds. Do not rebuild separate viewers for skills,
plugins and imported files.

## Product surfaces

- Four distinct views: **Marketplaces, MCP connections, Skills, Plugins**. Marketplaces
  owns source install, trust and updates once; the other views have **Yours / Discover**,
  search and their own contextual Add actions. Do not mount the full source and plugin
  settings page inside another inventory. Skills open their actual instructions directly.
- Shared Add machinery with category-specific choices: MCP URL/local command, skill
  create/import/upload, and plugin compose/import/upload. Upload is a clickable drop zone
  naming the required archive structure; the server validates the selected kind.
  URL + display name are sufficient for the common
  remote connection. Authentication is discovered; headers, legacy SSE and client
  registration overrides belong under Advanced.
- Details reuse the plugin explorer, file tree and shared reader. Skill contents stay
  an actual browsable tree, preserving their referenced files. Existing content is
  immutable; edits save a named local variation. Overview, contents and availability
  should not be repeated across nested headers.
- The composer exposes installed capabilities and their actual enabled state, with
  direct links back to the same library. Agent composition uses the existing resolver,
  dependency/removal preview and prompt inspection rather than a new composition model.
- A custom plugin can combine imported skill/plugin references and a named saved MCP
  connection. Reference credentials owned by the connection; never copy tokens, headers
  or credential-bearing endpoint URLs into a portable package or screenshot.

## Runtime implementation

Reuse the native/Claude and portable standalone-skill importers already in the Store,
including pinned source, trust/update handling and typed diagnostics. Add local content
authoring through that owner. Import/archive staging is bounded and
rejects traversal, links, conflicting names and partial publication. No imported setup
script runs merely because somebody browsed a source.

Created and uploaded items share the **private-local** marketplace, preserving each
plugin's identity and the usual per-executable trust digests. Adding an item must not
silently trust it or invalidate siblings. Existing standalone local marketplace references
remain valid. Native collections can contain native plugins, Claude plugins and standalone
skills through the same existing readers.

Marketplaces offers a portable ZIP for a user-managed private GitHub backup. This is
explicitly manual, with no claim of synchronization or automatic repository creation.
Saved connection credentials are outside the exported source. Referenced upstream sources
and named connections must be available on the destination host. A fresh restore remains
untrusted. Windows snapshot copies and failed staging cleanup use extended-length paths.

Saved MCP detail and the tools inventory reuse the connection-field renderer, inventory
rows and readable input-schema renderer. Only an explicit check starts the saved server;
the response includes names, descriptions and schemas from the actual MCP tools/list call.

Extend saved MCP declarations with explicit authentication settings and secret handling.
Reuse the installed MCP SDK's OAuth provider for protected-resource/AS discovery, PKCE,
client registration, code exchange and refresh. CLIO owns the user interaction:
start/status/complete/cancel, bounded pending flows, an external sign-in window, exact
callback/initiator binding, expiry, cancellation and reconnect. Registration modes are
pre-registered client, DCR compatibility and published client metadata only where an
actual CLIO client identity is configured. Never pretend a hosted identity exists.

Support browser and Desktop/remote-agent returns through the existing external-browser
and callback mechanisms. A blocked popup offers a real link; closing a dialog cancels
its pending operation. Browsing never starts sign-in. Tokens and codes stay out of
transcripts, API inspection, logs, file exports and capture assets. Revocation/removal
and invalid refresh tokens must lead to a clear reconnect state, not a silent retry loop.

## Acceptance and documentation

Use the real `homeassistant-ai/skills` repository to import, inspect and compose a local
Home Assistant variation, preserving attribution and the original source. Exercise
custom MCP setup with a real server handshake, and browser authorization with a real
protocol flow. External Home Assistant account acceptance requires the user's sign-in;
local protocol qualification must be labeled separately and cannot certify that account.
Inspect intermediate requests, failures and saved state, not just a final green badge.

Cover wrong URL, auth required, consent denial, stale/replayed callback, cancellation,
restart/refresh, missing connection references, source updates and removal. Verify
desktop/phone readability and that browsing does not eagerly connect every service or
load every skill body. Record resource/process ownership under the existing disposable
D: qualification folder. Code stays in the current repositories, without worktrees.

Update the website's MCP and agents/plugins guides and publish a practical walkthrough
with actual light-mode screenshots: discovery, custom connection, sign-in state, imported
skill contents, local composition and its resolved capabilities. Keep private hostnames,
credential-bearing URLs and secrets out of published captures. Build and render-review
the website; distinguish committed website content from a deployed production page.

## Implementation decisions and qualification findings

`connection_only` keeps newly saved library connections dormant until included in a
plugin. Existing CLI declarations retain their previous discovery behavior. Composition
stores named connection references in `extensions.ai.iowarp.clio.connections`, resolving
them on the agent host; a missing reference fails resolution with an actionable message.

OAuth credentials are isolated by server URL and saved connection name. Sign-out uses
a revocation generation so an old in-memory client or late refresh cannot restore the
credentials. Removing an OAuth connection clears its scoped credentials. The SDK remains
responsible for grants and PKCE. Durable storage records its validated authorization-server
metadata and absolute expiry, so a fresh process refreshes at the authorization server
rather than treating an old token as newly issued or sending refresh to the resource host.

The shared capability picker is used in conversations and composition. Its modal focus
scope prevents a nested picker from immediately closing in the Add dialog. Skill inspection
uses the existing source tree and file-viewer shell, with a provided-byte source for download;
relative reference links navigate only to known files inside that inspected bundle. Source
text is preserved. Skill-only local variations inherit license and notice files.

Qualification found and repaired canonical transport validation rejecting saved setup fields,
lost token expiry/discovery across process restart, accidental global connection availability,
same-URL credential conflation, nested-picker focus dismissal, and blocked bundled-reference
links, POST-only authentication detection, and cross-origin popup cancellation. Real public Home Assistant skill import and browser OAuth against a synthetic read-only
MCP are separate evidence. The synthetic account cannot establish Home Assistant deployment
compatibility or authorization. Session receipts, failed attempts, process custody and captures
are retained in the disposable qualification folder, outside the repositories.

A fresh composed-agent conversation loaded the actual imported skill, requested and received
one-time permission for the fixture tool, called it once, and read its dashboard guide before
answering. Its raw tool results and permission interaction were inspected. Browser journeys
also exercised header authentication, a local stdio command, source download, reference links,
skill variation, plugin composition, denial, retry and cancellation. A fresh process refreshed
expired credentials and made four parallel read-only calls without new registration or consent.
Native Windows callback sockets were exercised; a complete installed Desktop OAuth journey and
authorization against an actual Home Assistant deployment remain separate acceptance work.

Expanded transcript review found that the standard MCP presenter discarded duplicate JSON
text without rendering its structured counterpart. The common presenter now emits a bounded,
literal data outline when no human content is supplied. Specialized presenters and failure
classification remain authoritative. Historical empty SDK presentations can use their exact
retained result without rewriting records or trusting a truncated preview. A second fresh
workspace/session verified a newly recorded readable result, including its actual producer
event, one-time permission, raw returned values and rendered disclosure.
