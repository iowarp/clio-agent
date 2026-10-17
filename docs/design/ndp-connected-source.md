# National Data Platform connected source

NDP is available in Settings → Data sources and the workspace Attach picker.
The account is shared across workspaces on the connected CLIO. The picker
searches datasets, expands their resources and attaches only the selected IDs.
Sources appear in Files and use the existing connected-data tools, immutable
resource custody, downloaded copies and content hashing when bytes are read.
Attaching creates a metadata manifest; it does not download the dataset.

## Authentication

The shipped defaults are the tested **public** Keycloak client `cilo` on
`https://idp-test.nationaldataplatform.org/realms/NDP`. There is no client secret.
CLIO discovers the realm endpoints and uses device authorization with S256
PKCE and `openid offline_access`. It opens the complete verification link,
displays the user code and polls at the realm's interval. `slow_down` adds five
seconds. Cancellation, denial and expiry invalidate the pending flow.

Access/refresh tokens remain in the existing private storage credential vault;
the browser, conversation and tool results receive no tokens. Refresh runs
before expiry under the account owner's lock, preserves/rotates the refresh
token and survives restart. A changed issuer or client requires a new login.
Sign-out attempts refresh-token revocation and forgets local grants even if
the realm cannot be reached. Userinfo supplies a display name only. Token
role claims never determine resource access.

Distributor overrides:

```dotenv
CLIO_STORAGE_NDP_ISSUER=https://idp-test.nationaldataplatform.org/realms/NDP
CLIO_STORAGE_NDP_CLIENT_ID=cilo
```

A production realm needs its own registered public device-flow client. Do not
change the default client spelling to `clio` or substitute the AAI device
wrappers: the supplied working flow uses Keycloak directly.

## Catalogs and Endpoints

Discovery reuses `NDPClient` from the pinned clio-kit science MCP. Without
overrides it uses that client's existing public global catalog. CLIO loads
the installed client; it does not launch another MCP process or maintain a
second catalog protocol implementation.

Configure the deployment's actual Endpoints on the CLIO service, for example:

```dotenv
CLIO_STORAGE_NDP_ENDPOINTS='[{"id":"lab","name":"Lab Endpoint","url":"https://your-endpoint.example/api","server":"local","authenticated":true}]'
```

The example address is a placeholder. Use the actual ep-api base URL, including
any reverse-proxy prefix. `server` selects the existing NDP client's `local` or
`global` catalog. Endpoint IDs must be unique. `authenticated: true` explicitly
permits sending the user's NDP bearer beneath that HTTPS Endpoint URL. Catalog
metadata and arbitrary resource URLs cannot add credential destinations.

CLIO asks each authenticated Endpoint's `/user/info` for its live
`effective_role`, which is derived through NDP's AAI. Viewer/writer/admin give
a Viewer badge; an explicit denial gives Request access. Missing role data
or an AAI outage produces no inferred badge. Browsing remains available.
Only my groups includes Endpoints whose live access check succeeds and reports
when the check cannot be made. These are Endpoint access indications, not a
promise that every resource is readable.

The reported test-AAI role-query failure does not require another client
implementation. Its authenticated reads remain subject to the service's actual
authorization response.

## Reading and custody

The provider dispatches only selected catalog resources:

| Resource | Read route |
| --- | --- |
| HTTP(S) | Stream its URL. Bearer only beneath an explicitly configured HTTPS Endpoint. |
| `pelican://osg-htc.org/...` or `osdf://...` | Endpoint `GET /pelican/download?path=...&federation=osdf&stream=true` |
| `pelican://path-cc.io/...` | Same read route with `federation=path-cc` |
| `s3://bucket/key` | Endpoint `GET /s3/objects/bucket/key` |

Authenticated redirects to another origin receive **no bearer**. Readers own
and close their response/client, use bounded buffers and enforce a 512 MiB
limit. HTTP status errors are sanitized. A denied S3 read explains that current
ep-api policy requires writer access even to download. CLIO still exposes no
NDP write, upload, metadata mutation or deletion operation. Local editable
downloads follow existing copy semantics and never publish back to NDP.

Selected IDs/URLs are saved privately with the source. Files → refresh updates
catalog revisions and signed URLs while retaining the approved resource set
and names. Changed Endpoint configuration requires reconnection. Unknown
Pelican federation mappings and unsupported transports fail explicitly instead
of silently choosing another federation or credential route.

## Qualification

Automated protocol fixtures cover discovery, PKCE, pending/slow-down/terminal
errors, cancellation, restart, concurrent refresh, rotation, revocation and
realm/client ownership. The source integration uses the real clio-kit client
against a local HTTP server, attaches metadata, restarts the storage owner and
reads actual bytes through linked-file custody. Transport tests cover Pelican,
S3, read-only enforcement, access-check outages and redirect credential scope.
Browser fixtures exercise the real repository decoders and picker in both
themes and at a narrow viewport.

These fixtures do not claim a fresh human NDP sign-in or successful protected
reads through an NDP Endpoint. The supplied user script established the device
login path; live Endpoint role assignments and the test AAI remain external
acceptance conditions.

API contracts checked against
[ep-api read authorization](https://github.com/national-data-platform/ep-api/blob/main/docs/roles-and-permissions.md),
[Pelican routes](https://github.com/national-data-platform/ep-api/blob/main/api/routes/pelican_routes.py)
and [S3 object routes](https://github.com/national-data-platform/ep-api/blob/main/api/routes/minio_routes/object_routes.py).
