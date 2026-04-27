# Permission system

CLIO ships with an interactive permission gate for destructive tools. This
doc describes what's wired, how to use it from the TUI, and how the
auto-resolve paths work.

## Backend surface

| Endpoint | Verb | Purpose |
|---|---|---|
| `/v1/permissions` | GET | List pending + recent permission rows |
| `/v1/permissions/{pid}` | POST | Resolve one (`{"action": "allow"}` or `"deny"` or `"always_allow"`) |

Each row carries `id`, `session_id`, `tool_call.{tool_name, input}`,
`summary`, `created_at`, `status`, `action`, optional `resolved_at` +
`reason`.

## What triggers the gate

`_make_permission_gate` (in `gact/app.py`) fires on any tool call where
`_is_destructive(name)` is true — currently `fs_apply_edit_write` plus any
tool name containing `delete`, `remove`, `drop`, `rm`, etc.

Three resolution paths:

1. **Auto-deny** — when `session.mode` is `plan` or `architect`, the gate
   refuses any destructive call without prompting (read-only contract).
   Permission row gets `status: auto_denied`, `action: deny`,
   `reason: session.mode=plan`.

2. **Auto-approve** — when the destructive call originates from an
   *explicit user gesture* (e.g. user clicked `a` to apply a proposed
   diff via `/v1/sessions/{sid}/diffs/apply`), the gate fast-allows
   because the user's intent is unambiguous. Row: `status: auto_approved`,
   `action: allow`, `reason: user clicked /diffs/apply`.

3. **Interactive** — for everything else (e.g. an LM-driven ReAct loop
   that decides to call `fs_apply_edit_write` mid-turn), the gate
   publishes a `permission.requested` event and blocks on a
   `threading.Event` for up to 120 s. The TUI sees the event and
   renders a banner; the user resolves with a/d/s/w; the backend
   POSTs the resolution back; the gate returns `allow` or `deny`.

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

## Known gap

The `_apply_edit_to_disk` write doesn't always actually flush to disk
when the gate auto-approves via the user-click path. Tracked separately
(see TODO).
