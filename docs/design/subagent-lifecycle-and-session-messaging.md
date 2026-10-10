# Subagent lifecycle and session messaging

Status: source audit and proposed extension, 2026-10-10. The audited core is
`8feb486cb84a3c4587d9c424b69861e93925933b` on
`codex/agent-facing-async-tasks`. This document does not claim implementation or
live acceptance of the additional controls below.

## Existing behavior

| Operation | Current implementation |
| --- | --- |
| Inspect, observe, wait, result | Shared task tools accept Subagent handles, with conversation/descendant scope. |
| Cancel | `cancel_tasks` closes subtree admission and requests cancellation of the child and descendant tasks. Terminal publication waits for owners and cleanup. |
| Message a running child | `message_agent(task_id, message)` chooses the retained local or relay placement. Local messages enter the next safe iteration boundary; relay messages use the retained task input channel. |
| Message a completed child | Creates a new child task/session with the original briefing, previous output and new message. The old task remains terminal and a successor relation is emitted. This is not resuming the entire old provider conversation. |
| Message a failed/cancelled child | Refused as `task_unwakeable`. |
| Pause/resume | No dedicated model-facing or task UI lifecycle control found. Waiting for human input and waiting for background work are existing states, not explicit operator pause. |
| Restart | No explicit model-facing restart operation found. Completed-child messaging implements one restricted successor path. |
| Cross-session messaging | The tool takes a task ID, not a session ID from memory search. The shared messaging helper looks up the application-wide task registry without the caller-session authorization used by shared task controls. This is not a complete discovery/addressing/authorization contract. |

Sources: `gact/agent_messaging.py`, `gact/agent_message_transport.py`,
`gact/live_handle.py`, `gact/agent_tasks.py`, `gact/task_subagent_owner.py`,
`gact/task_projection.py`, `gact/turn_spawn.py` and
`gact/agents/spawn_runtime_declarations.py` under `src/clio_agent/`.

## Pause, Stop, cancel and restart

These meanings are distinct. The proposed default for Pause is a durable hold
until explicit resume or an explicitly waking message. This choice was asked
of the user and remains a proposed default until confirmed.

| Action | Agent execution | Accepted descendant tasks | Identity and notifications |
| --- | --- | --- | --- |
| Conversation Stop | Stops the current model turn and its waiter. | Continue. | Existing task completion can automatically wake the agent. |
| Pause subagent | Stops model execution cooperatively and holds future automatic model starts. | Continue; progress and results remain durable. | Same Subagent handle/session; nonterminal `paused` effective status. No completion notification for merely pausing. |
| Resume subagent | Clears the hold and continues using retained context and pending messages/results. | Continue. | Same handle/session; resumes once at an eligible boundary. |
| Cancel subagent | Ends this run and closes its subtree admission. | Explicit cancellation cascades through descendants, with cleanup before settlement. | Original handle settles `cancelled`; does not become active again. |
| Restart subagent | Starts a new linked run after the prior run has settled. | Never automatically replays or duplicates accepted operations. | New handle; old result/history immutable. Default context continuation, with an explicitly requested fresh mode. |

Pause is neither OS process suspension nor a promise that an ordinary foreground
RPC stops immediately. It must report `pause_requested` until the provider/turn
owner has stopped reaching further model/tool boundaries. Execution safeguards
for in-flight foreground work remain intact. Accepted background tasks must not
inherit the pause signal as task cancellation.

A paused Subagent remains active for queries and mixed waits. An unbounded wait
can remain pending until resume or cancellation. Its displayed supported actions
include resume, message, observe and cancel; generic MCP/Download/Indexing/Shell
tasks do not acquire subagent pause or restart semantics.

Restart defaults to continuing the retained assignment/context, rather than
blindly running the initial assignment again. The new run must know which work
already completed, failed or remains accepted. A fresh restart is an explicit
mode that preserves old evidence and still does not replay accepted operations
implicitly. If restart is requested while running, first settle the old run;
never allow overlapping replacements or reopen a cancelled subtree in place.

## Proposed model-facing controls

Keep shared `query_tasks`, `observe_tasks`, `wait_tasks`, `cancel_tasks` and
`get_task_result`. Add subagent-specific `pause_agent`, `resume_agent` and
`restart_agent`. Extend the existing `message_agent` instead of creating a second
message transport.

Targets must be explicit and unambiguous: a task handle for one run, or a session
ID for one conversation. Preserve the old `task_id` argument as a compatibility
alias. A session target resolves its current eligible run, or explicitly creates
a linked continuation when the previous run is terminal. An expert ID such as
`ndp` alone is insufficient because several instances may exist.

`message_agent` returns a durable message acknowledgement immediately, including
message ID, source/target sessions, target run handle, disposition and any
successor relation. Busy recipients see it before their next model iteration;
idle recipients wake without another human message. Paused recipients can receive
and retain messages; a declared `wake` option decides whether the message resumes
them. Terminal recipients create a linked continuation, including after failure
or cancellation, rather than modifying the old terminal result.

