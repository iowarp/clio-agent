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
   `fs_search_text`), or a fenced `shell_bash` command made only of listing and
   reading commands (`Get-ChildItem`, `Get-Content`, `Test-Path`, `Select-String`,
   `ls`, `cat`, `grep` and their aliases, optionally piped through `Select-Object`,
   `Sort-Object`, `Format-*`, `Measure-Object`, `head` or `wc`) with no redirection
   other than discarding stderr, no subexpression, script block, grouping or call
   operator, and no pipeline stage outside that list
   (`clio_agent/tools/shell_read_only.py`). Anything the classifier cannot prove
   stays a non-read. A read returns `allow` **before** the mode lock and
   records no tool-action permission row, in every `session.mode`. The durable
   task check that accepts the call (`task_runtime.authorization_mode`) agrees:
   a read is accepted with no plan lock, so a plan-mode shell read is not revoked
   after the gate allowed it; person policies still apply. This does not
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
   publishes a `permission.requested` event and waits for the answer with no
   expiry: the wait counts as the turn's progress, and only the answer or
   cancelling the work ends it (`gact/permission_timeout.py`; the old 600 s
   timeout is removed, taskable `run_code` CT13). A caller given an explicit
   deadline (the PreToolUse hook defer, `hooks.defer_timeout`) is denied with
   `permission_request_timeout` when it passes, which is not the same as the
   person refusing. A call with no driving session fails closed to `deny`
   immediately. The TUI
   renders a banner; the user resolves with a/d/s/w; the backend POSTs the
   resolution back; the gate returns `allow` or `deny`.

> Note: because classification is positive, a tool that is read-only *in fact*
> but declares neither a `readOnlyHint` annotation nor a catalog `read` tag is
> treated as non-read and routes to steps 2–3. Closing the headless case (no
> interactive approver) for such calls is the job of the approval-mode work
> (#1034); until then, give such tools a `read` tag or `readOnlyHint`.

### MCP servers that confirm calls themselves

A person sees one ask per call. When a server also asks during a call, CLIO
settles the two asks into one:

- **Plain confirms are answered by the person's own permission.** A server's
  confirm with nothing to choose (no fields, or one boolean) that is correlated
  to a call a *person* allowed is answered from that permission and recorded as
  `mcp_confirm_covered_by_host_permission` (`gact/mcp_confirm_coverage.py`).
  The server's message shows as a note under that call. A confirm with fields,
  choices or numbers, one carrying typed `_meta` (such as a job plan), or one
  for a call only a policy allowed still asks.
- **Server-confirmed tools get the server's card, not CLIO's yes/no.** A tool
  whose `tools/list` entry declares `_meta["io.iowarp/confirm"] = {"version": 1}`
  confirms itself. When CLIO would ask a person directly about such a call, it
  records the call as `mcp_confirm_deferred_to_server` and sends it with
  `params._meta["io.iowarp/confirm"] = "server"`. The server then asks exactly
  once before any side effect, or refuses `needs_approval` when it cannot ask
  (`gact/mcp_server_confirm.py`, `tools/server_confirm.py`). Unattended runs,
  the ai-review reviewer, `always_ask` decisions and any rule or mode that
  already decides keep CLIO's own path. A tool without the declaration, or
  with another version, is never deferred to.
- **A server that skips its card stops being trusted.** A flagged call that
  completes without an ask, and without result `_meta["io.iowarp/confirm"] =
  "not_needed"`, is recorded as `mcp_server_confirm_missing`. A deferred call
  whose server stopped declaring the confirm is not sent and is recorded as
  `mcp_server_confirm_undeclared`. Either way CLIO asks about that server's
  calls itself for the rest of the session. Failed or refused calls report
  their own error and are not counted. The check is held in memory, so a call
  that settles after an agent restart is not checked.

## Scripts and per-tool settings

**A script is approved before it runs.** `run_code` is one non-read call: it asks the
gate itself, with the script shown ("Run a Python script (N lines): ..."), in every
approval mode except `bypass` (`gact/code_mode/tool.py`). That approval covers what the
script does directly (`open()`, subprocesses) inside the write fence, as a shell
approval covers its command.

**Each call a script makes still meets the gate** with its own annotations
(`gact/code_mode/grants.py`). Reads do not ask unless a rule or a default says so;
denies hold. A nested non-read call that would ask is decided by the approval mode the
script was accepted with (a change of mode while it runs does not change it):

| Approval mode | Nested non-read calls |
| --- | --- |
| `bypass` | Run |
| `auto-edits`, `ai-review`, `spotter-ai`, `spotter-watcher` (`code.covering_postures`) | Covered by the script's approval, recorded with `run_code_script_grant` |
| `ask` | A person's "Allow once" on the script covers the calls its code names (`tools.<group>.<tool>` or `tools.call("<name>")`), recorded with `run_code_script_grant`. Any other call asks, one question per script at a time, showing the script; denial raises in the script |

In `ask` mode the question offers "Allow for the rest of this script"
(`allow_script`), which covers that script's remaining calls, named or not; the next
`run_code` asks again. A script approved by a rule or the reviewer rather than a person
covers nothing in `ask` mode. A tool whose rule or default is `ask` asks even under a
covering mode or a person's approval of the script. A
background script's question belongs to its task: it is asked in the owner's
conversation with the task's handle, and only an answer or cancelling the task ends
the wait. In plan mode a script runs only where the sandbox enforces the write fence:
its kernel may write only the session's scratch folder and temp, and its nested calls
meet the plan-mode rules like direct calls; elsewhere `run_code` refuses with
`code_plan_mode_unconfined`.

**Every tool has a default permission** (`allow`, `ask` or `deny`), declared by its
plugin (`native_tool(permission=...)`, `tool(permission=...)`,
`servers.<name>.permission`, `servers.<name>.tools.<tool>.permission`) and replaced by
the deployment's `tools.permissions` (tool name to permission; `CLIO_TOOLS_PERMISSIONS`
as JSON). Undeclared, the tool's annotations and the approval mode decide. The gate's
order lives in one place (`gact/runtime/tool_permission_layers.py`): a deny from any
layer wins; then the person's rules (`/v1/policies`); then the tool's default; then the
approval mode and a script's grant. So a plugin can keep `send_email` on `ask` in
`auto-edits`; `bypass` skips a default's question but never a deny.

