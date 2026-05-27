# Command And Capability Truth Audit

## Purpose

CLIO and GACT should not show commands or menu actions that fail at runtime
unless they are explicitly marked unsupported, disabled, or provisional. The
TUI should only advertise actions that match the backend's advertised
capabilities and implemented endpoints.

This audit covers:

- Slash commands exposed by the TUI palette.
- Backend commands exposed by `GET /v1/commands`.
- CLI commands that call CLIO/GACT endpoints.
- Capability flags relevant to command visibility.
- Permission, rewind, undo, compact, and voice semantics.

This document is planning/audit only. It does not implement changes.

## Three-Layer Reconciliation Model

The command/capability audit should be treated as a three-layer
reconciliation, not just a search for broken slash commands.

### 1. CLIO Has The Capability, But GACT/TUI Does Not Surface It

These are real CLIO features that users cannot discover or control well through
GACT/TUI.

Current examples:

- Permission policy rules exist in CLIO through `/v1/policies`, but the TUI
  does not expose a full policy-rule editor. The CLI has `gact perms rules`,
  but the interactive TUI mostly surfaces pending permission decisions.
- Permission audit rows and direct destructive-operation policy checks exist,
  but the user-facing workflow could make that more visible from Doctor,
  Settings, or a dedicated permissions view.
- CLIO-specific backend command metadata such as `status=unavailable` and
  `error=not_implemented` exists for `/optimize`, but the GACT Go command
  model does not currently preserve those fields for the TUI.

Outcome choices:

- Surface the capability in GACT/TUI.
- Add contract/client fields so the TUI can represent it honestly.
- Explicitly document it as backend-only if it should remain advanced/CLI-only.

### 2. CLIO Lacks The Capability, But GACT/TUI Should Surface It

These are GACT contract capabilities that should probably exist in CLIO because
the TUI, CLI, emulator, or contract already define the behavior.

Current examples:

- `POST /v1/sessions/{sid}/undo`.
- `POST /v1/sessions/{sid}/rewind`.

Permissions are probably not in this category anymore: CLIO already has the
core permission system. The remaining permission question is surfacing and
editing, which belongs in layer 1.

Outcome choices:

- Implement the missing CLIO endpoint/capability.
- Mark it unsupported in CLIO capabilities and make GACT/TUI hide or disable
  the affordance.
- Split the backend work into a dedicated issue when semantics are non-trivial,
  as with undo/rewind.

### 3. CLIO Lacks, Defers, Or Incorrectly Plugs The Capability, But TUI Shows It As Runnable

These are premature or broken affordances. Some are useful because they reserve
framework space for future CLIO support, but they must not create runtime
failures or imply that deferred features work today. This layer also includes
commands that CLIO technically has, but the TUI integration is wired
incorrectly enough that invoking them crashes or fails.

Current examples:

- `/cache-stats`: CLIO implements the command, but user testing reports that
  invoking it crashes the TUI. This is not a missing backend command; it is a
  wrongly plugged command/result/rendering path.
- Voice flows: CLIO advertises `voice=false`, but generic GACT/TUI/CLI surfaces
  still expose voice controls and `gact voice`. This is acceptable as framework
  scaffolding, but it should be disabled or marked not-supported-yet for CLIO.
- `/optimize`: CLIO lists it as unavailable/not implemented, but the TUI likely
  renders it as an executable command because command status metadata is not in
  the Go model.
- `/undo` and `/rewind`: if CLIO does not implement them yet, docs/help/CLI
  should not imply that they work against CLIO without capability-aware
  messaging.
- Any stale docs/help commands such as `/scenarios` should be removed or marked
  backend-specific.

Outcome choices:

- Keep idea/future commands visible when they are useful product direction, but
  mark them as TODO, coming-soon, disabled, or not-supported-yet.
- Hide only accidental noise, stale typos, or commands that should not be part
  of the product direction.
