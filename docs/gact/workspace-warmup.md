# Temporary composer and MCP preparation

The Desktop entry composer has no session identity until the user sends a
message. Opening it, changing its workspace, or visiting Settings must not
allocate a saved conversation.

Clients that see `x_clio_workspace_warmup: true` in GACT 0.3 capabilities can
prepare the selected workspace independently:

```http
POST /v1/workspaces/{workspace_id}/warmup

HTTP/1.1 202 Accepted
Content-Type: application/json

{"status":"warming"}
```

`warming` acknowledges background preparation; it does not claim every server
is ready. `disabled` means `tools.mcp.session_warmup` /
`CLIO_MCP_SESSION_WARMUP` is off. `unavailable` means the agent executor is not
available yet. Unknown workspaces return 404; unavailable roots return 409.

The connected agent resolves its registered workspace root and the persisted
session-default agent (the session defaults' `agent_ref`, else `clio/standard`),
without pinning a session. It lists and connects that agent's plugin MCP servers
and always-load services concurrently, using the same workspace fleet as later
sessions. No user turn or model request runs. Calls for the same app, root and
plugin server set coalesce while preparation is in flight. Failures are logged and may
be retried by subsequent preparation or normal tool use.

Desktop requests preparation when entering a supported draft and once per
minute while it remains visible. This keeps the fleet active while the user
composes, including against a remote agent; Desktop never resolves the remote
root as a local path. Sending does not wait on the preparation request. After
leaving, the ordinary workspace fleet idle reaper controls connection lifetime.
Older agents without the capability continue preparing tools at session creation.

## Preparation without a client request

The agent also prepares the default agent's servers by itself, off every
request path:

- **When the agent becomes ready** (launch, or a model change that rebuilds it), and
  when `PATCH /v1/session-defaults` changes `agent_ref`: a fleet rooted in a
  CLIO-owned folder (`launch-warmup` under the user state directory) provisions,
  starts and lists every plugin server and always-load service of the default
  agent. The listing is stored in the listing cache, and then that fleet is
  closed.
- **When a workspace is created** (`POST /v1/workspaces`): the new root's fleet is
  prepared the same way. The ordinary idle reaper brings it down.

Every workspace fleet preloads the cached listings when it is built. A listing
stored later is adopted by the next turn. So the first turn's model request
already lists those servers' tools, without a "Setting up environment" wait.

A turn that arrives while that preparation is still running does not start a
second one. It joins the running preparation and uses its listing. The first
request still waits only for definitions that are not known yet: it never omits a
declared server that is still listing.
