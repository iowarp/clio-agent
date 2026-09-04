# CIMD: hosting CLIO's OAuth client identity (#1285, C1-S5 item 5)

**Status:** deployment artifact, outside `src/` — the CODE hook this document
supports (`MCPAuthConfig.client_metadata_url`) already exists and is
SHOULD-level per the spec (obligations doc row H6, "since v3.0" —
library-covered by the installed MCP SDK); what was missing is the
deployment-side artifact and instructions to actually use it. This doc closes
that gap.

## What CIMD is, and why CLIO needs it

A **Client ID Metadata Document (CIMD)** is the SEP-2352-adjacent alternative
to Dynamic Client Registration (DCR, H7 — DEPRECATED). Instead of registering
a new OAuth client with every authorization server CLIO talks to, CLIO
presents an **https `client_id` URL** that resolves to a small JSON document
describing the client (name, redirect URIs, grant types). The authorization
server fetches that document itself instead of trusting a self-reported
registration payload.

CLIO needs this when a declared MCP server's authorization server:

- refuses DCR (many production ASes disable it, or require pre-registration),
  or
- explicitly advertises CIMD support in its `client_id_metadata_document_
  supported` (or equivalent) discovery metadata (H6: "check `*_supported`" —
  never assume support, read the AS's own advertised capability).

Without a hosted CIMD, CLIO falls back to whatever the target AS's own
pre-registration/DCR path allows — which is exactly the brittle, per-AS-setup
friction CIMD exists to remove.

## The document

A CIMD is a static JSON file, served over HTTPS at a STABLE URL (that URL
*is* the `client_id`). Minimal shape (mirrors `mcp.shared.auth.
OAuthClientMetadata`, the SDK type `MCPAuthConfig.client_metadata` already
accepts):

```json
{
  "client_name": "CLIO Agent",
  "client_uri": "https://clio.<CLUSTER_DOMAIN>/",
  "redirect_uris": [
    "https://gact.<CLUSTER_DOMAIN>/v1/oauth/callback"
  ],
  "grant_types": ["authorization_code", "refresh_token"],
  "response_types": ["code"],
  "token_endpoint_auth_method": "none",
  "scope": "mcp"
}
```

Two fields are **cluster-parameterized** (the "Deployment parameters never
hardcoded" house rule — no raw drive-root/cluster identity ever lands
committed):

- `client_uri` — the deployment's own public identity page/domain.
- `redirect_uris` — the GACT server's OAuth callback endpoint FOR THIS
  CLUSTER (the address the authorization server redirects back to after the
  user approves — must be reachable from wherever the user's browser runs,
  not just from inside the cluster).

Template the document exactly like the existing `deployment.env`-per-cluster
convention (`install/README.md`'s bin/-scripts pattern): keep one
`cimd.<cluster>.json` per deployment target, generated from the template
above with the cluster's own domain substituted in — never a single
hardcoded document checked into source with one cluster's URLs baked in.

## Hosting it

The document must be servable at a stable HTTPS URL with
`Content-Type: application/json` — a static file behind the same reverse
proxy/ingress that already serves the GACT server's own routes is the
simplest option (no new service to operate). It does **not** need to be
served BY the GACT server process itself; any static-file host that keeps the
URL stable across restarts/redeploys works, since the URL is the client's
permanent identity — changing it is equivalent to re-registering as a new
client with every AS that has it cached.

## Wiring it into CLIO

Once hosted, point a declared MCP server's OAuth block at it:

```python
from clio_agent.tools.mcp_config import MCPAuthConfig

auth = MCPAuthConfig(
    client_metadata={"redirect_uris": ["https://gact.<CLUSTER_DOMAIN>/v1/oauth/callback"]},
    client_metadata_url="https://clio.<CLUSTER_DOMAIN>/.well-known/oauth-client.json",
)
```

`tools/mcp_runtime.py::_oauth_provider_from_config` forwards
`client_metadata_url` straight to the installed SDK's
`OAuthClientProvider(client_metadata_url=...)` — no additional clio code is
needed; this document is the only piece that was missing. Token/client-info
durability across restarts is `tools/mcp_oauth_storage.py::
DurableFileTokenStorage` (the factory's default since #1285 C1-S5), keyed by
server URL, independent of whether that server's AS uses CIMD or DCR.

## Verifying support before relying on it (H6's own rule)

Never assume an authorization server accepts CIMD — read its own advertised
metadata first (RFC 8414 AS metadata, `H2` in the obligations doc) for a
`client_id_metadata_document_supported` (or the AS's own equivalent)
capability flag before pointing `client_metadata_url` at a server whose AS
has not confirmed support; an AS that does not support CIMD will simply
reject the unregistered `client_id`, surfacing as a typed OAuth failure at
connect time rather than a silent fallback.