**A built-in that acts outside CLIO follows the approval mode like an MCP tool.** A
native tool declares it with `native_tool(outside=True)`: it drives a server, a view the
person sees or a connector's state, and does not ask by a card of its own. Today a
view's own tools (`app__<server>__<tool>`) and `set_connector_value` declare it. Their
calls pass the whole gate (`native_gate` in `gact/runtime/tool_permission_layers.py`):
in `ask` each asks, titled with the tool's title, with allow once, for this session or
for this workspace (a rule for that exact tool name, so once per tool); in `bypass` none
asks; a view tool annotated `readOnlyHint` asks nothing unless a rule or default says
so. Other built-ins (`search_tools`, `load_skill`, plans, todos, memory) are CLIO's own
bookkeeping: only a rule or default makes them ask or refuses them. Built-ins that act
outside but ask by their own gate call or card keep doing so without the declaration
(`connected_data_connect`, `run_code`, `offer_plugin`'s card).

**Deny hides the tool.** A tool whose default is `deny`, or that a person's rule names
exactly and denies, is left out of the agent's tools: the tool list, the deferred
catalog and the `run_code` stubs, traced `tool_permission_deny`; a call to it by name is
refused with that reason (`gact/agents/tool_settings.py`). A wildcard deny rule keeps
denying at the gate, call by call.

**The plugin's Tools settings page** (`GET`/`PUT /v1/settings/tools`,
`POST /v1/settings/tools/rules/remove`; `gact/tool_settings_page.py`) lists a plugin's
tools grouped into read-only and write tools by their flags, with each tool's plugin
default, the deployment value, the person's saved rules and their scope, and what
happens in each approval mode for a direct call and for a call from a script. The
person's permission is saved as a policy rule in `/v1/policies`; the I/O choice
(`callers`: `direct`, `code` or `both`) is saved as `tools.callers` in the user
`config.yaml`. `?server=<name>` gives the same page for one MCP server that no plugin
brings (a person's or workspace's own); its tools have no plugin default, so their
default comes from the server's MCP annotations.

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

