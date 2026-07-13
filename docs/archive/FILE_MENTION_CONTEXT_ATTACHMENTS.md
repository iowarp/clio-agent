# File Mention Context Attachments

## Purpose

Make TUI `@file` mentions behave as real session context attachments instead of
raw prompt text that later fails as an invalid path.

This is a cross-repo feature/bug because the TUI owns picker and composer
behavior, while CLIO owns backend context-file attachment, workspace path
resolution, and message execution semantics.

## Current Understanding

CLIO already has the backend pieces for explicit context files:

- `GET /v1/workspaces/{wid}/files` lists workspace-relative file picker rows.
- `GET /v1/workspaces/{wid}/files/read` previews one workspace file.
- `POST /v1/sessions/{sid}/context/files` attaches a file to session context.
- `_enrich_with_context_files(...)` prepends attached file content or metadata
  before a turn reaches the agent.

The observed user-facing failure is different: the file picker works and inserts
an `@...` mention into the composer, but after sending, the turn errors. That
suggests the selected mention is not being converted into a valid session
context attachment, or the raw `@` token is still being interpreted as a literal
filesystem path by the agent/tool layer.

## Failure Classes To Audit

1. The TUI inserts `@path` but never calls `POST /context/files`.
2. The TUI calls `POST /context/files` with a workspace-relative path that CLIO
   resolves against the backend process working directory instead of the
   workspace root.
3. The TUI attaches the file but leaves `@path` in the message text, causing
   downstream path extraction to hand tools a path prefixed with `@`.
4. CLIO accepts an attached file but later fails because the file moved, was
   deleted, is not a file, cannot be inspected, or exceeds policy.
5. A binary scientific file is attached as text and should instead go through the
   structured binary inspector path.

## Desired Contract

The picker/composer/send path should be explicit:

1. User opens the TUI file picker with `@`.
2. Picker displays files from `GET /v1/workspaces/{wid}/files`.
3. Selecting a file inserts a visible mention in the composer.
4. Before sending, every unresolved file mention is attached through
   `POST /v1/sessions/{sid}/context/files`.
5. The attachment payload must use a path CLIO can resolve unambiguously:
   either an absolute path, or a workspace-relative path plus workspace id if the
   backend contract is extended.
6. Once attached, the prompt text sent to the agent must not contain raw `@`
   prefixes that tools could interpret as part of the path.
7. Errors must tell the user which stage failed: picker list, attach, preview,
   context enrichment, or tool execution.

## Backend Work In CLIO

- Decide whether `POST /v1/sessions/{sid}/context/files` should accept
  workspace-relative paths with `workspace_id`, or whether the TUI must always
  send absolute resolved paths.
- If workspace-relative paths are accepted, resolve them against the session or
  provided workspace root and reject path traversal.
- Add optional structured metadata to context file rows indicating source:
  `source=mention`, `workspace_id`, `display_path`, `resolved_path`.
- Return structured `context_file_error` details for attach-time failures such
  as missing files or directories selected as `read`/`pin` context. The details
  include the original path, resolved path, workspace id, mode, failed operation,
  and recovery actions for the TUI.
- Add tests for picker row -> context attachment -> message send.
- Add tests that raw `@path` does not reach tools as a literal path when it was
  intended as an attachment.
- Preserve current explicit attachment behavior for API users.

## TUI Work In gact-tui

- Track selected file mentions as structured composer attachments, not only as
  decorated text.
- On send, attach each selected file before posting the message.
- Normalize or remove raw `@` markers from the message text once attachment is
  complete.
- Keep visible mention chips/text in the composer/history so the user can see
  what was sent.
- Surface attachment-stage errors inline and keep the draft message editable.
- Support retry after fixing or removing a failed attachment.

## Acceptance Criteria

- Selecting a file with the TUI picker and sending a message no longer produces
  file-not-found errors caused by an `@` prefix.
- A selected text file is visible to the agent through attached context.
- A selected Parquet/HDF5/ADIOS-style file uses CLIO's structured file summary
  or relevant file tool path rather than raw binary text.
- Relative picker paths are resolved consistently against the active workspace.
- Missing/deleted files produce `context_file_error` with recovery actions,
  not a generic failed turn.
- Regression coverage exists in both repos for the cross-repo contract.

## Related Issues

- CLIO backend contract issue: create in `iowarp/clio-agent`.
- TUI picker/composer issue: create in `iowarp/gact-tui`.
- Memory/context truth issue: context attachment provenance should eventually
  feed into the authoritative context-frame model.
