# Tool result presentation

Execution acknowledgement is not result presentation. A reader of the transcript
must be able to understand what the agent did, what it learned or changed, and
what is happening now. Technical details remain the audit trail, not the only
way to understand a result.

## Three levels of understanding

The compact row answers **what action, on what, with what outcome**. Use a short
human purpose, selected filename, query, destination or named result. Stable IDs
remain available in expansion and technical details; they are rarely a useful
subject on their own. Descriptive subjects use normal text; commands and file
paths can retain monospace styling.

Expansion answers **what was requested and what was actually returned or changed**.
Show the relevant selection, returned items, checks, readable content, saved output
links, coverage and limitations. An exit code alone is not command output, a byte
count is not an API response, and a successful inspection does not prove its
widget is ready. Explicitly identify an empty recorded command output. Present
missing imports and failed HTTP responses as such even if execution returned a
structured receipt successfully.

Technical details preserve the exact recorded arguments, output, diagnostics and
timing. Routine timeout ceilings, internal scheduler limits and transport options
belong here. Actual timeouts and limits that affected the result remain visible
in the compact/expanded result. Do not copy an entire structured result into the
middle view just to fill an empty panel.

For `load_skill`, distinguish the main procedure from supporting reads. The main
row uses the skill's display title or recorded description/heading; a reference
row names its exact relative file or distinct-file count. Expansion includes the
skill ID, purpose, each requested file (including JSON fragments), actual resolved
source links and bounded loaded content. Present the recorded discovery description
as **When to use**, before the instructions, and retain it when opening the full
viewer. Keep the directory and source links under Source; loader-only instructions
such as `use as SKILL_ROOT` do not belong in the human document body. The metadata
comes from the resolved skill used for discovery and is retained with the load
event; do not replace missing historical descriptions with today's text.
A reference must never open `SKILL.md`
merely because older metadata recorded only the main skill path. Display titles
do not rename skill IDs or change model-facing procedures.

Historical presentation repairs use retained inputs and observations only. Never
read today's skill as if it were the original instruction, rerun an old request,
or rewrite the evidence ledger. Native archives can recover a full observation
from their durable tool record when message metadata kept only a preview. Without
that evidence, disclose the partial record and keep its raw preview in technical
details rather than inventing a successful result.

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

Question and Plan approval boundaries persist the observed assistant turn before
retiring its live ledger. A resumed answer starts a separate assistant turn;
previous tool parts remain exactly once in the paused turn. No answer is
synthesized, and the pending question/approval remains in its own durable ledger.

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

## Refinement audit (2026-09-08)

Native declarations also support an optional `presentation_start(arguments)`
callback. The instrumentation seam preserves it across wrappers and isolates its
inputs; failure produces a technical diagnostic without invoking or retrying the
tool. Wait uses this to identify children while its single call stays open.
Completed Wait reports each child's identity, status and observed duration, never
its answer. Observe owns incremental event excerpts; explicit task collection
owns the readable report. Model-facing wait and observation results are unchanged.

File reads declare Markdown, plain text, or code at the adapter boundary. Valid
Markdown frontmatter is presented as readable scalar metadata followed by the
rendered body; malformed frontmatter remains intact. Original source stays in
technical evidence. Clients do not infer formats from filenames or result keys.

The completion gate is this entire inventory, not three illustrative tools:

| Family / tools | Meaningful observer result | Required live evidence |
| --- | --- | --- |
| `fs_read_file` | One filename link; size; rendered document or source | Text, Markdown, code, long expansion |
| `fs_write_file`, `fs_edit_file` | Truthful diff, one context line; no padding | Created, overwritten and edited files |
| `load_skill`, `spawn_skill_task` | Loaded procedure, or dedicated child lifecycle | Real installed skill, not metadata alone |
| `spawn_agent_task`, `spawn_agents_parallel`, `run_flow`, `run_plan` | Dedicated indexed child lifecycle | Start, failure, successful return |
| `wait_agent_tasks` | Requested children; terminal statuses and durations | Single open wait; no repeated answers |
| `observe_agent_tasks` | Incremental progress events and child links | Snapshot and patterned observation |
| `get_agent_task_output` | Child link and readable Markdown report | Large report in bounded/full views |
| `write_todos` | Actual items with accessible status icons | Three-item preview, all statuses, expansion |
| `create_artifact` | Artifact links or explicit rejection reasons | Open the registered artifact |
| `workspace_resource_list/inspect/read/search/structure/wait` | Resource identity, custody sizes, processing, evidence | Real uploaded resource and derivatives |
| `goal_status` | Condition and progress, or explicitly no active goal | Read-only status |
| `cron_create/list/delete`, `loop_wakeup` | Actual schedule, timezone, next fire, removal state | Disposable session-owned schedule lifecycle |
| `message_agent` | Recipient link, sent message, delivery posture | Message to a real qualification child |
| `refresh_provider_models` | Provider/source/default and actual changes/errors | Read-back of authoritative result |
| `plan_exit`, `ask_user`, `raise_alert_card`, `create_a2ui_surface` | Existing dedicated review/interaction surface | Real rendered surface and controls |
| MCP standard text/resource/media | Bounded standard content; JSON in technical details | Text/resource/media preview |
| `web_search/fetch/fetch_events` | Sources, conversion progress, saved outputs, degradation | Ready service and real asynchronous PDF conversion |

Registration tests are a coverage floor. Each row additionally needs meaningful
result assertions and live inspection; an unavailable path stays unqualified.
The readability guard also rejects a string-only presenter on a structured-returning
native callback. Contract tests must exercise real result shapes, errors and empty
results, with unchanged model observations and consistent history, paging and export
views. A declared presenter with empty output is still an incomplete implementation.