Messages retain sender identity and provenance. Peer-agent messages are agent
content, not human instructions or parent-authoritative steer. Preserve the
recipient's agent definition, workspace, model policy and permissions. Delivery
must not transfer the sender's capabilities or silently reparent the recipient.
Replies need explicit reply/correlation IDs; do not create an unconditional
automatic reply loop. Task completion continues to belong to its original owner;
peer messaging does not consume another conversation's completion mailbox.

## Find a session, read context, then address it

The existing memory tools are a useful foundation, but their names hide limits:

| Tool | Actual source and behavior |
| --- | --- |
| `memory_search_sessions` | Lexical term matching over retained transcript `text`, `thinking` and `error` parts, ranked by matched-term fraction and recency. Hits carry session/message/part IDs and bounded excerpts. It is not a search over generated session summaries or a semantic ARC index. |
| `memory_read_session_summary` | Generated on demand from session metadata, message counts and up to the last five messages, each excerpt capped at 360 characters. It also returns message IDs and rollback metadata. No summarizer model creates this value; it is not a whole-session narrative. |
| `memory_read_context_frame` | Reads one retained context-assembly record: model/agent identity, referenced messages/files/injections, inclusion decisions and token estimates. It returns up to 50 items. It is not a general reader for the contents of those referenced messages/files. |

`_record_context_frame` creates frames during turn preparation. The audited path
stores them in a bounded `app.state.context_frames` ledger initialized empty at
application startup. No durable frame restore path was found in this audit;
therefore do not promise historical frame availability after restart. Message
and session persistence are separate from this frame ledger.

Sources: `gact/memory_tools.py`, `gact/routes/memory.py`,
`gact/runtime/memory_search.py`, `gact/enrichment.py`, `gact/turn_start_offloop.py`,
`gact/runtime/retention.py` and `gact/app.py` under `src/clio_agent/`.

The current memory policy allows same-session reads, same-workspace reads with
user intent, and separately scoped global-workspace reads. Arbitrary other
workspaces remain denied. Global workspace is not a wildcard over all projects.
Search filters for agent/kind/status/time and cursor pagination are desirable
extensions; the current native search exposes query/scope/limit/user_intent only.

Keep memory retrieval and execution controls distinct. Add live session/run
identity and addressable target information to discovery results after scope
checks. A discovered session does not automatically grant pause/cancel/restart
authority. Owner/descendant controls retain their existing authority; authorized
peer messaging is a separate capability. Cross-workspace messaging needs explicit
policy, rather than assuming that knowing an ID grants access.

For deeper reading, follow provenance to bounded message/context ranges with
cursors. A real authored session summary, if added later, must include covered
message range, source references, creation time and freshness. Never present the
current last-five-message projection as that feature. Frame persistence, frame
listing/discovery and bounded content reads also need explicit contracts.

## Implementation seams and verification

1. Persist pause intent in the existing session/task owners before acknowledging.
   Stop provider iteration/waiters without calling subtree cancellation. Guard
   task-result wakes, residual-message promotion, question resumes, scheduled/goal
   starts and queued subagent admission against the hold. Preserve all pending
   delivery intents. Restore the hold after service restart.
2. Keep paused child completion callbacks nonterminal and rebind them on resume.
   Update shared projection/actions, slot admission and parent mixed waits.
   Do not free a slot while an old worker still runs; define paused idle admission
   independently from live worker capacity.
3. Extend the existing successor path for explicit restart and terminal messaging.
   Preserve full retained-context provenance, immutable old results and original
   accepted task identities. Serialize competing restart/message/cancel actions.
4. Introduce a shared target resolver and messaging policy used by both HTTP and
   native tools. Connect scoped memory discovery to exact session/run targets.
   Preserve local/relay owner routing and sender provenance.
5. Add matching UI actions and shared model guidance; keep dismissal distinct.
   Expose pause requested, paused, cancellation requested and terminal states
   honestly. Cancellation confirmation continues to identify descendant work.

Focused checks must cover pause-versus-completion/message/admission races,
restart persistence, mixed waits on paused children, idempotent commands,
unauthorized targets, cross-session sender attribution, terminal successors,
delivery guards and rollback/deleted context ranges. Run cases individually and
sequentially with one worker, then scoped checks and unchanged guards.

Live qualification requires actual parent/child models: pause a long child,
prove model iteration stops while its real task keeps running, complete work
during the hold, resume once with retained results, then separately cancel the
subtree and verify process/provider cleanup. Restart failed/cancelled/completed
runs with new handles and no replay. Find another real session through memory,
message it while busy and idle, and verify next-iteration/wake delivery, reply
identity, no duplicate and no silent reparenting. Include UI review and all
distinct local/relay/provider routes. Repeat affected lifecycle races five times.

This source audit is not a live model demonstration. Earlier async-task receipts
keep their original source identities and do not qualify these proposed controls.