### Git repositories

Neither the agent's commands nor its file tools can change a repository's
`.git`. Under the OS fence, codex mounts `.git` read-only in every writable
folder (and an empty placeholder where none exists). The file tools run in
CLIO's own process, outside the fence, so their policy refuses any path with a
`.git` component, including a `.git` file, with the typed code
`git_metadata_protected`. Both rules exist because hooks and config in `.git`
run later, unsandboxed, whenever the person uses git.

Recording changes goes through `fs_git_commit` instead. CLIO runs git itself.
The tool is a closed-world write: it asks in `ask` mode and is allowed in
`auto-edits` mode, like an edit. It stages and commits the changes under the
folder, or exactly the named files, and creates the repository only when called
with `init`. Every git step runs with `core.hooksPath` set to an empty folder,
fsmonitor and automatic maintenance off, and no editor or terminal prompt, so
nothing in the repository executes. An enclosing repository is used only when
it lies inside the conversation's workspace. With `init`, a folder inside
someone else's repository gets a repository of its own; without `init`, the
call is refused with `repository_outside_workspace`. The commit uses the
person's git identity. If git has none, the call is refused with
`identity_unknown`, and `author` supplies one only from a name and email the
person gave.

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

### Start-up acknowledgement when the fence is off

CLIO fails open: when no fence resolves, the agent still runs, with only the
advisory layer. Because that is a security risk, every start that leaves the
fence off must be acknowledged by the user. `clio`, `clio web`, `clio start`
and `clio restart` print a security warning once the server has resolved its
backend. On an interactive terminal they ask
`Continue without protected execution? [y/N]`; anything but yes stops a server
the command started and exits 1. Runs without a terminal only warn, and
`CLIO_ACCEPT_UNSANDBOXED=1` acknowledges without the prompt. Desktop and Web
show the same verdict as a banner.

`GET /v1/system/sandbox` carries the verdict. It answers during start-up,
before the other `/v1` routes stop returning 503:

| Field | Type | Meaning |
|---|---|---|
| `active` | bool | Agent tools run inside an OS write fence now. False on the floor, and while a just-activated fence still needs a restart. |
| `resolved` | bool | A server boot has resolved the backend. Until it is true, wait and ask again; do not warn yet. |
| `mechanism` | string or null | `codex`, `landlock` or `none`; null until resolved. |
| `reason` | string | Typed reason token, for example `codex_not_installed` or `sandbox_fence_pending_restart`. |
| `acknowledgement_required` | bool | `resolved and not active`: show the warning and require acknowledgement. |
| `warning` | string or null | Banner text, present exactly when `acknowledgement_required` is true. |
| `summary` | string | Technical one-line description of the row. |
| `next_action` | string | The step that turns protection on, for example reinstalling CLIO Desktop. |
| `status` | `ready`, `degraded` or `unavailable` | The doctor row's state, as in `/v1/health`. |

The same row in `GET /v1/health` `integrations[]` (`name: "sandbox"`) carries
`active`, `acknowledgement_required`, `warning`, `reason`, `summary` and
`next_action`; those three new fields are null on every other row.

### Windows preparation and progress

The backend prepares Windows protection off the event loop before constructing
its agent and tool fleet, including developer and headless launches. Startup
qualifies native MXC or checks existing restricted accounts without requesting
UAC. Packaged installation and explicit **Set up protected execution** can request
administrator approval for first-time legacy account creation. Missing or failed
verification stays degraded; protection is never inferred from account presence.