- Disable it with an explicit reason.
- Fail fast with a capability-aware message before attempting the backend call.
- Fix the TUI/CLIO integration when the backend capability exists but the
  surfaced action crashes or misrenders.
- Implement the missing CLIO behavior if the command is intended to be real.

## Layer Audit

### Layer 1: CLIO Has It, GACT/TUI Does Not Surface It Enough

| Area | CLIO truth | GACT/TUI truth | Needed planning decision |
|---|---|---|---|
| Permission policy rules | `/v1/policies` exists; policies support allow/deny/ask-style rule management. | CLI has `gact perms rules`; TUI mostly surfaces pending permission decisions, not durable policy editing. | Decide whether to add a TUI policy editor, surface it in Doctor/Settings, or document it as CLI/advanced-only. |
| Permission audit/direct destructive checks | CLIO audits destructive GACT operations and applies policy checks. | TUI does not make the audit trail/policy consequence very discoverable. | Decide if Doctor/Settings should show audit rows and direct-operation policy state. |
| Command availability metadata | CLIO can return command metadata such as `status=unavailable` and `error=not_implemented`. | Go `gact.Command` does not preserve those fields, so the palette cannot render disabled rows. | Extend the GACT contract/client model or remove unavailable commands from `/v1/commands`. |
| CLIO command result messages | CLIO materializes backend command output as synthetic assistant messages. | TUI appears to rely on SSE/message reload, but `/cache-stats` reportedly crashes. | Treat command-result rendering as a contract surface and add live success-path tests. |
| TUI local command handlers | The TUI contains handlers for local commands such as `/new`, `/duplicate`, `/sessions`, `/rename`, `/cancel`, `/agents`, `/theme-next`, and `/theme-prev`. | Several are advertised in help, but they are not included in the always-injected local palette command list for CLIO. | Decide whether each should be added to the palette, removed from help, or converted to a backend command. |

### Layer 2: CLIO Does Not Have It, But GACT/TUI Should Surface It

| Area | GACT/TUI truth | CLIO truth | Needed planning decision |
|---|---|---|---|
| `undo` | GACT contract, emulator, and CLI define `POST /v1/sessions/{sid}/undo`. | CLIO route not found. | Implement in CLIO or explicitly mark unsupported until memory semantics are decided. Tracked in #328. |
| `rewind` | GACT contract, emulator, and CLI define `POST /v1/sessions/{sid}/rewind`. | CLIO route not found. | Implement in CLIO or explicitly mark unsupported until memory semantics are decided. Tracked in #328. |

Permissions are not currently a layer-2 item. The backend exists; the remaining
question is how much of it should be surfaced in the TUI.

### Layer 3: TUI Shows It, But It Is Missing, Deferred, Or Wrongly Plugged

