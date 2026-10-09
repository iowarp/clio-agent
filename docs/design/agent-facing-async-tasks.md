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

The supervisor tracks outstanding acceptance RPCs against their complete
conversation owner. Subtree cancellation closes admission under the same lock
and waits for already-dispatched acceptances to finish recording and enter their
owner's cancellation path. A parent cannot publish cancellation while an
uncertain descendant submission is still settling. Stop only releases the turn's
waiter; it does not close task admission or cancel accepted work.

Relay subagents persist their complete backend key in the existing child-session
metadata before returning a fresh opaque handle. Timeline and lifecycle frames
validate the retained backend key and project onto that handle. Bare backend IDs
remain aliases only when unique; colliding IDs cannot hide another owner or
select the wrong transport after reconstruction.

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

The existing Observability context frame is refreshed at commitment with the
exact surviving task injections. Collection between staging and commitment
must remove the staged text from both model input and its recorded context
frame, while preserving unrelated context and the frame identity.

The task dock, Observability and Runs use the same cancellation operation as
agent controls. An active cancellable task has a confirmation control naming
the assignment; subagent confirmation warns about descendants. Acknowledged
cancellation displays "Cancellation requested" until owner settlement. Dismissal
only hides a row.

The shared REST projection is `GET /v1/sessions/{sid}/async-tasks`; cancellation
is `POST /v1/sessions/{sid}/async-tasks/cancel`, and explicit result readback is
`GET /v1/sessions/{sid}/async-tasks/{handle}/result`. The existing `/tasks` routes
retain the conversation to-do contract. Dismissed task records remain readable
through shared controls and result readback, while UI listings hide their cards.

## Compatibility

Newly generated default tool catalogs expose shared task controls rather than
agent-only wait/observe/cancel controls. Explicit legacy agent control aliases
delegate to the shared implementation for the first released compatibility
cycle containing this feature. Remove those aliases in the following release,
with migration notes and updated tool declarations. Subagent messaging and
restart are retained. App-less legacy MCP callers retain their existing
transparent result behavior; all supported product bridges require the shared
agent-facing contract.

Compatibility observation keeps the old numeric cursor, bounded curated rows,
workflow-state snapshot and declared structured response while using the shared
wait/Stop/authorization machinery. Grouped collection follows recorded terminal
order on both result and event lanes. Shared result collection uses the existing
model-result bound and session-owned spill references, including the complete
stored subagent output; it does not refer to an omitted compatibility tool.

## Qualification ledger

Evidence lives under the recovery directory's `mcp-agent-tasks/`. A gate passes
only from actual model-visible tool Parts and actual owner outcomes. Fixtures,
CI and executor-only probes are separate evidence.

