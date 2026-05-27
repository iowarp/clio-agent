# Permission Surfacing Design

Tracking issue: TBD

## Purpose

CLIO already implements the core GACT permission backend. This design is about
surfacing that capability clearly in GACT/TUI so users can understand, inspect,
and manage permission state without confusing pending prompts, durable policy
rules, and audit history.

This is a prerequisite for the command/capability truth pass. That later pass
should not treat permissions as missing; it should consume the permission UX
decisions made here.

## Current State

Backend behavior is real and should remain the source of truth:

- `capabilities.permissions=true`.
- `GET /v1/permissions` lists pending and resolved permission rows.
- `POST /v1/permissions/{pid}` resolves rows with `allow`, `deny`,
  `allow_session`, or `allow_workspace`.
- `GET /v1/policies` and `PUT /v1/policies` expose durable policy rules.
- Destructive MCP/tool calls, diff applies, and direct destructive GACT
  endpoints create audit rows and enforce policy.
- Plan/architect mode auto-denies destructive operations.

Current TUI behavior is narrower:

- Pending permission prompts are shown as a banner.
- Keys `a`, `d`, `s`, and `w` resolve the oldest pending prompt.
- Durable policies are mainly exposed through CLI commands such as
  `gact perms rules`.
- Audit history is not easy to browse from the TUI.

## Design Goals

1. Make permissions discoverable without implying the backend is incomplete.
2. Separate three concepts in the UI:
   - pending decisions,
   - durable policy rules,
   - historical audit rows.
3. Let users inspect why a destructive action was allowed, denied, auto-denied,
   or auto-approved.
4. Keep destructive actions auditable even when they are user-initiated.
5. Keep this independent from undo/rewind and command truth, while allowing
   those features to reuse the same permission/audit model.

## Proposed UX

Add a permissions surface to TUI Settings or Doctor with three views:

| View | Purpose |
|---|---|
| Pending | Current permission requests that need a decision. Mirrors the existing banner but makes multiple pending rows browseable. |
| Policies | Durable allow/deny/ask rules from `/v1/policies`. Initially read-only is acceptable; editing can be added if the issue chooses full editor scope. |
| Audit | Recent resolved rows from `/v1/permissions?status=all`, including auto-approved and auto-denied rows. |

The pending banner remains the fast path. The new view is for inspection and
management, not a replacement for the banner.

Recommended slash/menu affordance:

- Add `/permissions` or `/perms` as a TUI-local command.
- It opens the permission surface.
- It is shown only when `capabilities.permissions=true`.
- If a backend lacks permissions, the command should be hidden or disabled with
  an explicit unsupported message.

Recommended CLI alignment:

- Keep `gact perms list`.
- Keep `gact perms rules list|set|clear`.
- Make CLI help use the same terms as the TUI: pending, policies, audit.

## Policy Editing Scope

Default implementation scope should be:

1. Browse pending permissions.
2. Browse durable policies.
3. Browse audit history.
4. Resolve pending permissions from the TUI.

Full in-TUI policy editing can be added after the read/inspect surface lands.
If implemented in this issue, it must preserve the backend's atomic
`PUT /v1/policies` behavior: invalid rows reject the whole update and leave the
previous policy set unchanged.

## Data And Contract Notes

No new backend endpoints are required for the initial surfacing pass.

The TUI should use:

- `GET /v1/permissions?session_id=<sid>&status=pending` for pending rows.
- `GET /v1/permissions?session_id=<sid>&status=all` for audit rows.
- Permission list responses include `metadata` with effective `session_id`,
  `status`, `limit`, `total`, `returned`, and `truncated` so TUI audit views can
  label filters and pagination honestly.
- `GET /v1/policies` for durable rules.
- `PUT /v1/policies` only if policy editing is in scope.

Permission action labels must align everywhere:

- `allow`
- `deny`
- `allow_session`
- `allow_workspace`

Legacy text such as `always_allow` should be removed or mapped only in
backward-compatible parsing, not shown as the canonical action.

## Interaction With Other Issues

- Undo/rewind should use this permission/audit model because transcript
  rollback is destructive.
- Command/capability truth should treat permissions as implemented and focus on
  whether `/permissions` or equivalent TUI surfacing is visible and gated.
- Voice and TODO commands do not need permission policy handling until they can
  perform destructive actions.

## Acceptance Criteria

- Users can discover from the TUI that CLIO permissions are active.
- Pending permission rows can still be resolved quickly with keyboard actions.
- The TUI has a clear place to inspect durable policies and recent audit rows.
- CLI, docs, and TUI use the same action names.
- Existing permission backend behavior and tests remain intact.
- Command/capability truth can classify permissions as "backend implemented,
  TUI surfaced" after this issue lands.
