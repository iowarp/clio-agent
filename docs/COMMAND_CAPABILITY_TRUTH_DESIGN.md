# Command And Capability Truth Design

Tracking issue: https://github.com/iowarp/clio-agent/issues/327

## Purpose

Make every TUI, CLI, help, menu, and slash-command affordance truthful for
CLIO. A visible action must either work, be disabled with an explicit reason,
or be clearly marked as TODO/coming-soon.

This issue should run after:

1. permission surfacing is designed,
2. undo/rewind backend semantics are implemented or explicitly marked
   unsupported.

## Three-Layer Model

Every command or capability must be classified into one layer:

| Layer | Meaning | Example |
|---|---|---|
| 1 | CLIO has it, but GACT/TUI does not surface it enough. | Permission policies exist, but the TUI does not expose them clearly. |
| 2 | CLIO lacks it, but GACT/TUI should surface it. | Undo/rewind are in GACT and the emulator, but missing in CLIO. |
| 3 | TUI shows it, but it is missing, deferred, stale, or wrongly plugged. | `/cache-stats` crashes the TUI; `/optimize` is TODO but rendered runnable. |

Layer 3 includes broken integrations where CLIO technically has the backend
command but the TUI path crashes or misroutes.

## Command Matrix

Implementation must begin by creating a command matrix with these columns:

- command/action,
- advertised in TUI palette,
- advertised in TUI help,
- advertised by CLIO `/v1/commands`,
- advertised by CLI,
- advertised in docs,
- capability gate,
- execution route,
- expected final state,
- current failure,
- layer.

The matrix must include:

- TUI local palette commands,
- TUI help commands,
- backend commands from `/v1/commands`,
- CLI commands that call CLIO/GACT endpoints,
- docs-only commands from legacy CLIO REPL docs.

Legacy CLIO REPL slash commands must be separated from GACT/TUI commands. They
are not automatically bugs unless current GACT docs or help advertise them as
GACT commands.

## Known Cases

| Surface | Required outcome |
|---|---|
| `/cache-stats` | Must not crash the TUI. It should render the synthetic command-result message or show a recoverable command error hint. |
| `/dump-trace` | Must render as a backend command result without crashing or silently no-oping. |
| `/clear` | Must keep two-step confirmation and remain destructive only after confirmation. |
| `/optimize` | Keep visible as a TODO/coming-soon idea command. It must preserve backend status metadata, render disabled, and never POST until implemented. |
| `/help` | If advertised, it must open the TUI help modal. Otherwise remove from GACT help. |
| `/new`, `/duplicate`, `/sessions`, `/rename` | If help advertises them and handlers exist, add them to the palette and test them. |
| `/cancel` | If first-class, route to `POST /v1/sessions/{sid}/cancel`, not generic command dispatch. |
| `/agents` / `/agents-list` | Pick canonical names and make help, palette, and handlers agree. Aliases are acceptable only if tested. |
| `/theme-next`, `/theme-prev` | Help, palette, and handlers must agree. |
| Voice | Keep framework scaffolding, but show disabled/not-supported-yet when `voice=false`. |
| Undo/rewind | Do not claim they work against CLIO until the undo/rewind issue lands. |

## Command Status Metadata

Extend the GACT command model to preserve optional backend status fields:

- `status`: recommended values `available`, `todo`, `unsupported`,
  `unavailable`.
- `error`: machine-readable reason such as `not_implemented`.
- `disabled_reason`: user-facing text.

Backends may keep idea commands visible by returning `status: "todo"`.

TUI behavior:

- available commands are selectable and runnable,
- todo/unsupported/unavailable commands are visible but disabled,
- pressing Enter on a disabled command does not call the backend,
- disabled rows show a concise reason.

The goal is not to hide product-direction ideas. Hide only accidental noise,
stale typos, or commands that are not intended as part of the product.

## Capability Gating

Capability gates must be applied consistently:

- `commands=false`: hide backend commands; keep local-only commands that do not
  require backend command dispatch.
- `permissions=false`: hide permission action keys and permission surfaces.
- `memory=false`: disable `/memory` and `/compact`.
- `mcp=false`: disable `/mcp` and MCP install/remove flows.
- `diffs=false`: disable `/diff`, apply, and reject actions.
- `voice=false`: keep voice scaffolding visible only as not-supported-yet.
- `integration_health=false`: hide or disable `/doctor`.

## Testing And Verification

Add tests for:

- palette/help/backend/CLI drift,
- disabled TODO command rendering and non-dispatch,
- `/cache-stats` live-success path,
- backend command error handling as transient hint,
- `/cancel` routing to the cancel endpoint,
- command aliases such as `/agents` and `/agents-list`,
- capability-disabled states.

Manual verification should include selecting commands from the palette, typing
slash commands manually, and invoking matching CLI commands against live CLIO.

## Acceptance Criteria

- No visible command can crash the TUI.
- No visible command silently no-ops.
- No command POSTs to the wrong endpoint.
- TODO commands remain visible but disabled.
- Help, palette, backend catalog, CLI, and docs no longer contradict each
  other for CLIO.
- The command truth issue can point to permission surfacing and undo/rewind as
  resolved prerequisites rather than re-litigating those designs.
