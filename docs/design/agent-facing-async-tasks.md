# Agent-facing asynchronous tasks

Feature branch: `codex/agent-facing-async-tasks`, based on core PR1658
`a8e1357150271a91cf627b9cbddaedb11589f883` and GACT PR555
`6bdc2a94e0f65067bb0b2f69fd91c0dbebd9a934`. The GACT gitlink in the feature
branch identifies the matching UI commit. Merge, release and installation are
separate decisions.

## User-visible contract

An accepted task returns an opaque handle to the model immediately after durable
ownership is recorded. Acceptance is not completion. The model can continue its
turn or use these controls for a single handle or a list of handles:

| Tool | Behavior |
| --- | --- |
| `query_tasks(kind=None, status=None, handle=None, tool=None, agent=None, active_only=False, cursor=None, limit=50)` | Authorized, filtered, paginated metadata. `query_tasks("Subagent")` selects subagents. |
| `observe_tasks(tasks, cursor=None, pattern=None, timeout_s=None)` | Incremental output/progress without collecting completion. A pattern waits for a match or settlement. |
| `wait_tasks(tasks, return_when="all", timeout_s=None)` | Collect settled outcomes; `any` or `all` can mix task kinds. |
| `cancel_tasks(tasks)` | Per-handle cancellation acknowledgement or error. |
| `get_task_result(handle)` | Stored terminal result, bounded using existing output/reference limits. |

Kinds are `Subagent`, `MCP`, `Download`, `Indexing`, and `Shell`. Placement and
transport are independent attributes. The snapshot includes the original
assignment description, label, owner, effective and raw status, progress,
timestamps, supported actions, result reference, cancellation intent and
connection freshness. Missing descriptions on older records are identified.
MCP callers can provide `_clio_task_description`; CLIO validates and removes
that metadata before forwarding backend arguments.

Only the caller's conversation and descendants are visible. Authorization is
applied before filters, pagination and counts. Native backend IDs are aliases
only when exactly one authorized record resolves; the public handle always
addresses the complete owner identity.

Wait is an explicit unbounded commitment by default. Zero is a snapshot; a
positive timeout ends that waiter. Failed and invalid members have their own
outcomes and do not cancel peers. Query and observe do not consume completion.
Wait, terminal collection and automatic delivery share the durable delivery
guard. Later explicit result reads remain available.

Conversation Stop stops the current turn and waiter. Accepted tasks continue.
Explicit subagent cancellation closes admission to its subtree, cancels its
descendant task owners and publishes cancellation only after required cleanup
settles. Messaging and restart remain subagent-specific.

## Ownership and lifetime

The existing `AgentTaskRegistry`, `TaskRecordStore` and storage
`TransferOperation` remain authoritative. Shared controls project those owners;
the application supervisor retains drivers, submission RPCs and transports on
the existing application loop. It is not another execution registry.

MCP identities include server, CLIO conversation, backend session and task ID.
After the backend accepts, the existing extension persists the handle before
returning a model-visible receipt. A stopped turn cannot cancel an already-sent
task-capable submission RPC or close its transport before custody recording.
Submission health limits remain. A failed submission never returns acceptance;
uncertain requests are not replayed. Existing orphan persistence errors retain
the backend identity and best-effort cancellation outcome.

Accepted MCP drivers reuse exclusive leases, task progress, input-answer
persistence and result collection. HTTP recovery uses the original backend
session and resumes observation without calling the original tool again.
Nonrecoverable lost work is interrupted. A disconnected recoverable task keeps
its backend state separate from connection freshness.

Downloads use the existing approved transfer path with its provider identities,
selected paths, revisions, hashes and cleanup-before-terminal publication.
Indexing persists an operation before enumerating off-loop, deduplicates
equivalent active requests, checks cancellation while traversing and commits
the complete manifest/source pointer/operation in one transaction. Failure,
cancellation or interruption leaves the previous manifest intact. Failure to
record shared custody prevents a newly queued storage worker from starting.

`shell_bash(background=true)` uses the existing confinement and output limits.
The default stays foreground. The owner retains a Windows Job or POSIX process
group, incremental output and explicit execution timeout. Service shutdown
settles owned process trees and records interruption. Recovery never uses a PID
alone to reattach or automatically repeats a command.

## Completion delivery

Terminal results are persisted before completion intent and an in-memory wake.
Existing subagent inbox/enrichment machinery carries every task kind. A busy
conversation receives bounded batches at a safe model boundary. An idle
conversation retains results for its next user-initiated turn; completion alone
does not start a turn.

Staging does not consume results. Consumption happens at the existing
commit-to-run boundary after veto checks. Aborted/vetoed turns retain results,
overflow remains queued, and collection between staging and commit cannot
cause duplicate automatic delivery. Submission, progress and result use the
same invocation and handle. Supervisor polling does not fabricate tool calls.

The task dock, Observability and Runs use the same cancellation operation as
agent controls. An active cancellable task has a confirmation control naming
the assignment; subagent confirmation warns about descendants. Acknowledged
cancellation displays "Cancellation requested" until owner settlement. Dismissal
only hides a row.

## Compatibility

Newly generated default tool catalogs expose shared task controls rather than
agent-only wait/observe/cancel controls. Explicit legacy agent control aliases
delegate to the shared implementation for the first released compatibility
cycle containing this feature. Remove those aliases in the following release,
with migration notes and updated tool declarations. Subagent messaging and
restart are retained. App-less legacy MCP callers retain their existing
transparent result behavior; all supported product bridges require the shared
agent-facing contract.

## Qualification ledger

Evidence lives under the recovery directory's `mcp-agent-tasks/`. A gate passes
only from actual model-visible tool Parts and actual owner outcomes. Fixtures,
CI and executor-only probes are separate evidence.

| Gate | Current evidence and remaining work |
| --- | --- |
| Pre-change model baseline | `model-baseline-1`: actual Codex/Luna fetch returned terminal document content before the next model tool action. |
| MCP model overlap | `model-mcp-overlap-1`: actual production Web fetch accepted a handle; the model queried that handle while working. Earlier feature state; final requalification required. |
| Shell model overlap | `model-shell-acceptance-4`: actual Codex/Luna handle, running query, expired wait, incremental observation and successful stored output. Independent file-read call failed and is not credited. Earlier feature state; final requalification required. |
| Subagent, Download, Indexing model overlap | Outstanding. Must prove receipt and another action before settlement, then actual child output, bytes/hashes or manifest/counts. |
| Mixed waits, partial errors, input/permission handling | Outstanding live matrix. Focused tests do not replace it. |
| Automatic completion, next-turn delivery, duplicate prevention | Outstanding live matrix, including veto/overflow/collection races. |
| Stop, UI cancellation and subtree settlement | Outstanding live matrix on the integrated UI/service. |
| Reconnect and isolated service interruption | Outstanding live matrix for each distinct execution route. Lost nonrecoverable work must be honest. |
| Desktop and phone UI | Outstanding real-service browser interaction and visual review. |
| Five final lifecycle race repetitions | Outstanding; run after the final integrated changes. |
| Focused checks and CI | In progress; no feature CI conclusion yet. |

Every live run must record source fingerprints **before** service start, actual
runtime/model identities, invocation IDs, handles, timestamps, transcripts,
backend outcomes and filesystem/process evidence. A source change during a
run invalidates its claim to qualify a single integrated state. Unavailable
provider, platform or transport gates remain blocked and unverified and prevent
claiming complete qualification. Preserve installed Desktop/auth/defaults,
released pins, private work, rejected launches and manual-only cleanup paths.
