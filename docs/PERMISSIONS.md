# Permission system

CLIO ships with an interactive permission gate for destructive tools. This
doc describes what's wired, how to use it from the TUI, and how the
auto-resolve paths work.

## Backend surface

| Endpoint | Verb | Purpose |
|---|---|---|
| `/v1/permissions` | GET | List pending + recent permission rows |
| `/v1/permissions/{pid}` | POST | Resolve one (`{"action": "allow"}`, `"deny"`, `"allow_session"`, or `"allow_workspace"`) |
| `/v1/policies` | GET/PUT | List or replace declarative allow/deny/ask policy rules |

Each row carries `id`, `session_id`, `tool_call.{tool_name, input}`,
`summary`, `created_at`, `status`, `action`, optional `resolved_at` +
`reason`.

Policy updates are atomic. `PUT /v1/policies` rejects malformed rows with a
structured `422 invalid_request` and leaves the previous policy set unchanged;
the backend never silently drops typoed scopes/actions or stores rules that it
cannot enforce.

## What triggers the gate

`_make_permission_gate` (in `gact/permission_gate.py`) classifies **positively**:
a provable *read* is always allowed, and everything the gate cannot prove is a
read proceeds to the resolution paths below. There is no tool-name
"destructive" substring list and no shell-command safety parser — both were
deleted (#1032). Writes and network egress are governed by the OS sandbox fence
(codex), not by name matching, so the gate no longer tries to guess
destructiveness from a tool name.

The gate decides in this order:

1. **Read fast-allow (first branch, no mode can override).**
   `grant_resolver.is_read_only(kind, name, args, context)` returns true when a
   tool call is provably read-only — an MCP `readOnlyHint=true` annotation, or a
   static tool-catalog `read` tag with no `write` tag (e.g. `fs_read_file`,
   `fs_propose_edit`). A read returns `allow` **before** the mode lock and
   records no permission row, in every `session.mode`. Reads are never gated.

2. **Auto-deny (read-only session lock)** — when `session.mode` is `plan` or
   `architect`, the gate refuses a non-read call without prompting (read-only
   contract). Row: `status: auto_denied`, `action: deny`,
   `reason: session_mode_readonly`.

3. **Policy resolution** — `grant_resolver.resolve(kind, pattern, …)` (which
   `_policy_action_for_tool` and the egress `_host_action_for` both delegate to)
   consults the declarative `/v1/policies` rules. A matching `deny` blocks; a
   matching `allow`/`allow_session`/`allow_workspace` fast-allows (recorded as a
   resolved audit row).

4. **Interactive** — for an un-resolved non-read call (e.g. an LM-driven ReAct
   loop calling `fs_apply_edit_write` or `shell_bash` mid-turn), the gate
   publishes a `permission.requested` event and blocks on a `threading.Event`
   for up to `DEFAULT_TIMEOUT_S = 600.0` s (a timeout fails safe: `deny`; a call
   with no driving session also fails closed to `deny` immediately). The TUI
   renders a banner; the user resolves with a/d/s/w; the backend POSTs the
   resolution back; the gate returns `allow` or `deny`.

> Note: because classification is positive, a tool that is read-only *in fact*
> but declares neither a `readOnlyHint` annotation nor a catalog `read` tag is
> treated as non-read and routes to steps 2–4. Closing the headless case (no
> interactive approver) for such calls is the job of the approval-mode work
> (#1034); until then, give such tools a `read` tag or `readOnlyHint`.

## TUI surface

- **Banner**: rendered above the conversation pane when
  `len(a.pendingPermissions) > 0`. Source: `app.go:4665`. Style: yellow
  background, bold "⚠ Permission needed: <summary>".
- **Keybindings** (focus must NOT be on input or any open modal):
  - `a` — allow
  - `d` — deny
  - `s` — stop (deny-and-cancel-turn)
  - `w` — always-allow-this-session (whitelists the tool)
- **Handler**: `handlePermissionKey` at `app.go:1918`. Pops the
  oldest pending permission, POSTs the resolution.

This is the current wired behavior. The broader TUI surfacing plan for durable
policy rules, audit visibility, and discoverability is tracked separately in
`archive/PERMISSION_SURFACING_DESIGN.md`. The important distinction is:

- CLIO already has the backend permission system.
- The remaining work is making that system understandable and manageable from
  GACT/TUI without implying permissions are missing.

## Verifying it's wired

End-to-end: propose an edit, press 'a' on the diff body. The backend
records a row visible at `GET /v1/permissions`. Verified live:

```
$ curl -s http://127.0.0.1:17800/v1/permissions | jq '.permissions[0]'
{
  "id": "perm_aa731b837079",
  "session_id": "sess_92043f851558",
  "tool_call": {
    "tool_name": "fs_apply_edit_write",
    "input": {
      "filepath": "/tmp/perm-test.txt",
      "new_content_bytes": 3
    }
  },
  "summary": "diffs/apply: write 3 bytes to /tmp/perm-test.txt",
  "status": "auto_approved",
  "action": "allow",
  "reason": "user clicked /diffs/apply"
}
```

To exercise the **interactive** path (banner + a/d/s/w), the destructive
tool needs to be called by the agent autonomously (LM-driven ReAct loop)
rather than by an explicit user click. Easiest way to force this:

```bash
# Set session.mode to "edit" so the agent has write authority,
# then ask it to edit a file via natural language.
SID=$(curl -s -X POST http://127.0.0.1:17800/v1/sessions \
  -d '{"title":"perm-interactive","mode":"edit"}' \
  | jq -r .id)
curl -s -X POST http://127.0.0.1:17800/v1/sessions/$SID/messages \
  -d '{"parts":[{"type":"text","text":"edit /tmp/perm-test.txt to say bye instead of hello"}]}'
```

When the LM calls `fs_apply_edit_write` directly, the gate fires the
interactive path → TUI banner → user keypress → backend resolves.

## Diff apply write path

`/v1/sessions/{sid}/diffs/apply` keeps its user-click auto-approval and
permission audit row in the GACT layer, then delegates the actual disk write
to the same policy-enforced implementation used by `fs_apply_edit_write`.
This keeps direct tool calls and user-approved diff applies aligned on path
validation, text encoding, and structured failure behavior.

## OS write fence (sandbox) — enforcement below the advisory gate

The permission gate + `file_policy` are the **advisory** layer: they produce
typed, model-actionable errors at the tool boundary on every platform. Beneath
them, the **OS write fence** (`runtime/sandbox.py`, campaign #974) confines
every process the agent spawns so a child cannot write outside its territory
even if it bypasses the tool boundary (`> /etc/x`, a subprocess `open().write`).
The fence is `@anthropic-ai/sandbox-runtime` (`srt`); the advisory `allowed_roots`
and the fence `write_roots` derive from one shared source so they cannot drift.

The two layers are complementary, not redundant: on the advisory floor (no
fence — an HPC login node, a host without `srt`) an out-of-root write succeeds
and is recorded as a provenance `gap`; under an active fence the same write is
DENIED at the OS (`EROFS`/`EACCES` on Linux/macOS, `WinError 5 /
ERROR_ACCESS_DENIED` on Windows) and minted as a typed `policy_violation`
attributed to the child, path, and call window. Every degradation is labeled —
no fence is ever *silently* absent.

### Windows setup (one-time UAC)

srt is the Windows fence path (no native restricted-token implementation is
built). Unlike Linux/macOS — where the fence activates automatically per
process — Windows needs a **one-time, self-elevating, idempotent** provisioning
step that creates the `srt-sandbox` principal + WFP filters:

```powershell
clio sandbox setup      # ONE UAC prompt; per-session use afterward is unprivileged
clio sandbox status     # mechanism + typed reason + next action (no elevation)
```

`setup` self-elevates exactly once. A re-run detects the already-provisioned
state and no-ops with **zero prompts** (safe to run twice). Preconditions are
typed and guided: if `srt` or Node.js (>= 20.11) is missing, `setup`/`status`
print the exact install pointer (`npm install -g @anthropic-ai/sandbox-runtime`)
rather than a raw error. Enterprise policy that blocks elevation leaves the
advisory floor in place, honestly labeled. `clio sandbox` is reachable from both
the `clio-agent` console entry point and the desktop `clio` launcher.

### Network chokepoint + deny mode (#978/#979)

Child egress defaults to **ALLOW + RECORD**: every spawned child routes through one
clio-owned CONNECT-only proxy (`runtime/net_chokepoint.py`), and each connection is
recorded as a trace-only `net.egress` event and joined into provenance as a
`used web:<domain>@<time>` ingest edge. On an srt tier the OS fence FORCES children
through the proxy (`proxy-enforced`); on Landlock/floor tiers egress is proxy-ENV
cooperation only (`env-cooperative`, raw sockets bypass) — the per-edge label never
claims enforcement the tier can't provide.

**Deny-by-default** is an opt-in per-workspace mode: egress is refused unless the
domain is granted, and the first connection to a new domain raises a
`network_egress` permission request (grant-on-first-domain). The `host_pattern`
vocabulary scopes a domain grant.

### Grants on the record (#979)

A grant is a **user/model decision** (never a clio heuristic), recorded as a
`boundary.*` semantic event with grantor + sticky-policy provenance, reusing the
existing permission gate + policy store (a new request *kind*, not a new gate):

| Endpoint | Verb | Purpose |
|---|---|---|
| `/v1/workspaces/{wid}/grants` | POST | Grant a writable root (`{"root": "<dir>"}`) or a domain (`{"host_pattern": "<pattern>"}`) |

A root grant widens the fence `write_roots` and restarts the workspace's resident
fleet so an already-spawned, workspace-shared child picks up the new territory at a
safe boundary (a busy fleet is never torn down mid-call — the restart defers and is
reported as a typed `grant_restart_deferred_busy`, never silently — #1033). A denied
`policy_violation` carries a
static grant affordance (`next_action`) so the model may *ask* and the user decides.

### Provenance tiers under the fence (#980)

The fence upgrades #966's honest-but-toothless floor into enforced guarantees, all
per-record and honestly labeled:

- an out-of-root write is a typed `policy_violation` (`prevented` / `detected`) under
  a fence, an honest `gap` on the floor;
- a generated (written) edge is marked **`fence_proven`** when the fence made this
  call's output territory exclusive by construction (its `write_roots` were disjoint
  from every other concurrent actor's during the window) — plain `lease-window`
  otherwise; a `contended` record (two fenced actors sharing a granted root) is never
  `fence_proven` (the fence narrows exclusivity, never fakes it);
- egress becomes a `used web:<domain>@<time>` ingest edge.

### Conformance (#980)

`clio doctor` reports a **`sandbox_conformance`** row: the zero-untyped-degrade
guarantee. It walks every child-spawn seam (the three wrapped seams + the three
verifiably-excluded seams — CTE daemon, provider links, `serve`) and asserts a TYPED
mechanism + reason on each, on whatever tier resolved. READY = every seam typed
(including on the honest floor); DEGRADED (loud) only if a seam ever resolved to an
`unknown` mechanism or a blank reason — the campaign-forbidden silent passthrough.
