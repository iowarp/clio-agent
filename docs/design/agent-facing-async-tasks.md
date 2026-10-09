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

The editable `clio.runtime.tasks` snippet is composed at the common model-request
boundary for roots and children with shared task controls, independent of their
provider or blueprint body. It teaches immediate handles, all five task kinds,
queued success/failure/interruption, busy-boundary versus next-turn delivery and
Stop versus task cancellation. Agents continue independent work, finish an
acknowledgement-only turn without waiting, or commit one wait when the current
request needs the result. Keeping a turn open is not required to keep work alive.
Orchestrator and spawn descriptions use the same lifecycle vocabulary.

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

Implementation is present on the integrated feature branch. **Release
qualification is incomplete.** The live matrix below describes executed gates,
including partial and blocked results; it is not a claim that every route passes.

The historical broad model-run runtime state is core
`4dc4b1c1c2757be716f094c5cbe1058b34a7a26c` with GACT
`5e8cee6cf38c9329d82f151d2305b8e2c0783507`. Later documentation-only commits
must preserve the runtime file hashes and matching GACT gitlink. A subsequent
cursor-validation correction rejects malformed pagination positions before
key comparison; its focused controls and real HTTP-route checks are recorded
in `query-cursor-*`. That input path has not been exercised by a live model.
The correction changes only `task_controls.py` in runtime source; it does not
relabel the earlier model runs as acceptance of the current head. Older evidence
retains its actual source identity and is not relabelled as a final-state run.

Draft review branches are core PR1662 and GACT PR557 into `develop`; the latter
target was changed during the separate human integration campaign.
Their retained PR1658/PR555 bases remain dependencies. Released branches,
installed Desktop, authentication, defaults and released pins are preserved.

Evidence is retained at
`D:/Libraries/Videos/clio_recordings/2026-10-07-release-recovery/mcp-agent-tasks/`.
Each run records source/runtime/model identities, actual model tool Parts,
invocation IDs, handles, timestamps and owner outcomes. A model-overlap pass
requires acceptance, a successful independent action in a later model step, and
a subsequent query of the same uncancelled running handle. Direct executor
probes, fixtures and CI do not satisfy that gate.