| Gate | Current evidence and remaining work |
| --- | --- |
| Pre-change model baseline | `model-baseline-1`: actual Codex/Luna fetch returned terminal document content before the next model tool action. |
| MCP model overlap | `model-mcp-overlap-1`: actual production Web fetch accepted a handle; the model queried that handle while working. Earlier feature state; final requalification required. |
| Shell model overlap | `integrated-160-shell-ws-1` and `integrated-796-shell-sse-1`: actual Codex/Luna accepted handle, successful independent file read, later running snapshot, expired wait, incremental stdout, completed owner/exit 0 and stored result through WebSocket and SSE. Source fingerprints remained unchanged. Earlier failed parallel-read runs remain separate. |
| Subagent and Download model overlap | `integrated-ec56-subagent-ws-1` passed actual Codex/Luna acceptance, successful independent read while the child remained running, expired wait, child stdout/exit and filesystem marker. Older broad Subagent/Download verdicts are superseded by `qualification-correction.md`; strict final Download acceptance remains outstanding. |
| Indexing model overlap | Outstanding. Must prove receipt and another successful action before settlement, then manifest/counts. |
| Mixed waits, partial errors, input/permission handling | Actual Shell permission approval passed in `integrated-796-shell-sse-1/permission-resume-verdict.json`: the original model request was approved through the real UI, received acceptance and collected actual process output. The initial harness misread the permission row shape; the resumed probe used the same pending request without resubmission. Mixed waits and MCP input-answer persistence remain outstanding. |
| Automatic completion, next-turn delivery, duplicate prevention | One actual model Shell cycle passed in `integrated-796-shell-sse-1/delivery-shell-diagnose`: idle completion did not start a turn, durable intent was injected once on the next normal turn, and a later turn did not reinject it. Veto/overflow/collection races remain outstanding live gates. |
| Stop, UI cancellation and subtree settlement | Five cycles in `integrated-796-shell-sse-1/stop-shell-final` passed actual model wait, real UI Stop with Shell work continuing, one confirmed cancellation request per handle despite double click, and stored cancelled owner results. The independent OS observer failed on an HTTP read timeout and does not establish process-tree acceptance. Subagent subtree settlement remains outstanding. |
| Reconnect and isolated service interruption | `integrated-2cdd-http-recovery-2` passed actual Codex/Luna HTTP recovery after killing the isolated API: the original handle, full backend/session identity and exclusive-lease expiry were retained, query/wait/result succeeded, and one actual payload request produced the expected bytes/hash without replay. The first attempt recovered the backend but lacked the isolated API's model binding for its follow-up; it remains a failed harness gate. Lost nonrecoverable work still needs live qualification. |
| HTTP model overlap | `integrated-2cdd-http-overlap-2` passed actual Codex/Luna handle acceptance, successful independent file read, a later running query, wait/result and one real 11,264-byte payload with verified hash. The first attempt's external search timed out and did not qualify overlap. |
| Desktop and phone UI | Visual review rejected the phone confirmation on GACT `e7ee1c77`: global popover layer 110 overrode the dialog utility classes and obscured its body. Real DOM diagnostics retained in `integrated-796-shell-sse-1/dialog-diagnostic.json`. The explicit task-confirmation layer repair requires fresh rendered qualification. |
| Subagent result readback | Actual UI cascade on `integrated-ec56-subagent-ws-1` settled the child-owned Shell process tree, then GET result failed with `LoopThreadStoreWrite` while recording terminal provenance. The real-store regression reproduced that failure before the off-loop route correction and passed afterward. Five complete final live cascades remain required. |
| Five final lifecycle race repetitions | Earlier five Shell cycles passed lifecycle behavior; the final integrated UI state must be repeated and visually accepted, with complete process identity evidence. |
| Focused checks and CI | GACT `16cffbbf` has 13 successful checks and seven conditional event/release jobs. Core `2cdd6b3d` exposed a renewal worker that starved a no-delay polling test and a renewal assertion tied to a 150ms scheduling window. The existing divergence case passes with an independent event-loop renewal timer; the competing-resume test now observes actual renewal and advances its lease clock explicitly. No test limit, production TTL or assertion was relaxed. Fresh exact-head CI remains required. Existing skipped Python tests and the chat-only WebView permission-card exclusion do not count as passed acceptance. |
| Parallel Codex arguments | `model-parallel-argument-diagnostic-1` recorded correct actual model arguments and empty tool inputs on the coalesced parallel path. The repaired bridge reconciles completed snapshots on WebSocket and SSE. Three focused cases pass; actual model parallel query/file-read succeeds through both transports in the Shell runs above. |

Later integrated evidence on core `5e791be8` and GACT `16cffbbf`:
`integrated-5e79-shell-sse-1` passed strict model overlap and native permission
approval; `stop-shell-final2` passed five Stop/UI-cancel cycles with desktop and
phone dialog review and direct PID/birth/parent-chain evidence for all five
PowerShell owners and their Python descendants. `delivery-shell-fresh1` passed
five fresh-conversation idle/next-turn/no-duplicate cycles. The earlier long
SSE conversation hit the unchanged 65,536-byte parser limit and remains an
unqualified route; fresh-conversation evidence does not repair or qualify it.

The actual collection hook in `delivery-races-collection` collected the result
after staging and before commitment. The real model correctly reported "nonce
absent", but the finalized Observability frame still included staged task text.
The focused collected/delivered regressions reproduce that stale frame and pass
after updating it at the existing commit boundary. Fresh actual-model delivery,
collection, veto and overflow repetitions remain required for this correction.

`integrated-487f-delivery-sse-1/delivery-races-collection-explicit-read-accounted`
now passes five actual-model collection cycles with no automatic reinjection,
no explicit result reads and truthful committed frames. The first collection
batch retained a probe failure when a model explicitly requested the stored
result despite its instructions. That permitted read supplied its quoted nonce;
it was not automatic delivery. The revised probe accounts for actual explicit
read telemetry and still requires the absence of automatic injection. Veto and
overflow repetitions remain outstanding. Exact-head CI on `487f6aab` found the
new test's imported fixture missing under full-suite collection. The regression
fixture is now module-local; its behavior and assertions are unchanged.

