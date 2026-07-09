# Undo And Rewind Design

Tracking issue: https://github.com/iowarp/clio-agent/issues/328

## Purpose

Bring CLIO into parity with the GACT contract, gact-tui CLI, and emulator for
session transcript rollback.

This is a prerequisite for the command/capability truth pass. Until these
endpoints exist, GACT/TUI must not imply that undo/rewind works against CLIO.

## Current State

GACT already defines the surface:

- `POST /v1/sessions/{sid}/undo` with `{count?: int}` returning
  `{reverted_messages: string[]}`.
- `POST /v1/sessions/{sid}/rewind` with
  `{to_message_id: string, include_target?: bool}` returning
  `{deleted_messages: string[]}`.

gact-tui already has client and CLI code for both operations, and the emulator
implements both routes.

CLIO currently has:

- session CRUD,
- message list/get/delete,
- session fork,
- diff apply/reject,
- permission/audit policy,
- ARC-backed memory.

CLIO does not currently implement the undo/rewind routes.

## Semantics

`undo` removes the last N messages from a session transcript.

- Default count is 1.
- Count must be greater than zero.
- If count exceeds the number of messages, delete all messages.
- Response returns the deleted message IDs in transcript order.

`rewind` removes messages after a target message.

- `to_message_id` must exist in the target session.
- With `include_target=false`, delete messages newer than the target.
- With `include_target=true`, delete the target and every newer message.
- Response returns deleted message IDs in transcript order.

Both operations are destructive transcript mutations and must:

- update session `message_count`,
- update session `updated_at`,
- leave the session in a non-running state unless a stricter cancellation rule
  is added,
- publish SSE events so the TUI refreshes without manual reload,
- create permission/audit rows or direct destructive audit entries.

## Running Session Behavior

If a session is currently running, rollback must not race with generation.

Default rule:

- Reject undo/rewind on running sessions with a structured recoverable error
  telling the user to cancel first.

This avoids ambiguous behavior where generated messages arrive after rollback.
Future work can combine cancel+rollback, but this issue should keep the first
implementation deterministic.

## ARC And Memory Behavior

Rollback must be explicit about memory consistency.

Default rule:

- The GACT visible transcript is authoritative for rollback.
- ARC durable memory is not physically deleted in the first implementation.
- Deleted messages should be represented as tombstoned or ignored by future
  GACT transcript reconstruction.
- The operation should record enough metadata to prevent deleted transcript
  messages from reappearing after reload/import within the same backend process.

If ARC exposes a safe delete/tombstone primitive later, the implementation can
upgrade to durable memory tombstones. It should not silently partially delete
memory in a way that makes replay nondeterministic.

## Permission And Audit

Undo/rewind are destructive operations. They should integrate with the same
policy/audit model as direct message delete:

- If policy denies destructive transcript mutation, return a permission error.
- If policy allows it, proceed and record a resolved audit row.
- If policy asks, either create a pending permission request or reject with a
  clear "permission required" response depending on what the TUI can handle.

The command/capability truth issue should not mark undo/rewind as working until
permission/audit behavior is covered.

## TUI And CLI Behavior

After backend support lands:

- `gact undo <sid> [--count N]` should work against CLIO.
- `gact rewind <sid> <message_id> [--include-target]` should work against CLIO.
- TUI affordances may be added only if they can show confirmation for
  destructive rollback.

Until backend support lands:

- CLI should fail with a capability-aware message when connected to CLIO.
- Help/docs should not say undo/rewind works against CLIO.

## Acceptance Criteria

- CLIO implements both endpoints with the GACT response shapes.
- Missing session, invalid count, invalid target, and running session return
  structured errors.
- Successful rollback updates session metadata and removes messages from
  subsequent `GET /messages` results.
- TUI receives events or reloads cleanly after rollback.
- Permission/audit behavior is covered.
- CLI undo/rewind work against CLIO after implementation.