| Executed gate | Evidence and scope |
| --- | --- |
| Pre-change model baseline | `model-baseline-1`: actual Codex/Luna production fetch returned terminal content before the model's next action. The earlier direct-executor probe was not model acceptance. |
| MCP stdio model overlap | `final-e35e4-mcp-ui-3` passed strict actual Codex/Luna acceptance, independent file action in a later model step, subsequent uncancelled running query, expired wait with work continuing, observation and original result collection on coree35e4/GACT898a54. Its real 11,264-byte fetch matched its hash. Earlier passes retain their recorded source identities. |
| MCP Stop/UI cancellation | `final-e35e4-mcp-ui-4/stop-mcp-disconnect-recorded` passed five fresh actual-model cycles on coree35e4/GACT898a54. Stop ended the model waiter while accepted work continued. Desktop/phone confirmation sent one cancellation per original handle, the raw provider settled cancelled, and the actual HTTP transfer disconnected without replay. Setup-only receipts are not counted as model acceptance. Earlier browser/recorder failures remain retained. |
| MCP HTTP overlap and reconnect | `final-7810-http-recovery-1`: five actual Codex/Luna cycles on core7810d5ae/GACTfe1cad75. Each model received a handle, read an independent file in a later step, and queried the same running task. After API loss and the unchanged exclusive lease, recovery retained the full backend identity and original session, with one actual 11,264-byte payload request/hash per cycle and no operation replay. The private Core/API/backend were released. Earlier failed external-search and probe-binding attempts remain retained. |
| MCP input and actual UI answer | `final-eea8-input-five-cycles.json`: five actual Codex/Luna cycles through production CLIO and a labelled real FastMCP reference backend on coreeea8/GACTde58. Handle, later independent work and input-required query preceded real UI answers; original stored results contained those answers. A separate accepted attempt lost the provider WebSocket after partial output and was not replayed or counted. Input recovery across API interruption remains unverified. |
| MCP human input | `final-eea8-input-five-cycles.json` links five successful actual Codex/Luna cycles on coreeea8/GACTde58. Each model received a handle, read an independent file in another model iteration and queried the original input-required task. The real desktop/phone UI supplied the answer; CLIO persisted it and the model collected the original result. This uses a real FastMCP reference input backend with production CLIO, not the production Web backend. A separate accepted attempt lost its provider connection after partial output; its failed receipt is retained, and the accepted operation was not replayed. Input reconnect remains unverified. |
| Shell model overlap | WebSocket evidence includes `integrated-47f-shell-ws-1`; final-state SSE is `integrated-4dc4-shell-lifecycle-1`. Actual model acceptance, subsequent successful independent read, uncancelled running query, expired wait, observe, unbounded wait, stdout, exit 0 and filesystem marker passed. |
| Subagent model overlap | WebSocket evidence includes `integrated-context-final-subagent-1`; final-state SSE is `integrated-4dc4-subagent-mixed-1`. Actual parent-model handle, later independent action, running child, expired wait and actual child output/marker passed. |
| Download model overlap and bytes | `integrated-959e-download-root-1`: the submitting Codex/Luna model received a handle, performed a later checklist action, queried running work, expired a wait without cancellation, observed, waited and retrieved the result. All 6,001 selected files / 6,132,063 bytes and hashes match. This run retains its earlier core959e/GACTd51 identity; the native adapter is unchanged by the later REST response and browser-test repairs. Final-state repetition remains pending. |
| Download continued after Stop | `integrated-959e-download-native-1/stop-accepted-download` and `large-download-custody.json`: UI Stop ended the original model waiter while byte progress continued without cancellation. All 24,001 files / 18,228,063 bytes and hashes completed. Submission was delegated; this is owner/Stop evidence, not submitting-coordinator overlap. |
| Indexing model overlap and manifest | `integrated-4dc4-indexing-refresh-1`: actual Codex/Luna accepted handle, later checklist action, running query, expired wait, observe, unbounded wait and stored result. The atomically published manifest exactly matches 48,098 real entries. |
| Indexing Stop/cancel and manifest custody | Five new-folder cycles in `integrated-4c7c-indexing-root-1/stop-indexing-cold-final-dialog` settled cancelled owners without partial publication. Five final-state public REST refresh cycles in `integrated-4dc4-indexing-refresh-1/stop-indexing-retained-published-manifest` passed Stop independence and one confirmed cancellation each. Its `refresh-manifest-custody.json` verifies the previous pointer, body hash and 48,098-entry manifest remained intact. Refresh submission was client-initiated; the native Connect run supplies the separate model-submission gate. |
| Mixed tasks and honest partial failure | `integrated-4dc4-subagent-mixed-1/mixed-exact-child-assignment`: the root model queried/observed Subagent plus two Shell handles, collected the child, expired an all-wait, and used any/all waits with an invalid member. Actual slow Shell succeeded and the deliberately failing command returned its nonzero outcome without cancelling peers. Earlier model path/delegation probe failures were not counted. |
| Shell Stop/UI cancellation | `integrated-4dc4-shell-lifecycle-1/stop-shell-final-native` passed five cycles. `shell-stop-process-proof.json` verifies PID, birth time and actual PowerShell/launcher/Python chains gone, with no unresolved descendants. Stop left work running; confirmed cancellation settled it. |
| Subagent Shell subtree cancellation | `integrated-4dc4-subagent-mixed-1/cancel-subagent-private-confirmed` and `subtree-process-confirmed.json` passed five actual-model/UI cycles and all owned process-chain checks. The warning identified descendant cancellation; repeated clicks sent one request. These used private workspaces without connected sources. |
| Subagent Download subtree cancellation | `integrated-959e-download-native-1/cancel-subagent-download-fixed-dialog` passed five actual-model/UI cycles. Its `storage-cancel-proof.json` verifies cancelled owners, staging absence and unchanged upstream bytes/hash/source pointers. The separate overlap audit passed only one of five child cycles; it is not five overlap passes. |
| Busy completion delivery | `integrated-4dc4-shell-lifecycle-1/busy-delivery-runtime-injections` passed five rounds: a real background command completed during foreground model work; exactly one runtime tool Part delivered its hidden stdout nonce at the next safe boundary, and the model used it without task controls. A later turn had no duplicate Part or context-frame injection. |
| Idle/next-turn delivery | `integrated-4dc4-shell-lifecycle-1/delivery-shell-final-native` passed five fresh-conversation cycles. Completion alone created no turn/message; the next user turn received one committed delivery, and a later turn received none. |
| Veto retention | `integrated-4dc4-delivery-races-1/delivery-races-veto-final` passed five rounds using an actual trusted production UserPromptSubmit hook: a denied turn retained completion; the next eligible model turn received it once. |
| Collection versus commitment | `integrated-4dc4-delivery-races-1/delivery-races-collection-final` passed five rounds. Actual result collection after staging and before commit removed automatic model injection and stale context-frame text. Later explicit result reads remain permitted and are accounted using actual tool telemetry. |
| Overflow | `integrated-4dc4-delivery-races-1/delivery-overflow-original-handles` passed five rounds of nine actual tasks with bounded 8+1 batches, one delivery per original handle and no later duplicates. Round one resumed the already accepted handles after a probe expected an FS read but the model successfully read the sentinel through foreground Shell. No command was resubmitted; the original probe failure is retained. |
| Native permission handling | `integrated-4dc4-shell-lifecycle-1/permission-verdict.json`: a fresh actual-model Shell request waited for real UI Allow once, then continued under the original invocation/handle and produced its result/marker. This is native permission evidence, not MCP `input_required` acceptance. |
| Real-service desktop/phone UI | Final-state Shell, Subagent and Indexing cancellation/delivery runs used the production UI against the actual source API. Download subtree evidence uses the current unchanged cancellation UI at GACTd51. Captures and hit tests verify readable assignment, visible controls, descendant warning, one confirmation request and pending-cancellation state. The long-assignment scroll region repair is included in GACT5e8. |
| Graceful Shell interruption | `integrated-4dc4-shell-graceful-1` passed four complete model/process/restart/readback/no-replay cycles. The fifth stopped the actual process chain but its replacement API failed Core attachment before model readback. The verdict remains false; four is not five. `orphan-api-retirement.json` verifies the exact remaining source API exited through its authenticated shutdown endpoint and the installed Core identity remained alive. |
| Fresh graceful Shell recovery | `final-e35e4-shell-graceful-2` passed five complete actual-model cycles on coree35e4/GACT898a54. Each model received a running handle, performed a later independent action and queried it running. Authenticated source-service shutdown ended every recorded process chain. A replacement API preserved the original interrupted handle; Luna queried and retrieved it without command replay. The private real-Core launcher exited successfully and released its owned daemon. |