`integrated-context-final-subagent-1` passed strict model overlap on core
`59ce2dbd` before connecting any source. Its later connected-source descendant
probe accepted a real Download, but the host rejected Shell because its child
process fence cannot enforce connected-source exclusions. That combined route
is blocked and unverified; do not retry or bypass it without the human's fence
state changing. Native storage and task controls remain independently available.

Cancellation of that already accepted Subagent and Download through the real
phone UI passed owner settlement and cleanup custody once. Read-only durable
records, original source pointers, staging absence and upstream bytes/hash are
retained in `existing-download-phone-early/storage-cancel-proof.json`. Its visual
gate failed: the long assignment pushed the warning and action buttons below
the viewport. GACT `d51b77a4` preserves the complete assignment in a focusable
scroll region while keeping the warning and controls visible. Two focused
short/long cases, scoped checks, all six guards, TypeScript and both builds pass;
fresh real-service visual and five lifecycle repetitions remain required.

On core `959e2238` / GACT `d51b77a4`, the native Download-only subtree probe
passes five actual-model/UI lifecycle cycles with one confirmed cancellation
request each, honest pending cancellation, cancelled descendant owners and
stored parent result readback. Desktop and phone captures and hit tests show
the complete warning and controls above the originating popover. All five
durable storage audits retain original manifest pointers, unchanged upstream
payload bytes/hash and absent staging trees. These are native storage routes;
the connected-source Shell route remains blocked.

The original 24,001-file selection also completed after real UI Stop ended its
model waiter. Byte progress continued without a cancellation request; all
18,228,063 selected bytes and file hashes match. Its coordinator delegated
submission, so that run does not qualify submitting-model overlap. The direct
single-agent native Download gate in `integrated-959e-download-root-1` does:
the actual Codex/Luna model receives acceptance in one model step, performs a
successful checklist action in a later step, observes the original handle
running, expires a wait without cancellation, then waits and retrieves its
completed result. All 6,001 selected files / 6,132,063 bytes and hashes match.
The separate five-cycle overlap audit is retained honestly: only one child
cycle established its independent action before a query with no cancellation
request. It is not five complete overlap passes.

GACT's exact `d51b77a4` CI passed 71 browser cases and failed its existing chart
fullscreen case after a wheel scroll unmounted the virtualized tail. Retained
CI trace/context show the missing chart and available Scroll-to-bottom control.
The original focused case passed locally; it was not reproduced as a local
failure. The test now returns through that real reader control and asserts
canvas remount before fullscreen. Its focused Chromium case and scoped checks
pass. All existing canvas/fullscreen/return/no-error assertions and budgets are
retained; production code and the qualified build are unchanged by this repair.

The single-agent native Indexing gate `integrated-4c7c-indexing-root-1` passes
actual Codex/Luna acceptance, a subsequent checklist action while running,
expired wait, observation, unbounded wait and stored result. Its atomically
published manifest exactly matches 48,098 real entries. Five fresh owned-folder
UI cycles also pass Stop independence, confirmation hit tests at desktop/phone
widths, exactly one cancellation request and actual owner settlement. Durable
audits show progress before cancellation and no partial manifests. Reconnecting
an already linked source correctly returns its existing link instead of starting
another index; the first reuse probe is retained as a probe failure.

An explicit public refresh exposed a real acceptance response defect: storage
custody persisted its handle, but the REST link/transfer routes serialized the
older operation object. The Indexing regression reproduces the empty handle.
Both routes now reload the persisted operation before returning 202. Two focused
real API cases pass identity/query/result joins and actual entry or byte/hash
checks; scoped Ruff/Pyright and guards pass. The initial refresh finished normally
before service retirement; it is not an interruption acceptance pass. Fresh live
refresh cancellation against an existing manifest remains required after this fix.

Every live run must record source fingerprints **before** service start, actual
runtime/model identities, invocation IDs, handles, timestamps, transcripts,
backend outcomes and filesystem/process evidence. A source change during a
run invalidates its claim to qualify a single integrated state. Unavailable
provider, platform or transport gates remain blocked and unverified and prevent
claiming complete qualification. Preserve installed Desktop/auth/defaults,
released pins, private work, rejected launches and manual-only cleanup paths.
