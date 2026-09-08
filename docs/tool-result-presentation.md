# Tool result presentation

Execution acknowledgement is not result presentation. A reader of the transcript
must be able to understand what the agent did, what it learned or changed, and
what is happening now. Technical details remain the audit trail, not the only
way to understand a result.

## Observer contract

`ToolInvocation.presentation` has a concise provider-authored `summary` and
ordered blocks (`text`, `markdown`, `code`, `diff`, `terminal`, `link`, `check`,
`media`). Blocks have stable IDs. Links declare file, artifact, resource,
child-session, or URL targets. Checks carry pending/in-progress/completed state;
media carries its declared MIME type. Clients do not infer these from tool names.
Tool arguments and raw results are separate technical evidence.

Native tools must supply `presentation=` to `native_tool`: a registered native
contract or a callback `(arguments, result, structured_content) -> presentation`.
The inventory regression rejects missing declarations. Handoffs retain their
dedicated child lifecycle representation. New internal tools cannot opt out by
omitting presentation metadata.

MCP adapters are registered in `tools/tool_presentation.py`. An adapter owns its
upstream result contract and may declare `capture` and `start` hooks. The former
captures pre-call evidence (for truthful edit diffs); the latter supplies running
blocks such as the exact shell command. An unknown MCP provider receives bounded
standard content, with structured JSON retained in technical details. A standard
text mirror of structuredContent is not printed twice.

Callbacks receive isolated inputs. Presentation is observer-only: it cannot
change the result returned to the model, tool success, agent prose, or ReAct
termination. A presenter exception logs a diagnostic and leaves a compact row;
it never triggers a model retry or fabricated output.

## Persistence, paging, and streaming

Full blocks are stored on tool Parts. The v3 snapshot sends the first 2048 Unicode
characters per completed block and a session/call/block content reference.
`GET /v1/sessions/{sid}/tools/{call_id}/presentation/{block_id}?cursor={offset}`
returns a contiguous page, `next_cursor` (null at EOF), and total character count.
The endpoint resolves only content owned by that session and call; it cannot read
an arbitrary file path. Invalid cursors are errors.

`tool.presentation.delta` carries call ID, block ID, absolute Unicode-character
offset/sequence, stdout/stderr channel, and appended text. A running snapshot
contains accumulated output (a bounded tail over the wire) and `stream_offset`.
Clients append only at the expected offset; a final upsert replaces the body,
preventing repeated terminal output on completion or reconnect. Late terminal
progress after completion is ignored. Shell deltas are published before process
completion, not synthesized from the final exit code.

Transport paging is independent of visual expansion: a client may retrieve all
remaining pages in a separate viewer after one Show more action. File presenters
emit a single basename link, and edit presenters retain one surrounding context
line per hunk. The preview budget is a maximum, not a requirement to pad a diff.

## Qualification

Focused suites cover inventory, exact observation preservation, callback failure,
session-scoped paging, persistence, Unicode offsets, actual slow-shell progress,
task output, indexed todos, and Web Search conversion outputs. Live acceptance
must separately inspect read/edit/skill/shell/web/task/artifact/plan results.
Passing a test suite is not a claim that browser or CI qualification passed.