### Focused checks and CI

The shared task-prompt change has 12 focused cases passing individually and
sequentially, with no skips. Six request-boundary cases cover root/child requests
for Codex, Claude and vLLM using the test scripted engine; these are request
composition tests, not live acceptance of those providers. Editable resolution,
missing-snippet errors, absence of unavailable controls, stable orchestrator
composition, declared-child context and existing GitHub guidance also pass.
New helper/test Pyright reports zero errors/warnings. The wider changed-file
check has the same 24 inherited diagnostics before/after, with zero new messages.
Scoped Ruff/format, size/instrumentation/class guards and diff checks pass.
Fresh actual-model prompt/lifecycle repetitions remain required.

Corecced2296 repairs an actual malformed-PNG CI timeout by restricting Pillow to
its PNG decoder. A controlled test reproduced the same timeout before correction;
five relevant cases pass individually/sequentially with zero skips and unchanged
2-second/15-second budgets. Scoped Ruff/format/Pyright0/0 and guards pass. Its
three Docker jobs subsequently failed during BuildKit token-service bootstrap,
before project builds; this is not source-build acceptance. Retained logs identify
the exact CI heads and infrastructure failures.

`final-9bd4-shell-queued-failure-2` passed five actual Codex/Luna cycles on
core9bd4/GACTb610: 120-second background Python work, independent later model
action, 122,880 real bytes and matching SHA256 per cycle, idle completion,
single next-turn result delivery and no later duplicate. Python exited 7;
the outer PowerShell process exited 1. Both outcomes are recorded distinctly.
The first Subagent controls mission targeted its descendant Shell after
pagination and failed the strict original-Subagent assertion. The corrected
mission passed two cycles on corecced/GACTb610 before a third spawn was refused
for explicit empty placement; no handle was accepted in that third cycle.
Neither incomplete series is counted as five passes. Fresh missions specify
local placement, preserve the assertions and use the shared lifecycle prompt.

