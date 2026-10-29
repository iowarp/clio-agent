# Tool result presentation

Execution acknowledgement is not result presentation. A reader of the transcript
must be able to understand what the agent did, what it learned or changed, and
what is happening now. Technical details remain the audit trail, not the only
way to understand a result.

## Adding or changing a tool

Design the presentation alongside the callable contract, before implementing a
new tool or changing its arguments, result shape or lifecycle. Answer these
questions with concrete example data:

| User question                              | Where the answer belongs                                                      |
| ------------------------------------------ | ----------------------------------------------------------------------------- |
| What is the agent doing, to what?          | One compact action row with a named target or useful query/filter and counts. |
| What did it ask, send or select?           | The actual assignment, message or consequential selection on expansion.       |
| What came back or changed?                 | Rendered content, named matches, a truthful diff, files or returned reports.  |
| Did this operation succeed?                | Its recorded semantic outcome, separate from submission or child completion.  |
| What are the exact arguments and response? | Technical details, preserving the recorded evidence.                          |

Keep the row short. Do not add mandatory Input/Output boxes, repeated status
sentences, Work paragraphs, link-opening instructions, decorative dots or em
dashes. Names and scope values should themselves navigate to the correct
recorded entity. Keep routine transport options and opaque IDs in details.

Use these ownership points:

1. Declare the result at the producer. Built-in registrations use `presentation=`
   on `gact/agents/tool_instrumentation.py::native_tool`, with an existing native
   presenter or an observer callback. This is distinct from the SDK's
   `native_tool("name")`, which only claims a built-in. MCP integrations use
   `tools/tool_presentation.py::PresentationAdapter` and its registration seam.
   Python/plugin tools must follow the runtime instrumentation path that exposes
   them; do not invent an unsupported presentation argument on `sdk.tool`.
2. Preserve exact inputs, model observations and lifecycle evidence. Presenters
   must not rerun work, change the model's result or mark acceptance as success.
   Use stable block IDs, typed links and existing retained-content paging.
3. Coordinate the consumer declaration and shared reader in GACT. Follow its
   [authoring steps](../external/gact-tui/docs/tool-result-presentation.md#adding-or-changing-a-tool).
   New integrations should produce meaningful standard content even before a
   specialized adapter exists. Do not require a frontend viewer for every tool.
4. Add a showcase of actual request/result shapes: a small success, a large
   result and applicable empty, partial, denied, failed, interrupted and running
   states. Keep synthetic data in tests/review fixtures, never production tools.
   Include long names and wrapped content, not just short happy-path strings.
5. Assert meaningful content, identity, outcomes and unchanged observations in
   the producer tests. Review the production chat/Evidence components at desktop
   and phone widths. Exercise disclosure, navigation, paging and keyboard access.
   Registration coverage alone is insufficient. A screenshot of the final
   answer does not establish the correctness of intermediate tool results.

For example, a delegation row is `Delegate to [Independent audit]` with its
status mark. Expansion shows the named assignment, its agent/model when recorded
and the actual assignment text. A wait names the tasks and returned/running
counts; it stays closed until content or a collection error exists. Returned
reports use named tabs. Pending and uncertain task snapshots are not reports.
Search shows the query in its row and linked matches/excerpts on expansion.
Memory changes show before/after content and a linked scope value.

Application failures remain failures even if transport succeeded. An interrupted
wait is not a failed child. Keep success marks quiet; do not apply the Work
rail's task-kind coloring to every transcript tool. Large results need bounded,
readable previews and a working path to complete retained content. A missing
historical result must be disclosed, never replaced with today's state.

Record synthetic presentation review separately from a fresh native run. Live
acceptance must inspect the request, intermediate tool outcomes and recovery,
not merely whether the agent eventually produced plausible prose.

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
The inventory regression rejects missing declarations. Handoffs retain named
child identities and lifecycle evidence; the UI presents each assignment once.
New internal tools cannot opt out by
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

## Lifecycle and coverage expectations

Native declarations also support an optional `presentation_start(arguments)`
callback. The instrumentation seam preserves it across wrappers and isolates its
inputs; failure produces a technical diagnostic without invoking or retrying the
tool. Wait uses this to identify children while its single call stays open.
Wait identifies its selection while running. When its recorded receipt includes
collected results, present their actual reports in named tabs; do not replace
them with a status recap. Observe owns progress snapshots and task selection;
explicit result reads expose the retained answer. Presentation never changes
the model-facing wait or observation result.

File reads declare Markdown, plain text, or code at the adapter boundary. Valid
Markdown frontmatter is presented as readable scalar metadata followed by the
rendered body; malformed frontmatter remains intact. Original source stays in
technical evidence. Clients do not infer formats from filenames or result keys.

The completion gate is this entire inventory, not three illustrative tools:

| Family / tools                                                          | Meaningful observer result                                                  | Required live evidence                                                          |
| ----------------------------------------------------------------------- | --------------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| `fs_read_file`                                                          | One filename link; size; rendered document or source                        | Text, Markdown, code, long expansion                                            |
| `fs_write_file`, `fs_edit_file`                                         | Truthful diff, one context line; no padding                                 | Created, overwritten and edited files                                           |
| `load_skill`                                                            | Loaded procedure or exact supporting reference                              | Real installed skill, not metadata alone                                        |
| `spawn_agent_task`, `spawn_agents_parallel`, `run_flow`, `run_workflow` | Named assignment/flow, actual request, truthful lifecycle                   | Start, refusal, failure, successful return                                      |
| `wait_tasks`                                                            | Named selection, returned/running counts; tabs containing collected reports | Pending stays closed; interruption, collection error and large returned content |
| `observe_tasks`                                                         | Named tasks and recorded progress snapshots                                 | Snapshot and exact task navigation                                              |
| `get_task_result`                                                       | Named child and readable retained report                                    | Large report, pinned paging and missing evidence                                |
| `write_todos`                                                           | Actual items with accessible status icons                                   | Three-item preview, all statuses, expansion                                     |
| `create_artifact`                                                       | Artifact links or explicit rejection reasons                                | Open the registered artifact                                                    |
| `workspace_resource_list/inspect/read/search/structure/wait`            | Resource identity, custody sizes, processing, evidence                      | Real uploaded resource and derivatives                                          |
| `goal_status`                                                           | Condition and progress, or explicitly no active goal                        | Read-only status                                                                |
| `cron_create/list/delete`, `loop_wakeup`                                | Actual schedule, timezone, next fire, removal state                         | Disposable session-owned schedule lifecycle                                     |
| `send_agent_message`                                                    | Named recipient link and actual sent message                                | Message to a real qualification child                                           |
| `refresh_provider_models`                                               | Provider/source/default and actual changes/errors                           | Read-back of authoritative result                                               |
| `plan_exit`, `ask_user`, `raise_alert_card`, `create_a2ui_surface`      | Existing dedicated review/interaction surface                               | Real rendered surface and controls                                              |
| MCP standard text/resource/media                                        | Bounded standard content; JSON in technical details                         | Text/resource/media preview                                                     |
| `web_search/fetch/fetch_events`                                         | Sources, conversion progress, saved outputs, degradation                    | Ready service and real asynchronous PDF conversion                              |

Registration tests are a coverage floor. Each row additionally needs meaningful
result assertions and live inspection; an unavailable path stays unqualified.
The readability guard also rejects a string-only presenter on a structured-returning
native callback. Contract tests must exercise real result shapes, errors and empty
results, with unchanged model observations and consistent history, paging and export
views. A declared presenter with empty output is still an incomplete implementation.