| Surface | Current behavior | Classification | Needed planning decision |
|---|---|---|---|
| `/cache-stats` | CLIO implements it and emits a synthetic assistant message, but user testing reports that invoking it crashes the TUI. | Wrongly plugged command success path. | Reproduce against live CLIO; test command dispatch, SSE event handling, message decoding, and synthetic command-result rendering. |
| `/optimize` | CLIO advertises it in `/v1/commands` with `status=unavailable`, `error=not_implemented`; TUI likely renders it as runnable. | Deferred backend feature shown as runnable. | Keep it visible as a TODO/coming-soon idea command, preserve command status metadata, render it disabled, and do not POST it until implemented. |
| `/help` | Help advertises `/help`, but the CLIO local palette injection does not add it and no TUI special-case handler was found in the command dispatch path. | Help-only/stale command. | Either implement `/help` as a palette command that opens help, or remove it from command help. |
| `/new`, `/duplicate`, `/sessions`, `/rename` | TUI dispatch code has local handlers, and help advertises most/all of them. The CLIO local palette list does not currently inject them. | Implemented local handlers not consistently surfaced. | Add them to the local palette if intended, or remove help references. |
| `/cancel` | TUI dispatch code has a local optimistic hint, and CLIO has a real `/v1/sessions/{sid}/cancel` endpoint, but `/cancel` is not injected into the CLIO local palette and CLIO does not expose it through `/v1/commands`. | Existing backend endpoint/local handler not consistently surfaced as a slash command. | Decide whether `/cancel` is a first-class slash command; if yes, wire it directly to the cancel endpoint rather than generic command dispatch. |
| `/agents` vs `/agents-list` | Help advertises `/agents`; TUI dispatch handles `/agent` and `/agents`; local palette injects `/agents-list`. | Naming drift across help, handler, and palette. | Pick one canonical command, keep aliases only if deliberately supported, and test all advertised aliases. |
| `/theme-next`, `/theme-prev` | TUI dispatch code handles them and help advertises them, but the CLIO local palette list does not inject them. | Help/handler/palette drift. | Add to palette or remove from help. |
| `gact voice` / voice key help | CLIO advertises `voice=false`; generic GACT/TUI still has voice scaffolding. | Reserved future capability shown too much like a runnable feature. | Keep framework, but mark disabled/not-supported-yet and fail fast capability-aware. |
| `gact undo` | CLI calls `/v1/sessions/{sid}/undo`; CLIO lacks the route. | Missing backend capability shown by CLI/docs. | Implement via #328 or make CLI/docs capability-aware for CLIO. |
| `gact rewind` | CLI calls `/v1/sessions/{sid}/rewind`; CLIO lacks the route. | Missing backend capability shown by CLI/docs. | Implement via #328 or make CLI/docs capability-aware for CLIO. |
| Stale docs/help commands such as `/scenarios` | Older docs may mention commands not present in CLIO or the current TUI palette. | Stale surfaced contract. | Remove, mark emulator-only, or route to the correct current surface. |

## Audit Methodology Required Before Implementation

The implementation task should start with a reproducible matrix, not one-off
fixes.

For every slash/menu/CLI action:

1. Identify where it is advertised:
   - TUI palette local command list.
   - TUI help modal.
   - Backend `/v1/commands`.
   - CLI subcommands.
   - Docs.
2. Identify the intended execution path:
   - Pure local TUI action.
   - Dedicated GACT endpoint.
   - Generic `POST /v1/sessions/{sid}/commands/{cmd}` backend command.
   - CLI-only endpoint.
   - Reserved future capability.
3. Test the actual action path against live CLIO:
   - Selecting from palette.
   - Typing the slash command manually.
   - Invoking the matching CLI command, if one exists.
   - Backend unavailable/error path.
   - Backend success path with SSE/message refresh.
4. Classify the result into one of the three layers:
   - CLIO has it but GACT/TUI does not surface it.
   - CLIO lacks it but GACT/TUI should surface it.
   - TUI shows it but it is missing, deferred, stale, or wrongly plugged.
5. Only then choose the remedy:
   - implement backend,
   - add TUI/local palette command,
   - route to a dedicated endpoint,
   - disable with reason,
   - mark as TODO/coming-soon/not-supported-yet,
   - hide only accidental noise or stale typos,
   - remove stale docs/help,
   - or keep visible as not-supported-yet framework scaffolding.

## Current Truth Summary

CLIO currently advertises most GACT capabilities as supported. The relevant
capability state is:

| Capability | CLIO state | Audit result |
|---|---:|---|
| `commands` | true | Mostly real, but command status/unavailable semantics are not honored by the TUI. |
| `permissions` | true | Real. Permission rows, blocking gates, policy rules, and direct-delete auditing exist. Needs UX/docs cleanup, not a new core backend feature. |
| `diffs` | true | Real for file diffs apply/reject. |
| `memory` | true | Real memory stats plus `/compact` endpoint. `/compact` is TUI-local and provisional in wording. |
| `voice` | false | Reserved future capability. Keep the framework, but make CLIO surfaces clearly disabled/not-supported-yet. |
| `session_branching` | true | Fork/branch exists, but rewind is a separate endpoint and is missing in CLIO. |

The strongest backend gap found is:

- GACT spec and gact-tui CLI support `POST /v1/sessions/{id}/undo` and
  `POST /v1/sessions/{id}/rewind`, and the emulator implements both.
- CLIO GACT does not appear to implement either route.

## Surfaces Audited

### TUI Palette Local Commands

The TUI always injects these local commands into the palette unless already
provided by the backend:

| Command | TUI behavior | Backend dependency | Audit result |
|---|---|---|---|
| `/metrics` | Opens metrics modal. | `GET /v1/metrics`. | OK. |
| `/memory` | Opens memory inspector; checks `capabilities.memory`. | `GET /v1/memory/stats`. | OK. |
| `/theme` | Opens local settings theme tab. | None. | OK. |
| `/theme-export` | Writes local `~/.config/gact/theme.json`. | None. | OK. |
| `/mcp` | Opens catalog browser. | `GET /v1/mcp/servers`. | OK when `mcp=true`; currently not gated locally. |
| `/tools` | Opens catalog browser. | `GET /v1/tools` / catalog. | OK. |
| `/catalog` | Alias for tools catalog. | Same as `/tools`. | OK. |
| `/skills` | Opens catalog browser. | Skill/agent catalog. | OK. |
| `/agents-list` | Opens catalog browser. | `GET /v1/agents`. | OK. |
| `/mode` | Cycles session routing mode. | `PATCH /v1/sessions/{sid}`. | OK. |
| `/clear` | Two-step confirmation, then backend command. | `/v1/commands` dispatch if backend also has `/clear`. | OK for CLIO. |
| `/copy` | Copies last assistant reply locally. | Clipboard only. | OK. |
| `/diff` | Opens workspace diff locally. | Session diffs. | OK. |
| `/compact` | Posts `/v1/sessions/{sid}/compact`. | CLIO endpoint exists. | Works, but wording says provisional. Should be made honest/stable or gated. |
| `/doctor` | Opens Doctor modal only when `integration_health=true`. | `GET /v1/health`. | OK. |

### Backend Slash Commands From CLIO

CLIO returns the following backend commands from `GET /v1/commands`:

| Command | CLIO behavior | TUI behavior | Audit result |
|---|---|---|---|
| `/clear` | Clears stored messages and emits a visible synthetic command result. | Has two-step local confirmation, then posts command. | OK. |
| `/cache-stats` | Appends ARC cache stats as a synthetic assistant message. | TUI posts to backend and relies on SSE/message reload. | Backend command exists, but user reports it crashes the TUI when used. Treat as a real TUI/CLIO integration bug requiring reproduction before implementation. |
| `/dump-trace` | Appends last thinking trace or a no-trace message. | TUI posts to backend. | OK. |
| `/optimize` | Listed with `status=unavailable`, `error=not_implemented`; dispatch returns 501. | TUI's Go `Command` type does not model `status`/`error`, so it likely renders and runs as normal. | Gap. TUI must render unavailable commands disabled or CLIO must hide it until real. |

Important spelling note:

- `/cache-stats` is the implemented CLIO command.
- `/cache-stash` does not appear in the CLIO or gact-tui codebase. If it is
  surfaced anywhere, it is a typo/stale command and will fail as an unknown
  backend command.

Important runtime note:

- `/cache-stats` was initially classified as OK because CLIO implements the
  backend command and materializes a synthetic assistant message.
- User testing reports that invoking `/cache-stats` crashes the TUI. That makes
  it a first-class command finalization bug even though the backend route
  exists.
- The fix should reproduce the crash against live CLIO, then decide whether the
  fault is in command dispatch, SSE/message reload, message decoding, synthetic
  command-result rendering, or ARC cache-stat payload handling.
- Existing gact-tui tests cover backend error decoding for `/cache-stats`, but
  this reported crash sounds like the success path or live CLIO event path needs
  coverage.

### TUI Help / Docs Slash Commands

Some docs/help mention commands not present in the CLIO command catalog or TUI
local palette:

| Command | Where advertised | Reality | Audit result |
|---|---|---|---|
| `/undo` | gact-tui docs/help and emulator command list. | Emulator has `/undo` endpoint; CLIO lacks `/v1/sessions/{sid}/undo`; TUI palette does not inject `/undo` locally unless backend provides it. | Docs stale for CLIO; backend gap if CLIO wants parity. |
| `/help` | gact-tui docs/help and emulator command list. | CLIO does not expose backend `/help`; TUI has `?` overlay. | Probably OK if docs say `?`; backend `/help` optional. |
| `/scenarios` | older docs. | Not present in current TUI local command list for CLIO. | Stale docs if still visible anywhere. |
| `/undo` via CLI | `gact undo`. | TUI CLI calls `/v1/sessions/{sid}/undo`; CLIO route missing. | Real CLIO gap. |
| `gact rewind` | CLI and docs. | TUI CLI calls `/v1/sessions/{sid}/rewind`; CLIO route missing. | Real CLIO gap. |
| `/cache-stash` | Possible typo/stale slash command. | Not found in CLIO or gact-tui; implemented command is `/cache-stats`. | Literal fail if surfaced or typed. Treat as stale/invalid command. |

## Permission Semantics

Initial concern: permissions might not exist on CLIO.

Audit finding: permissions are real.

CLIO has:

- `capabilities.permissions=true`.
- `GET /v1/permissions`.
- `POST /v1/permissions/{pid}` with `allow`, `deny`, `allow_session`,
  `allow_workspace`.
- Blocking permission gate for destructive MCP tool calls.
- Permission policies through `GET /v1/policies` and `PUT /v1/policies`.
- Direct destructive GACT DELETE auditing/policy checks.
- `/diffs/apply` permission audit rows.
- Plan/architect mode auto-deny for destructive operations.

Remaining permission work is UX/documentation:

- The TUI has permission banner keys `a`, `d`, `s`, `w`, but command surfaces
  for permission rules are CLI-only (`gact perms rules ...`).
- The TUI should expose or at least link to policy-rule management from Doctor
  or Settings if users are expected to manage policies interactively.
- Docs should clearly distinguish pending permission decisions from persistent
  policy rules.

This does not need to be a separate core backend issue unless the product goal
is a full TUI permission-policy editor.

## Undo And Rewind Semantics

This should become its own backend implementation issue or a blocking subtask.

gact-tui and the emulator define:

- `POST /v1/sessions/{id}/undo` with `{count?: int}` returning
  `{reverted_messages: string[]}`.
- `POST /v1/sessions/{id}/rewind` with `{to_message_id, include_target?}`
  returning `{deleted_messages: string[]}`.

CLIO GACT does not appear to implement these routes.

Desired semantics:

- `undo` removes the last N messages from the session ledger.
- `rewind` removes every message after a target message, optionally including
  the target.
- Both should update `message_count`, timestamps, session status, metrics where
  applicable, and emit SSE events so the TUI refreshes without polling.
- Both should use permission policy/audit semantics because they are
  destructive.
- Both should consider ARC/session memory implications. If ARC keeps a durable
  conversation copy, the operation must either update ARC consistently or mark
  the operation as GACT-transcript-only.

Open design point:

- Should rewind/undo delete from ARC memory, append tombstones, or only alter
  the GACT-visible transcript?

That memory question should link to the memory refinement issue.

## Voice Semantics

CLIO correctly advertises `voice=false`.

gact-tui still exposes:

- `Ctrl+Y` help text.
- `--voice-cmd` setting.
- `gact voice <sid> <audio>`.
- Client support for `/v1/sessions/{id}/voice/transcribe`.

This is acceptable as generic GACT functionality and as reserved CLIO framework
space. CLIO-specific UX should keep the path available for future support while
making it clear that voice does not work yet when `capabilities.voice=false`.

Needed cleanup:

- Keep or gray voice affordances in CLIO sessions when `voice=false`, but mark
  them as not supported yet rather than runnable.
