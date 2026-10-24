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
a provable *read* inside permitted file territory needs no action approval, and everything the gate cannot prove is a
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
   `fs_search_text`). A read returns `allow` **before** the mode lock and
   records no tool-action permission row, in every `session.mode`. This does not
   grant access to a new folder: a managed built-in file call outside permitted
   roots parks on a `kind="folder_access"` request first.

   Folder requests show the exact operation, command, computer, paths, reason,
   and workspace duration. Only a human `allow_workspace` applies the existing
   persisted root grant and wakes the original call; `deny` returns an actionable
   refusal. No broad tool allow policy is derived. The Files browser includes
   granted folders; actual consumed/produced files use the artifact identity and
   version pipeline without bulk indexing. `shell_bash` declares `access_paths`
   and `access_reason`, including both ends of a move. Built-in calls and newly
   spawned shell children see grants immediately; leased external server fleets
   retain the existing drain-aware restart. Arbitrary shell reads remain outside
   the write fence's universal coverage.

2. **Policy resolution** — `grant_resolver.resolve(kind, pattern, …)` (which
   `_policy_action_for_tool` and the egress `_host_action_for` both delegate to)
   consults the declarative `/v1/policies` rules. A matching `deny` blocks; a
   matching `allow`/`allow_session`/`allow_workspace` fast-allows (recorded as a
   resolved audit row).

   Plan/architect enforcement lives here too, not as a separate lock step: when
   `session.mode` is `plan` or `architect`, `resolve()` also consults a pair of
   built-in, never-persisted `plan_acl` rows — deny every non-read tool in
   plan/architect, with the sole `<plans>/*.md` write carve-out in plan mode.
   These rows carry a **dynamically computed priority**, raised above every
   matching user policy row for that call (so a permissive `/v1/policies` entry
   — however it was prioritized or migrated — can never outrank the mode lock;
   the carve-out is raised one band above the deny so it still wins for the
   plan file itself). A resulting denial is recorded exactly like any other
   policy denial: `status: auto_denied`, `action: deny`, `reason: policy_deny`.
   There is no `session_mode_readonly` reason and no separate predicate — this
   is the same one-matcher path every other policy row resolves through.

3. **Interactive** — for an un-resolved non-read call (e.g. an LM-driven ReAct
   loop calling `fs_write_file` or `shell_bash` mid-turn), the gate
   publishes a `permission.requested` event and blocks on a `threading.Event`
   for up to `DEFAULT_TIMEOUT_S = 600.0` s (a timeout fails safe: `deny`; a call
   with no driving session also fails closed to `deny` immediately). The TUI
   renders a banner; the user resolves with a/d/s/w; the backend POSTs the
   resolution back; the gate returns `allow` or `deny`.