MXC refuses to launch packaged (MSIX) apps, so CLIO cannot qualify it while it
runs on Microsoft Store Python, including a virtual environment built on it. Setup
then reports `codex_mxc_packaged_python` at once instead of a launch failure. CLIO
and `install.ps1` set `UV_MANAGED_PYTHON=1` on Windows so uv builds environments on
its own Pythons. For a source checkout already built on Store Python, run
`uv sync --managed-python`. When Codex no longer accepts the existing legacy
accounts' setup, it waits for administrator setup instead of starting commands.
CLIO reports this as `codex_windows_setup_required` and stops repeating that check
at startup. Explicit **Set up protected execution** requests UAC to refresh the setup.

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

**SSH from the shell.** Remote work uses the ordinary `ssh`, `scp`, `sftp` and
git-over-SSH commands in `shell_bash`; CLIO has no separate SSH tool. Inside an
enforced fence the shell's `ssh`, `scp` and `sftp`, and those of fenced MCP servers,
resolve to small shims
(`runtime/fence_ssh.py`) that run the real program with CLIO's CONNECT helper
(`runtime/net_connect.py`) as its `ProxyCommand`. Git reaches the same shim through
`PATH`, or through `GIT_SSH` when the person has not set an ssh command of their own
(`GIT_SSH_COMMAND` and `core.sshCommand` still win). A `ProxyCommand` from the
person's ssh config is kept, and a `ProxyJump` chain is rewritten so every hop goes
through the helper. The connection therefore crosses the chokepoint below like any
other egress: it is recorded as `net.egress` with its host and port 22, and a host
outside the workspace's network policy is refused with an explicit message instead
of a hang. In deny mode a host with no grant raises the same `network_egress`
request as any other domain, and the connection waits open until the person allows
it once or always (the helper says it is waiting) or declines it. The person's keys,
agent and `known_hosts` are used as they are; nothing is copied into the workspace.
Remote commands run with the SSH account's permissions, and the local write fence
does not constrain the remote filesystem.
On Windows each shim is an `.exe` console launcher (distlib's, as pip uses) with a
`.cmd` beside it. `CreateProcess` — Python's `subprocess` with a list, Node's `spawn`
without a shell — tries only `.exe` for a bare `ssh`, so without the launcher such a
spawn reaches System32 OpenSSH, whose direct connect the fence blocks. The launcher
also passes arguments verbatim; the `.cmd` goes through `cmd.exe`, which expands
`%VAR%` and strips `^` in a remote command, so `GIT_SSH`, `scp -S` and `ProxyJump`
hops use the `.exe` too.
On the floor (no fence) the shims are not installed and `ssh` connects directly.

Results recorded by the former built-in SSH tool in old sessions keep their
recorded presentation and render through the generic tool view.

Child egress defaults to **ALLOW + RECORD**: every spawned child routes through one
clio-owned CONNECT-only proxy (`runtime/net_chokepoint.py`), and each connection is
recorded as a trace-only `net.egress` event and joined into provenance as a
`used web:<domain>@<time>` ingest edge. On the legacy Codex tier (Windows) CLIO
labels egress `proxy-enforced`; native MXC records external proxy enforcement and
direct host loopback separately; on macOS Codex and on Landlock/floor tiers egress is
proxy-ENV cooperation only (`env-cooperative`, raw sockets bypass) — the per-edge
label never claims enforcement the tier can't provide.

On Linux the Codex fence runs each child in its own network namespace whose only
route out is Codex's managed proxy (`[features] network_proxy`, SOCKS and local
binding off), and that proxy chains every connection to the child's CLIO chokepoint
port. An `ssh` called by absolute path, `curl --noproxy '*'` and raw sockets
therefore fail, and everything that does connect is recorded and subject to deny
mode. The fence probe (`sandbox_codex_probe.probe_codex_linux_fence`) proves this at
startup: its child must fail to reach a listener on the host's loopback. Only then
is the tier labelled `proxy-enforced`; if the child can still reach the host network,
CLIO keeps the write fence, reports `codex_linux_net_unisolated` in
`details["codex_net"]` and labels egress `env-cooperative`. If the per-child
chokepoint port cannot be opened, the child's proxy is pointed at a closed port, so
it stays offline rather than connecting unrecorded. Codex's proxy passes only public
hosts on to CLIO, so for everything else the fenced `ssh`, `scp`, `sftp` and Git over
SSH use a relay: the CONNECT helper (told by `CLIO_EGRESS_RELAY`) first opens a
tunnel to the never-routed documentation address `[2001:db8::c110]:443`, which Codex
forwards to the child's chokepoint port, and CLIO's proxy answers that tunnel itself
and reads the real `CONNECT` from inside it. A private, link-local or loopback host
reached this way (an SSH login node on a cluster network, say) always needs consent,
in or out of deny mode: unless a workspace `host_pattern` policy already decides it,
the person gets a `network_egress` request naming the address kind and can allow it
once, allow it for the workspace, or decline. An allow is held too: "once" covers that
host and port for the rest of the turn (a scheduler client opens one connection per
command), and an allow for the session covers it until the session ends; another port
asks again. An allow also covers the child sessions the session starts (a delegate, or
the agent that answers a plugin's question). A decline holds for the rest of the turn
it was given in: a client that retries the same host fails at once without asking
again, and the next turn asks afresh (a workspace policy the person adds still
decides first). The same holds for any `network_egress` request. The connection is recorded as
`proxy-enforced` with its `network` kind. CLIO resolves the name once and dials the
addresses it classified, and refuses any relayed connection that would land on a port
CLIO itself serves. Other programs (`curl`, Python clients) keep Codex's rule:
loopback, private and non-public names are refused, and the CONNECT helper says so
when it happens. Fenced MCP servers get the same `ssh` shims as the shell, so a
server that runs `ssh` (a remote-site adapter, say) takes the same route and asks the
same question.

Raw TCP clients that ignore proxies and are not `ssh` (Slurm's `sbatch`, `squeue` and
`scontrol` dialing `slurmctld`) reach only the endpoints a site declares in
`sandbox.forward_tcp` (`CLIO_SANDBOX_FORWARD_TCP`, comma-separated `host:port`, for
example `ares:6817,ares:6819`). Every Linux-fenced child (shell, MCP server, kernel)
then starts behind the CONNECT helper's forwarder (`runtime/fence_forward.py`), which
listens inside the sandbox and carries each connection through the same relay, so it
is recorded and asks the same `network_egress` question. A host that resolves to a
loopback address on this machine (a login node that is also the controller) is
forwarded at that address. Any other host gets its own `127.0.0.N` address (N from 2),
and a host given by name is renamed for the child: the forwarder runs the program in a
user and mount namespace of its own (same uid and gid, no added privilege, the fence's
read-only mounts stay read-only) with a private `/etc/hosts` in which the name points at
that address, so `sbatch` resolving `ares` reaches the forwarder unchanged. Every such
endpoint is also listed in the child's `CLIO_FORWARDED_TCP` (`host:port=address:port`),
for a client given an IP address or a kernel that refuses the namespace. Nothing is
forwarded without a declaration. A client with a
short connect timeout (Slurm's `MessageTimeout`) may give up while the person is
deciding; its retry goes through on the held answer.

The Linux fence allows Unix-domain sockets, because `ssh -o ControlMaster`, munge and
other local IPC need them and they carry no route off the machine (abstract sockets
belong to the child's own network namespace). A child can bind a socket only where it
can write (its workspace, data directories and temp). Connecting is not confined by
path, so CLIO denies the known host control sockets inside the fence: the session's
runtime directory (`$XDG_RUNTIME_DIR`, which holds the user bus and agent sockets),
D-Bus, systemd and nscd (a resolver path that would bypass the proxy), Docker,
containerd, Podman, libvirt, snapd and LXD, and X11, tmux and screen sockets. A socket a
host service opens elsewhere stays reachable, so treat Unix sockets as local IPC the
child can use, not as part of the network barrier.

A socket path holds at most 107 bytes. When a fenced shell's, kernel's or plugin
server's `TMPDIR`, `TEMP` or `TMP` is deeper than 64 bytes (a session scratch folder or
the fence's per-workspace cache under a long `CLIO_AGENT_HOME`), CLIO points it at a
short alias instead: a symlink `/tmp/clio-<uid>-<hash>` owned by the user, which leads
to the same folder. Files and sockets still land in that folder, and deleting or
sweeping a session removes its scratch alias. A name already there that is not the
user's own link is never used.

Windows' MXC AppContainer replaces a fenced child's `LOCALAPPDATA`, `TEMP` and `TMP`
with a per-launch `Packages\sandbox.{GUID}\AC` profile that the fence does not grant and
that is discarded on the next launch (uv failed there with *Access is denied*). Setting
them before the container starts breaks MXC's own bootstrap, so the child runs under a
small in-fence shim (`runtime/fence_container_env.py`) that sets them inside: to the
child's cache folder under `CLIO_AGENT_HOME` (`cache/sandbox/<hash>`, `Temp` beneath it),
which is granted and the same on every launch. A fenced shell keeps its session scratch
`TEMP`. `APPDATA` stays the real roaming folder, which holds uv's tools, Pythons and
credentials and other tools' settings: a fenced child reads it but does not write it, as
`~/.config` elsewhere. Python's `platformdirs` asks Windows' Known Folder API rather than
these variables, so inside MXC it still names the per-launch profile; tools built on it
need their own override (`FASTMCP_HOME`, `CLIO_KIT_CACHE_DIR`).

A fenced MCP server's loopback is its own network namespace's, so a port an MCP App
declares for its live service (MA23) is not on the host's `127.0.0.1`. CLIO reaches it
from inside instead: each Linux-fenced child carries `CLIO_FENCE_CHILD` (its egress
channel), and the probe and both service proxies find the one namespace among CLIO's
own descendants that carries the serving child's mark and listens on the declared port.
A short helper joins that namespace (through the user namespace that owns it, which
this user created, so no privilege is added), connects to `127.0.0.1:<port>` and hands
the connected socket back (`runtime/fence_service.py`,
`gact/mcp_app_service_dial.py`). Nothing listens for it on the host, and no other port
in the namespace is dialed, so the fence gains no inbound path. When two namespaces
could be the server's, CLIO refuses to guess and the view reports `service_unreachable`.

On Windows the Codex fence runs a fenced MCP server in an MXC AppContainer. The
container shares the host's port space, but the desktop (a browser, the ParaView
client, CLIO itself) cannot connect to a port the server listens on inside it: the
loopback SYN is dropped. CLIO adds no firewall rule or loopback exemption. Instead each
fenced stdio server runs under a small in-fence agent (`runtime/fence_loopback_agent.py`,
marked by `CLIO_FENCE_LOOPBACK`), and a host publisher in CLIO
(`runtime/fence_loopback.py`) carries traffic for that one container:

- When the server writes a `127.0.0.1`/`localhost` address or URL to its stdout, the agent
  asks the publisher to publish that port. The publisher answers only a CLIO descendant
  holding that spawn's token, and only for a port that a process in that agent's own tree
  is listening on, on loopback.
- It opens a host listener on `127.0.0.1:<new port>`, plus `[::1]:<same port>` when the
  server bound only IPv4. Each accepted connection is spliced to a connection that the
  agent dials from inside the container (outbound to host loopback is allowed).
- The agent rewrites the address in the server's output:
  - a URL becomes `http://localhost:<same port>/…`, so pages that check `Origin` against
    their own port, such as relay's ask page, keep working;
  - a bare address, such as a serve-route forward port for an IPv4-only client, becomes
    `127.0.0.1:<new port>`.
- On the way in, published addresses are mapped back to the server's own port.
- The published listeners close when the server exits. A port no process in that tree
  listens on is never published, so the fence gains no general inbound path.
- The MCP App service probe and proxies use the same publisher (`fence_service.py`).

`inbound_loopback: "published"` in the fence details reports this.

Known limits:

- Only addresses in the server's text output are translated. A port sent as a separate
  JSON number is not.
- A sign-in that sends an identity provider straight to the server's own loopback
  callback is not translated.
- A desktop CLI that reads the server's state files, such as `clio-relay asks answer`,
  sees the unpublished port.
- If the server itself holds `[::1]` on its port, URLs use the new port, and a page that
  checks `Origin` may refuse its form posts.

On macOS the Codex write fence does not restrict network routes. Tools that honor
`HTTP(S)_PROXY`/`ALL_PROXY` (curl, Python, pip/uv, git over HTTPS) and the shell's
`ssh`/`scp`/`sftp` shims go through CLIO's proxy and are recorded. A client that
ignores those variables (an `ssh` called by absolute path, raw sockets, some Node
`fetch` clients) connects directly: that connection works but is not recorded.

**Deny-by-default** is an opt-in per-workspace mode: egress is refused unless the
domain is granted, and the first connection to a new domain raises a
`network_egress` permission request (grant-on-first-domain). The `host_pattern`
vocabulary scopes a domain grant. Deny mode is decided at CLIO's proxy, so on macOS
(and other `env-cooperative` tiers) it cannot stop a client that ignores the proxy
variables; treat it as a policy for cooperating tools, not a network barrier there.
On Linux, when the fence probe proves isolation, deny mode is a real barrier.

### Local listeners

The CLIO server listens on `127.0.0.1` at the `gact.port` (17800 by default) unless
it is started with `--host`. The clio-core runtime it starts binds its RPC port
(`9413` by default) and data port to loopback: CLIO sets `CLIO_BIND_ADDR=127.0.0.1`
in the clio-core child environments only (`arc/clio_core_config.py`
`pin_loopback_bind`), and keeps an explicit value set by a hand-authored multi-node
setup. The server's own environment is not changed.

clio-core's runtime web dashboard (`viz`, fixed port 8080 by default) is off in
CLIO: the same helper sets `CLIO_VIZ_ENABLE=0`, which wins over any `cte.yaml`.
CLIO does not use the dashboard, and with a fixed port, two homes or users on one
computer (and a common dev server) would share 8080. An explicit
`CLIO_VIZ_ENABLE=1` in the server's environment turns it back on for debugging.

**Known gap:** clio-core's control channel, on the RPC port + 2 (`9415` by default),
binds `tcp://*` (all interfaces) and ignores `CLIO_BIND_ADDR`
(`ControlChannelLoop` in iowarp/clio-core `context-runtime/src/ipc_manager.cc`).
It needs an upstream clio-core fix to use the same bind address; until then, a host
firewall is the only control for that port.

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
`policy_violation` carries the boundary diagnosis' message as its `next_action`, so
the model may *ask* and the user decides.

**CLIO's own folders are outside the shell's territory.** The shell writes the
workspace, granted roots, its scratch and temp, and the workspace's state and cache
folders, where its tool caches are redirected. CLIO's cache and config folders (the
servers' environments, settings, hook trust, credentials, the plugin store) are the
servers' only, so a shell command cannot break a running server, widen its own limits or
replace a credential (`runtime/sandbox_roots.py`; KiCad ledger FN-3).

**Server launchers' homes exist before the fence starts.** A fence rules only on
folders that exist, and the launchers that start MCP servers create theirs on first
use (`uvx` locks `<uv data>/tools/.lock`, `npx` fills the npm cache). CLIO creates uv's
data, cache and tool folders (`UV_TOOL_DIR` when set), the npm cache and clio-kit's
folders before granting them, so a fresh machine or a fresh `XDG_DATA_HOME` starts a
plugin server on its first try.

**Boundary feedback in the result (#1905).** A denial is diagnosed once, in the
confinement layer (`runtime/sandbox_diagnosis.py`): a per-platform denial signature
in stderr (PowerShell "Access to the path … is denied", `UnauthorizedAccessException`,
`WinError 5`, `0x80070005`, errno 13/30/1, "Read-only file system", "Operation not
permitted"; numeric codes match localized text), the denied paths from the denial
lines or the command, and the territory check against the write roots that seam gave
its fence. The shell and `run_code` attach it to their result as a typed `boundary`
field (`kind`, `confidence`, `mechanism`, `paths`, `request_roots`, `message`,
`signature`) only when there is one, and the model reads its `message`: the folder
(nearest existing parent) and the retry with `access_paths` + `access_reason`.
`confidence` is `confirmed` with paths outside the roots and `possible` with none
found; a path inside the roots, or one the server's own account cannot write either,
gets nothing, as does the floor (mechanism `none`). Exit 740 or "requires elevation"
is `elevation_required`: no grant applies, and the agent tells the person the exact
file to run. A confined stdio MCP server's failed call is diagnosed by the observer
against the recomputed fleet roots at `possible` confidence, and its message is noted
on the error (or appended to the returned error result). A narrowed territory (plan
mode, a read-only task, a workflow step) names where the call may write instead of a
retry. A confirmed `write_denied` with folders to ask for also raises the folder-access
card (`gact/sandbox_folder_offer.py`, `origin: sandbox_boundary`): a pending permission
that parks no call, so the session needs the person; allowing grants the folders, and a
grant made another way (`POST /v1/workspaces/{wid}/grants`) settles the card (owner's
review, CT41). No automatic rerun; `artifact.policy_violation` reads the same diagnosis.

**Outside the sandbox, one command at a time.** Some blocks no folder grant lifts: the
fenced child runs without the server account's privileges, so a server running as root
still cannot replace files another account owns inside a granted folder (a package
manager's download folders). A denial inside the territory on a path a root server may
write is diagnosed as `privilege_denied` (`possible`); its message tells the agent that more
folders will not help and how to ask. The agent then calls `shell_bash` with
`outside_sandbox: true` and an `access_reason`. The gate always asks the person for that
call, whatever the approval mode, the person's rules or the AI reviewer would do (a deny
from a rule or the mode still wins); the card's title says the command runs outside CLIO's
sandbox, shows the exact command and the reason, and offers only **allow** (this command,
once) or **deny**, never a standing rule. An allow is recorded for exactly that command
(`runtime/sandbox_outside.py`) and the shell takes it once, within ten minutes, then runs
the command unfenced (mechanism `none`, reason `outside_sandbox_allowed`, logged). The
shell refuses `outside_sandbox` without a recorded allow (`outside_sandbox_not_approved`), so
a path that skipped the gate fails closed. CLIO never takes a password: a command that needs
one (`sudo` on a desktop) is still the person's to run, as `elevation_required` says.

**A connector's server outside the sandbox, once per connector.** Some connector servers
start a desktop app or a user-scope installer, and those keep the person's own settings
where the fence blocks them: the registry and Documents on Windows, `~/Library` on macOS,
`~/.config` and the display on Linux. Such a connector's catalog entry says so in
`outside_sandbox`. The first time an agent calls one of its tools under an active fence,
before the call reaches the server, the gate asks the person whether that server may run
outside CLIO's sandbox (`gact/connector_outside.py`). As with a command outside the
sandbox, only the person answers, whatever the approval mode, rules or AI reviewer; the
card states what the server starts and what running outside the sandbox means, and offers
only **allow** or **deny**. The row's `consent` (`scope: connector_outside_sandbox`,
`lasting: true`) tells the card the allow is kept, so it reads **Allow**, not "Allow
once". The answer is kept for that connector and its server
declaration (`connectors/outside_sandbox.py`, in `connectors.yaml`), so a new command or
pinned release is asked again. An allow starts the connector's server again, unconfined,
in every resident fleet (mechanism `none`, reason `outside_sandbox_allowed_for_connector`,
logged), so the call that asked already reaches it; a deny keeps it fenced. The call
itself then goes through the gate's other layers as usual. Nothing is asked in an
unattended run, when a rule or the mode denies the call, or when the question ends with
the turn. The person takes it back on the connector (`DELETE
/v1/connectors/{id}/outside-sandbox`), and the server runs fenced again. The same code
runs on every platform; only the fence it leaves differs (MXC AppContainer on Windows,
Seatbelt on macOS, Landlock or bubblewrap on Linux), and with no active fence nothing is
asked.

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
