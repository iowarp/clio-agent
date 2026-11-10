# Existing NDP Endpoints and service operations

NDP catalog datasets and Endpoint services are separate capabilities. The
dataset picker retains the normal Download/Link workflow. Its **Endpoints &
streams** tab connects an existing API and operates it, without installing a
service or starting containers.

## Connections

Connections belong to one workspace and the connected CLIO's OS principal.
The settings are durable in the existing connected-source ledger. Both the UI
and native agent tools use the same owner and saved operation contract.

- Direct URL: an HTTP(S) API base such as `https://host/gnss`.
- SSH: the shared SSH route selector offers saved CLIO hosts and explicit
  OpenSSH aliases on the connected CLIO. Use **Add hop** for a destination
  behind a login host, or type/configure a custom computer, username, port
  and key-file path. The final computer hosts the remote loopback URL:
  `ares → ares-comp-11` with `http://127.0.0.1:8002` addresses the API on the
  compute node. The Endpoint saves a reviewed snapshot of the shared `SshRoute`,
  so later edits to a saved host do not silently change an approved destination.
  Existing single-alias Endpoint connections remain supported. The existing
  SFTP SSH resolver supplies profile routing, keys/agent and strict host-key
  verification. Requests use short-lived direct-tcpip channels. They expose
  no listening local port and close the response, channel and SSH owner.
- Authentication: public discovery or the user's existing private NDP
  account. Authenticated direct connections require HTTPS; SSH connections
  protect the remote loopback hop. Tokens go only to the approved destination,
  with no redirects. CLIO refuses token forwarding through NDP's service proxy,
  whose implementation passes request headers to upstream services. Connect
  the service directly when its authentication is intended.

The editor calls this **API sign-in**. **No NDP sign-in** omits the NDP account
token; **Use my NDP sign-in** sends the saved account token to that API. This
does not grant an Endpoint role or organization membership. SSH login is a
separate authentication layer and still applies to either API sign-in choice.

CLIO observes `/openapi.json`; it does not accept arbitrary model-authored
method/path combinations. Returned operation IDs combine the method and path,
so duplicate or absent upstream `operationId` fields remain unambiguous.
Parameter and JSON-body schemas are expanded from local references and
validated without fetching external schema references. NDP's registered-service
proxy additionally accepts a `query` object for its documented upstream query
forwarding behavior.

## Agent workflow

`connected_data_status.ndp_endpoints` inventories saved connections.

1. `connected_data_ndp_endpoint_connect(name, url, ssh_profile, authentication, ssh_route)`
   requests normal approval of a new destination and discovers its operations.
2. `connected_data_ndp_endpoint_operations(endpoint_id, query)` filters the
   observed operation descriptions and input schemas.
3. `connected_data_ndp_endpoint_request(endpoint_id, operation_id, parameters,
   body, max_seconds, max_events)` requests approval of the exact operation,
   including its destination and inputs, then runs it. Denial returns control
   without performing the request or saving a capture. Private account setup
   uses the ordinary NDP sign-in control.

The `ndp` plugin's `work-with-ndp` skill teaches catalog search, registered services,
FDSN station queries, finite SSE segments and live-bridge session ownership.
Endpoint connection is always available to agents carrying the plugin; operations,
requests and uploads load once the workspace has a connected NDP dataset or a
visible saved Endpoint.
Starting a remote producer is not consuming its output. A model must retain the
returned session identity, inspect actual status/data and stop only the session
it owns. Closing a bounded local sample does not stop another remote producer.

## Response custody and limits

Successful operations retain the actual response bytes as an immutable
workspace resource and publish a normal Sources artifact and local input copy.
The receipt records endpoint/workspace/operation IDs, inputs, timestamps, HTTP
status, MIME type, byte count, stream-event count, stop reason and SHA-256.
Captured responses remain inspectable after disconnecting their live endpoint.
The UI offers openable results and capture history; agent resource and file
tools use the same retained data.

Defaults are 10 seconds and 100 SSE messages. Maxima are 30 seconds, 1000
messages and 2 MiB of response bytes. A timer interrupts even a silent socket;
the response owns and retires that timer. HTTP/SSH connection establishment has
its own bounded transport timeouts. SSE counts include control messages such
as `hello` and `done`. Time/event/byte limits are explicit receipt outcomes.
Normal finite completion preserves `done` resume cursors. Preview text is
bounded separately and does not claim to contain an entire large response.

## Live qualification on 2026-10-18

The existing Ares Endpoint (`ep-api` 0.19.5) and Kafka broker were running.
Its local CKAN nginx container was stopped; restarting that existing container
restored local search and readiness. No replacement Endpoint was installed.

The real local CLIO build connected to Ares through verified SSH, queried its
local catalog, discovered `earthscope-fdsn-station`, and fetched filtered
`IU.ANMO` station metadata through the registered-service proxy. Responses were
retained as resources, materialized and registered as Sources artifacts.

The UI also connected directly to the published EarthScope on-demand API and
captured three records from `WWMT.CI.LY_.40`. Its resume cursor then returned
the next two records, starting at index 3, without repeats. These measurements
date to 2024-12-03: this is archived CSV replay over SSE, not current live Kafka.
The first receipt contains 911 bytes/5 SSE messages; the resumed receipt contains
706 bytes/4 messages. Independent local hash and timestamp checks use the
retained originals.

The live EarthScope bridge API exposed its available Kafka topics and a catalog
of station codes. Its protected live station-sampling operation returned HTTP
401 without an NDP login. Public metadata/replay checks do not establish
protected Kafka consumption or a production Endpoint role. The operator was
offered the existing private sign-in flow for that separate live check.

Local qualification includes real TCP HTTP/SSE tests for bounds, absolute
deadlines, byte preservation, schema validation, denials, redirect handling,
workspace/principal ownership, restart retention and source materialization;
real gate tests exercise approval and readable operation summaries. UI tests
cover explicit SSH connection, immediate operation filtering, typed optional
parameters, state-change review and openable captures. Live browser and actual
model-driven acceptance evidence is retained under the operator's private
`D:/CLIO-Develop/ndp-live-20261018` directory.