> Note: because classification is positive, a tool that is read-only *in fact*
> but declares neither a `readOnlyHint` annotation nor a catalog `read` tag is
> treated as non-read and routes to steps 2–3. Closing the headless case (no
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

## The agent writes files itself

The agent changes files with two tools shaped like Claude Code's Write and Edit:
`fs_write_file` (create a file or replace its content) and `fs_edit_file`
(replace an exact string; a missing or ambiguous `old_string` is refused with a
typed error, never fuzzy-matched). Both change the file on disk when they return.
They declare a destructive, closed-world write, so they carry the catalog `write`
tag:

- the gate never fast-allows them; the session's approval mode decides. In `ask`
  mode the person is asked; in `auto-edits` mode the write is allowed and an
  audited `auto_approved` row is still recorded; a deny policy or the
  plan/architect lock refuses it (in plan mode only the recorded plan file may be
  written);
- PreToolUse hooks run on every write, including a Claude Code plugin's guard on
  `Write|Edit`: the hook sees `tool_name` `Write` or `Edit` and Claude Code's
  `tool_input` keys (`file_path`, `content`, `old_string`, `new_string`,
  `replace_all`). A hook deny blocks the write, and its reason is what the agent
  reads and what the `hook_deny` audit row carries.

## The record of changes, and Undo

Each successful write is recorded as an applied `file_diff`: a part in the turn's
transcript where the change happened, and a row on the session's change ledger
listed by `GET /v1/sessions/{sid}/diffs`. The diff panel shows that list with an
Undo action, `POST /v1/sessions/{sid}/diffs/revert` with body `{"id": ...}`. Undo
restores the previous content (or deletes a file the write created) only while the
file is still exactly what the agent wrote. It goes through the same boundary as
any write: the session's workspace root, the file policy, and the policy resolver
(as an `fs_write_file` call, so a deny policy or plan mode refuses it). It records
an audited permission row (`reason: user_requested_revert`) and publishes
`file.diff.reverted`. Every refusal is typed:

| Error | Status | Meaning |
|---|---|---|
| `file_change_not_found` | 404 | No recorded change with that id in the session |
| `already_reverted` | 409 | The change was undone before |
| `undo_unavailable` | 409 | No previous content was kept; `details.reason` says why (for example `previous_content_too_large`, over 256 KiB) |
| `file_changed_since_write` | 409 | The file changed after the write; Undo would overwrite that change |
| `path_outside_workspace`, `file_policy`, `policy_denied` | 403 | The write boundary refused the restore |

The previous content is captured at the call boundary and kept on the server, not
in the tool result the model reads. The ledger lives in memory: after a server
restart the transcript still shows each change, but it can no longer be undone.

To see the interactive path, ask the agent to edit a file in a session in `ask`
mode:

```bash
SID=$(curl -s -X POST http://127.0.0.1:17800/v1/sessions \
  -d '{"title":"perm-interactive"}' | jq -r .id)
curl -s -X POST http://127.0.0.1:17800/v1/sessions/$SID/messages \
  -d '{"parts":[{"type":"text","text":"edit /tmp/perm-test.txt to say bye instead of hello"}]}'
```

When the model calls `fs_edit_file`, the gate publishes `permission.requested`,
the UI asks, and the person's answer resolves the call.

## OS write fence (sandbox) — enforcement below the advisory gate

The permission gate + `file_policy` are the **advisory** layer: they produce
typed, model-actionable errors at the tool boundary on every platform. Beneath
them, the **OS write fence** (`runtime/sandbox.py`, campaign #974) confines
every process the agent spawns so a child cannot write outside its territory
even if it bypasses the tool boundary (`> /etc/x`, a subprocess `open().write`).
The primary fence is the Codex sandbox; Linux can fall back to Landlock.
The advisory `allowed_roots` and fence `write_roots` share the same territory
resolver. Windows uses verified native MXC when available or the legacy
restricted-account backend. A runtime upgrade invalidates version-specific proof.

### Windows preparation and progress

The backend prepares Windows protection off the event loop before constructing
its agent and tool fleet, including developer and headless launches. Startup
qualifies native MXC or checks existing restricted accounts without requesting
UAC. Packaged installation and explicit **Set up protected execution** can request
administrator approval for first-time legacy account creation. Missing or failed
verification stays degraded; protection is never inferred from account presence.

Startup, installer, button and CLI call the same provisioning engine. Its shared
runtime-access cache checks directory identity and the root ACL before reuse;
changed roots are repaired with one traversal for both restricted users. Allowing
UAC does not force a fresh native probe. A failed access grant is unsuccessful
for every entry point.

Open **Infrastructure > CLIO hosts > Protected execution**, or run:

```powershell
clio sandbox setup
clio sandbox status
```

The service retains the current setup step, start time, elapsed time and outcome
in `GET /v1/system/sandbox`'s `setup_progress`. The UI follows progress across route
changes. A successful setup after MCP processes already connected reports that a
restart is required; an HTTP timeout does not imply the setup worker stopped.

**Bypass checks** changes action approvals only. Connected-source exclusions
(`runtime/storage_access.py`) refuse shell/MCP spawning when the active fence
cannot protect source credentials or read-only source roots. The advisory floor
is therefore not an unconditional permission to spawn arbitrary processes.

Approved downloaded source copies are readable by file tools and fenced shell
commands, including Python analysis. Declaring a source in `access_paths` does
not request write access or add a writable folder grant. The OS fence keeps the
source read-only; save derived tables, plots and reports in the workspace.
File edit tools still reject writes to protected source copies before dispatch.
Private credentials remain excluded from both reads and writes. If the host
cannot enforce these exclusions, shell execution fails before starting a child.

### Network chokepoint + deny mode (#978/#979)

Agents with `shell_bash` also receive `ssh_execute` for remote commands. An
agent may explicitly declare `ssh_execute` without receiving local shell
access. Read-only side sessions do not receive this effectful tool. It reuses
the shared SSH profile/custom route and jump chain, existing keys/agent and
strict known-host verification. Approval names the route and exact remote
command before connecting; **Bypass checks** records the usual automatic
approval. Private keys are not copied into the shell workspace.

This is a trusted SSH transport operation, separate from a local shell's
HTTP-proxy network fence. A raw `ssh` subprocess does not automatically use
HTTP proxy variables, so approving a local shell command alone cannot make
its direct TCP connection work. The managed `ssh_execute` connector provides
finite remote execution through CLIO's configured routes. Ordinary shell SSH
is still a valid capability, but proxy integration for the enforced network
route remains unresolved; this tool does not establish Bash/OpenSSH parity.
Use the NDP connector for NDP service operations. Neither requires disabling
the local sandbox. Remote commands run with the SSH account's permissions;
the local workspace write fence does not constrain the remote filesystem.
The tool captures both output streams, spills large output into session-owned
files and reports the real exit status. Its finite command deadline or
cancellation closes the SSH channel, but does not prove remote descendants
stopped; inspect remote state before retrying an uncertain command.

The normal tool catalog describes its command, profile/custom route, command
deadline and output limits. Its typed result contains the normalized route,
stdout/stderr, exit status, stop reason, truncation flags and optional spill
receipts. Command input and actual output use the existing terminal blocks,
including an explicit empty-capture message when both streams are empty.
See [SSH contract and qualification](verification/ssh-agent-execution-2026-10-19.md#contract-and-presentation-follow-up).

Child egress defaults to **ALLOW + RECORD**: every spawned child routes through one
clio-owned CONNECT-only proxy (`runtime/net_chokepoint.py`), and each connection is
recorded as a trace-only `net.egress` event and joined into provenance as a
`used web:<domain>@<time>` ingest edge. On the legacy Codex tier the OS fence forces external traffic
through the proxy; native MXC records external proxy enforcement and direct host
loopback separately; on Landlock/floor tiers egress is proxy-ENV
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