On coreeea8/GACTde58, current CI exposed two additional failures. The provider
SDK check-mode test paired a recorded release feed with moving repository pins,
so its expected upgrade vanished. The corrected test pins both outdated and
current feed floors, verifies exact change/no-change output and proves check
mode leaves the project untouched. Both cases passed individually/sequentially;
scoped Ruff/format/Pyright passed. No dependency or production SDK behavior changed.

GACT's 3,023 unit cases and production build passed before one transcript browser
case failed. The same case reproduced locally. Saved history could move a live
message to another index, and layout scrolls could replace the reader's explicit
anchor. The repair retains that anchor and resolves it using the message ID.
The new focused case reproduced both causes before repair; four viewport cases
passed individually/sequentially. The unchanged browser scenario passed five
consecutive desktop/phone runs, with scoped lint/format, six guards and full
TypeScript/online/offline builds passing. GACTfe1cad75 contains the repair.
Evidence: `sdk-bump-ci-*`, `transcript-anchor-*`, `transcript-ci-*` and original
exact-head CI logs. New-head CI and affected live UI gates remain pending.

On core7810/GACTfe1, both workspace runs passed 3,024 unit cases and builds,
then exposed an additional unchanged resize-browser regression. It reproduced
locally. Native compensation could replace the stable reading anchor before
the viewport width observer ran; after restoration, first measurement of the
partially visible anchor itself could move its top again. The repair retains
the capture width, refreshes ordinary navigation anchors as a gesture settles,
and compensates measured rows before the anchored row using its current index.
Three new focused cases reproduce these causes before correction. Eight affected
viewport cases passed individually/sequentially, and the unchanged virtualized
resize, in-flow resize and desktop/phone disclosure contracts each passed five
times (15 executed / 15 passed). Scoped lint/format and six guards passed.
Full TypeScript/online/offline builds passed. GACTb61006a7 contains this repair.
Evidence: `transcript-compensation-*` and `transcript-anchor-width-before.log`.
Earlier candidate failures remain retained, including one Chromium
`ERR_NETWORK_CHANGED` that prevented initial JavaScript loading; none are
counted as passing qualification.

Core7810's Python3.12 shard0 exceeded the unchanged 18-minute CI budget without
a recorded failing test; its dependent coverage job lacked that shard artifact.
A separate BuildKit image-bootstrap failure passed one same-job rerun before
any project/workflow/authentication changes. Current CI is not green; replacement
head CI and affected actual-model gates must still pass. No assertion, execution
budget, worker count or guard was weakened.

Focused unit/integration cases were run individually and sequentially with one
worker. They cover durable acceptance, complete identity and collisions, caller
authorization, legacy records, filters/cursors, mixed controls, task input,
submission/Stop/cancellation races, late descendant admission, owner cleanup,
delivery/collection/veto/overflow, reconnect, storage custody and process trees.
Evidence includes the recorded `compat-*`, `core-ci-repair-*`,
`codex-parallel-*`, context-commit and storage-HTTP-ack results. Scoped Ruff,
formatting, Pyright and existing guards passed. UI cases used Node 1 GiB; type
checks/builds used 2 GiB. Guards only tightened; assertions, timeouts, worker
counts and output budgets were not weakened.

