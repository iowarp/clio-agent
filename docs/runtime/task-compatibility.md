# Task tool compatibility

New agent catalogs use `query_tasks`, `observe_tasks`, `wait_tasks`,
`cancel_tasks`, `get_task_result` and `send_agent_message`. Existing agents may
explicitly request these shipped aliases for one Tasks release cycle:

| Legacy tool | Shared operation |
| --- | --- |
| `wait_agent_tasks(task_ids, timeout_s=None)` | `wait_tasks(tasks, return_when="all", timeout_s)`; results are returned under `results` |
| `observe_agent_tasks(task_ids, cursor=1, limit=40, pattern=None, include_state=True)` | Shared observation, with honest cursor migration below |
| `get_agent_task_output(task_id)` | `get_task_result(handle)` |
| `message_agent(task_id, message)` | `send_agent_message(target, message)` |

`cancel_agent_tasks` was an unfinished branch addition, not a shipped alias in
the selected compatibility baseline. Use `cancel_tasks`.

Native model-facing `wait_tasks` defaults `interrupt_on_mail` to true. An
authorized, eligible peer message, live human input, or question answer can
return `interrupted="mail_received"` with the actual still-pending handles.
This does not satisfy `return_when="all"`, settle/cancel a child, or consume mail.
The existing model-input commit delivers the addressed input before the next
model decision. Already committed messages do not interrupt a later wait;
task-result notices alone do not turn an all-wait into any-wait. Explicit false
keeps an outcome-only wait. The shared `TaskOperations.wait` API keeps false as
its default, so authored flow waits retain their existing control-flow contract.

An MCP-to-native Shell handoff retains the original approval for one exact
observer invocation. It does not create a session or workspace grant. Changed
arguments, app/session/workspace/assignment ownership, approval posture, applicable
policies or active playbook restrictions require the normal gate again. Reuse is
one-shot; current folder/source/symlink policy and SPOTTER clearance still apply
before native submission. PreToolUse interception remains at its original boundary;
a hook that changes the command cannot inherit approval for the old arguments.

`spawn_agent_task` takes `agent`, `task`, `placement`, `input_task_ids` and
`strategy`; `agent` names a defined subagent of the session's agent or an
installed agent (`marketplace/id`). The former `blueprint_id` argument is gone
with the blueprint system. `spawn_agents_parallel` preserves `spawns` and
`placement`; each entry uses the same agent, evidence and strategy preparation
as a single spawn. Invalid members return independent errors. Omitted or local
placement works. Special relay placement is explicitly rejected; configure an
ordinary MCP server instead.

Observation cursor `1` starts the new Core history epoch with an explicit
`legacy_history_unavailable` notice. Old numeric SSE resume positions cannot be
translated and return `legacy_observation_history_unavailable`. Returned string
cursors resume Core history. `limit` counts changed task rows, not old SSE events.
`include_state` exposes actual retained workflow state when available, never a
substitute task status. Missing state is explicit.

Output pages use a task-local Core namespace, newest chunks first, with per-chunk
offsets and continuations that retain omitted bytes. Omitted tasks receive
priority over new changes on already-served tasks in the next page. JSON fragments must be
concatenated before parsing. Pattern observation searches disclosed output and
progress messages, not descriptions or identity metadata. It scans available
pages before waiting; `search_start_cursor` identifies where that pattern search
began so earlier nonmatching output can be read independently. Matching searches
the complete integrity-checked logical output chunk, independently of its page
boundaries. A matching excerpt includes its exact span and an explicit truncation
marker; it does not pretend a partial excerpt is the entire match. No background
poller or mutable cursor store is added.

Historical IDs resolve only when unambiguous. Result reads return pending state
or the stored content with its exact reference. A result whose JSON is larger
than `limits.agent_task_output_digest_chars` is not returned inline: it is
written once to the reading session's spill folder (deleted with the session)
and the read returns `result_file` (path, size, media type, typed `reason`)
and the head of the content in `result_excerpt`, with `result: null` (#1844).
A string result is saved as text, anything else as indented JSON. Version-1
`next_cursor` values from earlier releases still read paginated fragments at
`result.preview`, identified by `chunk_field`; no new cursors are issued. Explicit
collection is acknowledged through the recipient-input commitment used by
automatic delivery. Readable results remain available afterward.

`get_task_result(handle, select=["/workflow_state/delivery"])` reads selected
RFC 6901 JSON Pointer paths into a pointer-keyed result object. Escape `/` in a
dictionary key as `~1` and `~` as `~0`; array positions use canonical zero-based
indices. Missing paths fail explicitly. Selection does not change the immutable
source reference or authorization; both normal and committed delegated-evidence
reads retain their original scope. A selected value too large to return inline
is saved to a file the same way. Unselected dictionary reads include bounded
`result_keys` metadata.
The REST result route accepts repeated `select` query parameters.

`input_task_ids` selects readable finished task results in the spawning session's
own same-human tree, including a child's completed tool results. A read grant
for an unrelated peer does not make its result shareable. The
child receives bounded quoted evidence and the source handle to read with
`get_task_result`. That read returns only the exact snapshot in its confirmed delegation input,
including after a successor continues the stable session. It grants no sibling
task rights and cannot consume the source's completion notification. A separate
task grant changing later cannot silently switch those pinned result bytes.

Aliases are excluded from newly generated default catalogs. Remove them after
the first released Tasks compatibility cycle. Old registries, drivers and
mailboxes are not restored for compatibility.