- Doctor already shows `voice=false`; help/settings should be equally honest.
- `gact voice` should fail fast with a capability-aware message when connected
  to CLIO, rather than only surfacing backend 404/501.

Voice should not block command finalization because CLIO intentionally does not
support it yet, while the GACT/TUI framework can remain in place.

## Command Status And Availability

CLIO currently returns `/optimize` with:

```json
{
  "id": "/optimize",
  "status": "unavailable",
  "error": "not_implemented"
}
```

The Go GACT `Command` type does not include `status` or `error`, so the TUI
cannot distinguish unavailable commands from runnable commands.

The final design should choose one of:

1. Extend the GACT `Command` type with `status`, `error`, and possibly
   `disabled_reason`, then render unavailable commands disabled.
2. Keep unavailable commands out of `/v1/commands` until implemented.
3. Keep them visible only in Doctor/capability diagnostics, not the executable
   slash palette.

Recommended:

- Add optional command status fields to the contract/client.
- TUI displays unavailable commands as disabled rows with the reason.
- Pressing Enter on a disabled command should not POST.
- CLIO also exposes `/optimize` as `optimizer_command` in
  `/v1/capability-gaps` and `x_clio_capability_gaps`, so Doctor/help surfaces
  can discover the deferred command without treating it as runnable.

## Capability Gating Rules

The TUI should consistently apply capability gates:

- `voice=false`: keep the framework available, but render voice UI disabled or
  not-supported-yet; voice CLI should explain the unsupported capability.
- `commands=false`: hide backend commands but keep local-only commands that do
  not require backend command dispatch.
- `memory=false`: hide or mark `/memory` and `/compact` unsupported.
- `mcp=false`: hide or mark `/mcp` unsupported.
- `permissions=false`: hide permission action keys and permission-policy UI.
- `diffs=false`: hide `/diff`, apply/reject actions, and diff CLI affordances.
- `integration_health=false`: hide `/doctor` as already done.

Current TUI gating is partial. `/memory` and `/doctor` are gated. Voice, MCP,
tools, catalog, diff, and compact need a stricter capability truth pass.

## Recommended Issue Breakdown

### Main Issue: Command And Capability Truth

Use one main issue to align TUI/CLIO command visibility with real backend
support:

- Audit all command rows.
- Hide, disable, or implement each failing command.
- Add command status support.
- Fix CLIO docs/help drift.
- Add regression tests against CLIO capabilities.

### Child Issue: CLIO Undo/Rewind Endpoints

Create a child issue if we want to keep the main issue focused:

- Implement `/v1/sessions/{sid}/undo`.
- Implement `/v1/sessions/{sid}/rewind`.
- Define ARC/memory consistency semantics.
- Add TUI/CLI live CLIO tests.

### Possible Child Issue: TUI Permission Policy Editor

Only needed if interactive policy management is a product goal:

- Browse permission policies.
- Add/edit/remove allow/deny/ask rules.
- Explain rule matching.
- Show direct-delete audit rows.

## Acceptance Criteria

- Every command visible in the TUI palette is one of:
  - fully implemented,
  - local-only and working,
  - disabled with an explicit reason,
  - hidden because the backend capability is false.
- CLIO no longer exposes runnable `/optimize` semantics until it works, or the
  TUI renders it disabled using command status fields.
- `/cache-stats` does not crash the TUI on the live CLIO success path; it either
  renders the synthetic command-result message or shows a recoverable command
  error hint.
- `gact undo` and `gact rewind` either work against CLIO or are clearly marked
  unsupported by capability/endpoint truth.
- TUI help and docs stop advertising CLIO-unsupported `/undo` and voice flows as
  if they work today; voice may remain visible as a planned/not-supported-yet
  framework path.
- Permission behavior is documented as implemented, with remaining work scoped
  to UX/policy editing if desired.
- Regression tests compare CLIO capabilities, `/v1/commands`, and TUI palette
  command behavior.