On the runtime core4dc/GACT5e8 heads, all required workflow runs completed:
core 23 successful checks plus one conditional Docker restore-job skip; GACT
13 successful checks plus seven conditional event/release-job skips. Core's six
Python shards total 11,398 passed / 66 skipped per interpreter, both coverage
reports show 86%, and all three OS document-smoke jobs passed. Flake hunt is
153 passed / 1 skipped. Existing executed-test exclusions are **not acceptance
passes**. GACT's workspace/browser runs and Linux/Windows debug builds passed;
native WebView's chat-shell endpoint case passed but its permission-card
subtest was skipped under `TAURI_E2E_CHAT_ONLY=1`. This does not establish full
native acceptance. Exact snapshots/logs are retained in `ci-*-20261009T034915Z`
and the matching core/UI/document logs.

### Blocking and unverified gates

- The historical fifth graceful interruption readback failed with
  `clio_core_client_attach_failed`. The native client could not create its
  per-process shared memory (`shm_open`: `Resource temporarily unavailable`).
  The existing machine-wide installed Core daemon adopted its own configuration;
  it was not restarted or reconfigured. The root cause is not established by
  this error alone. On October 9 the existing isolated real-Core launcher
  successfully recovered a permitted source runtime without restarting the
  installed Core or retired services. A later original-task model readback
  actually executed, but its source-freeze guard failed during concurrent
  integration and its false verdict remains retained. Five fresh graceful
  recovery cycles subsequently passed, followed by five abrupt API-crash cycles
  in `final-eea8-shell-crash-1`. Actual Windows process ownership stopped each
  accepted command's descendants; restart preserved the original interrupted
  handle, and the real model collected it without command replay. No accepted operation may
  be replayed when resuming qualification.
- Five MCP Stop/UI-cancel cycles and five HTTP reconnect cycles passed, retaining
  their exact source identities above. Nonrecoverable backend-loss and input
  recovery qualification remain pending. Prepared external probes are not
  executed evidence. The input backend is explicitly a real FastMCP reference;
  the tested production Web backend exposes no input-required operation.
- Human integration introduced a compact Work inventory that omitted shared
  task controls. Actual service/browser probes exposed it; those failures
  remain retained. The inventory now reuses the shared task component for
  kind, assignment, status, progress and confirmed cancellation. Focused MCP
  case reproduced the missing controls before the repair. MCP and Subagent
  cases qualify confirmation, Keep running, pending progress and duplicate-request
  suppression after it. Five actual-model MCP Stop/cancel repetitions passed
  through the repaired compact inventory at desktop and phone widths.
- The connected-source Download-plus-Shell subtree route was rejected by the
  actual host fence in `integrated-context-final-subagent-1`: it cannot enforce
  connected-source child-process exclusions. The already accepted storage work
  was cancelled through its owner. Fresh UAC/fence setup is prohibited; never
  retry or bypass the rejected combined route. Separately qualified native
  storage and private-workspace Shell routes do not qualify this route.
- The retained long-conversation SSE run hit the unchanged 65,536-byte parser
  limit (observed line 65,700 bytes in the read-only provider dependency).
  Fresh-conversation success does not repair or qualify that route. Do not
  increase/bypass the limit or alter the dependency in place.
- Other distinct provider/native-client, relay, POSIX and platform execution
  routes have not passed the complete live matrix. Installed native-client
  launches and retired-service restarts remain prohibited. Final-state Download
  overlap/recovery repetitions and storage interruption custody remain open.
  Unit tests and older-state runs cannot silently close these gates.

These limits prevent claiming implementation completion under the requested
full qualification gate. Both PRs remain drafts. Preserve every rejected-launch,
installed-runtime, private-data and manual-only cleanup boundary. Source task
services/controllers are retired; unrelated active human work is preserved.
Concurrent human integration changed the primary and feature source heads;
each receipt records the actual identity it tested. This task does not promote
the async drafts or alter installed Desktop, authentication, defaults or released
pins. Unrelated human changes are preserved. Disk/manual cleanup remains a
separate incomplete monitor obligation.
