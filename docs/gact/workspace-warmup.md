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
session-default blueprint. It lists and connects that blueprint's MCP servers
and always-load services concurrently, using the same workspace fleet as later
sessions. No user turn or model request runs. Calls for the same app, root and
blueprint coalesce while preparation is in flight. Failures are logged and may
be retried by subsequent preparation or normal tool use.

Desktop requests preparation when entering a supported draft and once per
minute while it remains visible. This keeps the fleet active while the user
composes, including against a remote agent; Desktop never resolves the remote
root as a local path. Sending does not wait on the preparation request. After
leaving, the ordinary workspace fleet idle reaper controls connection lifetime.
Older agents without the capability continue preparing tools at session creation.
