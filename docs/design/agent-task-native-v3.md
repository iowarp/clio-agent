# V3 — Rebuild CLIO task integration around native execution and durable agent context

## 1. Direction and public contract

Replace the accumulated task integration, preserving requirements and useful workload algorithms rather than its module graph. All requested task, subagent, session, peer and workflow capabilities remain in scope. Use codex/agent-facing-async-tasks in core and GACT; record the GACT commit in core. Preserve unrelated work, installed Desktop, authentication, defaults and released pins.

Use two execution families:
- FastMCP Tasks owns ordinary Shell, Download and Indexing execution. External MCP servers own their tasks.
- CLIO's conversation engine owns agent assignments, model execution and lineage. Root and child agents share that implementation. Preserve the existing relay transport's MCP task identity/idempotency and message route as an agent-placement adapter; it may carry an agent assignment but does not create a second CLIO execution lifecycle.

Register product built-ins through FastMCP. Short control tools delegate to the existing owner; they are not themselves background tasks. Built-in task calls use the owning native runtime root. Preserve capability-based direct routing for declared MCP servers so task-capable servers are not sent through a Tasks-suppressing ProxyClient; do not blanket-toggle unrelated client capabilities. Register task tools before their owning server starts. Future Labios support attaches at this MCP boundary, without a speculative scheduler.

Preserve the shared tools:
- query_tasks(kind=None, status=None, handle=None, tool=None, agent=None, active_only=False, cursor=None, limit=50, scope="tree")
- observe_tasks(tasks, cursor=None, pattern=None, timeout_s=None, notify=None)
- wait_tasks(tasks, return_when="all", timeout_s=None)
- cancel_tasks(tasks)
- get_task_result(handle)

Accept one handle or a list. Kinds are Subagent, Session, MCP, Download, Indexing, Shell and Question; transport/placement are separate fields. Queries expose assignment, display label, owner, raw backend status, effective status, activity, progress, timestamps, supported actions and result reference. Preserve the original subagent assignment before launch enrichment. Optional CLIO task-description metadata is removed before backend arguments; derive safe descriptions otherwise and identify missing legacy descriptions.

The user-approved Question extension is an implementation and qualification gate.
ask_user always posts through the existing question owner and returns a durable
handle immediately. It is a short FastMCP control operation, with no execution
worker or separate scheduler. The question owner records full history in Core;
its live UI dictionary is a rebuildable projection. Shared query/observe/wait,
result collection and cancellation use that owner through the same task surface.
wait_tasks(question_handle) explicitly waits for an answer, unbounded by default.
An answer already recorded returns immediately. Stop cancels the waiter and
leaves the question open; cancel_tasks explicitly dismisses the question.
Answers use the recipient-input completion boundary instead of the composer FIFO,
so they can enter the next iteration or wake an idle session without duplicate
automatic delivery. Permission and external MCP input gates retain their own
authorization requirements. Qualify mixed Question/Shell/Subagent waits, busy
and idle answers, Stop independence, recovery and attributed human controls with
actual models before claiming this extension complete.

Authorize before lookup disclosure, filtering, counts and pagination. Default scope is the caller's tree. scope="authorized" permits peer/root discovery only through trusted owner grants; model-supplied intent is not authority. Reuse existing permission policy and explicitly represent missing coordination grants as Core relationships (granting human, recipient session/assignment, target scope, read/observe/message/control rights); only authenticated human actions or trusted configured policy create grants. Reading/messaging does not grant cancellation. Memory session discovery returns stable session addresses and task links subject to the same permissions.

Query and observe do not consume completion. Mixed waits return individual outcomes and `unfinished` members, each with its recorded status and activity; one failure never cancels other members. Waits are unbounded by default; zero is a snapshot and positive expiry ends only the waiter. Reuse native result driving instead of adding another polling implementation. notify=True subscribes the caller to the exact handle, False removes that subscription, and omission leaves it unchanged. Owner notifications are automatic; peer collection never consumes them.

## 2. Execution, context and recovery

Persist a submission with its opaque public handle, immutable owner/assignment identity, invocation, workspace, permissions, confinement, attribution, tool arguments and explicit execution limits before invoking the backend. Public acceptance follows native acceptance and confirmed persistence of the full backend identity. A confirmed pre-acceptance failure is a submission error. Failure to record identity after native acceptance is accepted-but-recording-unconfirmed; loss of the submission reply is acceptance-uncertain. Return the existing public handle and the distinction, never a clean retryable submission error. Preserve this recovery evidence and never blindly submit again.

For CLIO-owned task tools, carry an opaque submission reference explicitly into the native queued invocation, hidden from the model-facing schema. One shared request boundary validates public arguments, strips/rejects model-supplied internal fields and injects its host-owned reference. Store the reference with the exact tool and argument digest in Core. The actual registered worker callable resolves this trusted context and binds it to its full native task identity under the application-owned writer's serialization before effects; reject a different task using that reference, while recording same-task redelivery as another attempt. In-process authorization comes from this host-owned binding, not an assumed MCP access token. Do not depend on inherited ContextVars or request middleware executing inside Docket. Missing, mismatched or unrecoverable context fails before workload effects. Revalidate current revocations and descendant cancellation admission when execution starts.

Shell retains background=false by default. Both execution modes use the same native workload implementation and retain its native handle internally:
- background=true returns the public handle immediately; Stop or cancelled waits leave it running.
- background=false awaits the result; turn Stop explicitly cancels that native task and awaits its process-tree cleanup. Never rely on transparent call_tool cancellation.
- If Stop races with submission, finish recording acceptance. Apply the appropriate foreground cancellation or background continuation rule without resubmission.

GACT application lifespan owns the native server roots, clients and result observers on its application event loop. They do not live on resident SyncMCPToolExecutor threads and are excluded from that fleet's idle reaper. Provider bridges submit onto this application loop. Remove inherited whole-call deadlines after acceptance; retain submission/RPC health limits and explicit workload execution limits. Their responsibilities are transport retention, observation, input routing and confirmed recording, not workload scheduling. Persist outcomes before creating completion intent. Observers use native driving/status calls to refresh active metadata well within its TTL and promptly store terminal output in Core. Native expiry/not-found means uncertain or expired backend visibility, not proof of failed execution. Known CLIO-owned cancellation may use the authenticated original Docket execution identity if protocol metadata expired; external owners report uncertainty. Native polling creates no extra agent tool calls.

Use embedded memory:// by default. Expose native backend and worker settings through existing configuration; document that local execution does not survive process loss. Shell and a new connected-storage FastMCP server each have an independent native runtime root/lifespan, Docket queue identity and worker pool (configurable concurrency, default 10 each), managed on the application event loop. The shared catalog supplies names, but task calls and controls route through native SDK clients to their owning runtime root; mounting beneath one worker-owning gateway is not pool isolation. These remain MCP calls, never direct Python dispatch. Expose queued versus actually started execution in both background task rows and foreground invocation progress. Do not implement CLIO scheduling or per-tool queues.

Recovery promises honest uncertainty, not exactly-once effects:
- CLIO does not automatically resubmit uncertain accepted operations when an agent or service restarts.
- Native backend recovery/redelivery can repeat or overlap execution after lease loss; report observed attempts and uncertainty honestly. Keep blocking work off the worker event loop. We do not promise globally exclusive execution. A configured persistent backend does not strengthen this guarantee.
- Resume the agent with its assignment, fixed context checkpoint, handles, last confirmed observations and an explicit recovery notice. It can inspect effects, collect results, reconnect existing work, retry unchanged work or submit a revised operation.
- An explicit resubmission gets a new linked handle; old outcomes remain historical. Do not add an exactly-once engine or side-effect rollback promise.
- Reattach surviving external transfers/jobs only using their verified original identity. Never infer custody from a PID alone.
- On shutdown, stop admission, explicitly cancel/withdraw owned active and queued local native executions before leaving their FastMCP lifespans, show pending settlements and record interruption or uncertainty before closing Core. An operator or the implementing agent supervising its own verified qualification processes may force-close unresolved shutdown with recorded uncertainty (or recording unconfirmed during Core outage). Agent-owned qualification processes and disposable data require no further cleanup permission. No hidden automatic cutoff is introduced. Do not mark external surviving work interrupted merely because the local observer stopped. No hidden execution or settlement deadline.

Workload requirements:
- Qualification resource ownership includes the complete descendant process tree and disposable data generated by those processes, even after the original launcher exits. Track and verify creation identities and ancestry; the implementing agent may stop/force-close those owned descendants and remove their disposable data without another permission request.
- Shell keeps confinement, output bounds/references, incremental output, explicit execution limits and process-tree cleanup.
- Downloads reuse approved providers, selected paths/revisions, byte/hash accounting and cleanup-before-terminal. connected_data_download submits the native task; remove the separate transfer scheduler.
- Connect indexing runs as a native task; enumerate off the application loop with progress/cooperative cancellation, deduplicate equivalent active requests within the owning session (provider, revision, paths, indexing options, workspace and permission scope) and atomically publish the complete manifest. Check/reserve deduplication under existing submission serialization before creating another native submission; return the existing handle with reused=true rather than enqueueing a second task or minting a second submission reference. Failure/cancellation/interruption preserves the previous manifest.
- Obtain CLIO-owned workload approvals before execution. Native input_required on external tasks reaches the existing question surface even when the agent is idle, with persisted request/answer identity. Input-answer re-entry and reconnect must not cause CLIO to replay submission.

Preserve cancellation request separately from raw terminal status and physical settlement. Use the shared owner start/cancel admission boundary to distinguish confirmed never started, started with cleanup confirmed, and settlement unknown. A queued cancellation can settle without running a body; prove withdrawal/non-admission rather than awaiting a finally that will never execute. A started invocation's shared server wrapper writes a cleanup receipt after required cleanup. Start observations and receipts identify their execution attempt; an old receipt cannot settle a redelivered attempt. Controls/UI show Cancellation requested while actual settlement remains pending, and expose uncertainty honestly. For external servers, never claim process termination from a cancel acknowledgement alone.

## 3. Assignments, Core history and delivery

A session is a stable address, history and mailbox. An assignment/run is finite delegated or human-started work with one task handle. A model turn is one execution segment; an iteration is one model call and its tool observations. Existing standing watcher assignments retain their handle across check segments, including a failed check; their owner records waiting/error activity, and explicit disarm ends the assignment. They do not publish a completed assignment after each check or wake their parent as a side effect of checking it.

Ordinary yields, task-result wakes, steering, pause/resume and recovery continue the same active assignment. Do not complete its handle merely because the model turn ended. Extend the existing finish/submit boundary with a separate orchestration argument assignment_disposition="yield"|"complete"; it is not a signature output. A direct response without a disposition yields while owned tasks, declared dependencies or input remain pending; otherwise it completes. Explicit completion may leave accepted background work running, and this is recorded. Prompts explain this choice, and the parent/UI can see yielded activity and its pending dependencies. A later message/result after actual completion creates a linked new assignment in the same session and wakes it automatically under the ordinary pause/input/veto gates, as explicitly required; it does not reopen the completed handle or re-enable a stopped goal/loop. Explicit restart also creates a new linked assignment/handle. Subscription and result handles stay pinned to their original assignment; messaging aliases may resolve to the stable session, never silently retarget historical cancellation/results. scope="tree" follows the stable owning session and its delegated descendants, including predecessor assignments. Linked successors in that session retain read/collect/observe/control rights over predecessor-owned work, subject to current revocations; they do not acquire unrelated peer rights. Cancelling a successor includes still-active inherited background work and its delegated descendants, without changing the historical ownership or outcome of old handles.

Pause holds model execution and preserves context; accepted background tasks continue and accumulate results. Resume releases the hold. Cancel applies to every task kind; agent-assignment cancellation closes descendant admission and cancels its task subtree, settling after required cleanup. Restart of an active assignment follows explicit cancellation/settlement; an already terminal assignment can restart directly. Uncertain settlement returns that uncertainty instead of fabricating completion or silently launching a successor.

Keep complete messages, observations, assignments, subscriptions, interventions, lineage and delivery history in Core's existing hierarchy and confirmed writer path. Session metadata contains bounded rebuildable projections, not whole retained histories. There is no new canonical database, execution registry or copied transcript. Restart references a fixed Core history/checkpoint boundary and later accepted-operation observations.

Every stable session owns one Core-backed mailbox. Human feedback, peer messages, attributed runtime notices and task-result references share its recipient-input boundary. A task handle addresses a finite assignment or operation; its notifications target the owning session mailbox, which survives linked assignments and restarts. A workflow groups sessions and grants rather than introducing another delivery queue. Resident projections are rebuildable Core views, not another message inbox. An application-loop signal carries only the session address and requests a fresh Core pending-input check; losing that signal cannot erase accepted content. Startup and busy-to-idle transitions recheck Core. Distributed transports can later deliver the same identities and attributed content through this acceptance boundary without changing acknowledgement or authorization.

Accepted input keeps its original assignment binding. Replay verifies content identity, rechecks admission, and reconciles question answers and parent notices from the confirmed input. It neither dispatches a replacement operation nor retargets a successor. Only recipient-input persistence acknowledges delivery; inspecting or staging a message does not consume it.

Use one authoritative recipient-input commit record in the existing context lane, containing assignment/iteration identity, fixed context boundary and included notification IDs/content references. Write referenced content first, then this confirmed commit; acknowledgements derive from it. put_many is not a transaction. Before the commit, interrupted staging stays pending; after it, recovery reconstructs that input, including the crash window before the model call. Explicit tool-result collection uses the same record after its observation is stored. Delivery is recoverable with stable IDs, not exactly-once external model execution.

Use one application-owned Core writer for these lifecycle commits. Normally persist before projections, terminal waiters and notifications. During a Core outage, confirmed control effects and cleanup observations may update provisional UI/control outcomes and local waiters as recording unconfirmed; this exception includes cleanup receipts, not only parent notices. It never acknowledges durable delivery or promises recovery after an application crash. Native execution remains native; Core records observations and delivery evidence, not a competing task state machine. Create subscriber notifications from durable terminal observations and indexed recipient relationships, without per-subscription workers or whole-session scans.

Keep all mailbox history. gact.mailbox.max_pending_messages defaults to 0/unset, meaning unlimited. A positive setting counts only undelivered messages, including staged messages until input commit. Apply retryable backpressure to new peer-message admission; already-accepted results, ordinary human feedback and human-control records remain durable and cannot be blocked or dropped by that limit. Resident/context batch bounds are not retention limits.

Human steering/pause/resume/cancel/restart records intent in the controlled owner, independent of mailbox capacity, then applies control and derives an attributed parent notice. If Core is unavailable or intent persistence fails, trusted cancellation/pausing of known active work still proceeds through its existing owner using verified identity; report recording as unconfirmed and reconcile its notice when Core returns. Retain pending evidence in application memory only, without another canonical store; a crash during that outage cannot promise durable notice recovery. Reject new durable submissions and do not acknowledge undurable messages as accepted. Parent notices enter the same next-iteration injection path. Repeated controls use their request identity without issuing duplicate effects.

## 4. One admission path, UI and replacement map

Consolidate reservation/admission into the existing conversation queue/TurnRunner under the same session serialization as busy-to-idle transitions; the present spawn() is not already an admission gate. Use this one final decision and one per-iteration durable injection boundary. Include user feedback, composer-queue promotion/autostart suspension, task completions, peer messages, goal continuation, loops/schedules, deferred hooks, A2UI actions, spotter requests, lifecycle resume, elicitation/blocking-question answers, plan-exit approval/revision and blueprint revision gates. Replace the composed idle-hook chain's competing admission decisions.

- Busy: inject eligible feedback/results before the next model call; never start a parallel turn.
- Explicitly paused: retain until explicit resume.
- Waiting for human input: preserve the input gate; the matching answer releases it.
- Idle, including after turn Stop or explicit assignment completion: eligible messages/results wake the agent automatically.
- Goal dependency waits retain partial results until the declared any/all condition; human feedback still arrives.
- Goals, schedules and loops retain their independent enabled/disabled policies. A task wake does not reactivate a disabled control or resume a separately paused future-message queue.

Serialize pending-item checks with busy-to-idle transitions. An arrival during shutdown of a turn must either enter its next iteration or trigger subsequent admission. Temporary busy/admission failures recheck on the corresponding state change. Policy vetoes remain visibly pending until that policy changes or explicit human action; do not spin or permanently consume an attempted batch. After application recovery, rebuild pending delivery from Core. Algorithm wakes remain attributed system injections in the owning session.

Extend the Work information surface, Observability and Runs with compact clickable rows: label, kind, status and useful progress first; details/output/history progressively disclosed. Cancellation has an identifying confirmation and descendant warning for agent assignments. Show pending cancellation, disable duplicate requests, and keep dismissal separate. UI and agent tools use the same operations.

Remove, rather than wrap:
- task_shell_owner execution scheduling and storage transfer/indexing schedulers/task_adapter polling;
- custom generic TaskSupervisor execution drivers for ordinary native tasks;
- task_completion_wake policy, mailbox wake policy and attempted-batch maps;
- independent child-forward terminal writes;
- subagent custody/restart/continuation/history reconstruction modules as their behavior moves into the shared conversation lifecycle and Core references;
- mcp_wait_ladder, duplicate completion-consumption guards and loop_inbox's lossy deque, including its payload and hint caches.

Each stable session has one Core-backed mailbox for accepted human feedback,
peer messages, runtime notices and task outcomes. Task outcomes reference their
existing durable result; they do not copy it into another execution registry.
Assignment and task handles identify the work within a session, rather than
creating separate inboxes. Authorization and recipient-specific input commits
apply equally to local and remote producers. Transient TurnRunner signals only
request a Core mailbox check; they carry no message, task result or delivery
state. Lost signals recover from the Core recipient index on startup/readiness.
Restarted assignments keep that stable mailbox address. Human cancellation acts
through the verified execution owner immediately, with its attributed parent
notice entering the same mailbox; it cannot depend on the model reading mail.
Distributed agent hosting additionally needs cross-host turn reservation and
wake transport. The shared mailbox establishes their addressing and durable
input boundary; this local implementation does not qualify distributed hosting.

Retain workload algorithms, process cleanup, native capability/backend identity routing and shipped compatibility aliases. Aliases only delegate. Preserve released completed-child messaging through stable session aliases and linked assignments; historical task results remain unchanged. Migrate records idempotently, including missing descriptions. Drain/settle old owners before cutover. No permanent old-engine fallback. When Core is unavailable, reject new durable background admission explicitly rather than introducing another store or silently calling it durable.

## 5. Construction and completion gates

Build on one integrated feature branch in this order; all capabilities remain required:
1. One complete native Shell slice: trusted worker context, immediate/background versus foreground behavior, acceptance races, physical cancellation, uncertain recovery and durable delivery.
2. Native MCP, Download and Indexing through that proven integration.
3. Shared root/child assignments, lifecycle, Core mailbox/subscriptions and session/workflow coordination.
4. Unified admission with current autonomy behavior, integrated UI and canonical prompts/docs.
5. Final integrated qualification and deletion/cutover checks.

Focused tests run individually/sequentially with one worker. Cover persistence-before-ack, invalid/uncertain submission, authorization/collisions, legacy migration, filters/cursors, mixed controls, input re-entry, queued versus started cancellation, stale attempt receipts, cancellation during Core failure, Stop independence, delivery/collection races, crash before/after input commit, busy-to-idle arrival, veto recovery, overflow/backpressure, more than 1,000 messages and complete retained history. Fill Shell's native worker capacity and prove storage still starts on its independent runtime root.

Real-model gates use production implementations, Codex/Luna by default and Claude where the bridge differs:
- Every kind returns a handle while running and the model performs other work.
- Query/observe, any/all waits, expired waits with continuing work, partial failures, idle completion and exactly-once receipt under ordinary operation.
- Child yield/download/wake/analysis retains one assignment handle; explicit completion and restart follow their distinct rules.
- Stop versus pause versus cancellation; UI human controls notify the parent; actual descendant/process settlement.
- Independent sessions and distinct workflows exercise discovery, messaging, subscriptions, observation/waits and permission denial.
- Real bytes/hashes, indexing manifests/counts, shell output and child output.
- Worker loss after a visible side effect but before result commit; native backend recovery/redelivery uncertainty, agent inspection and explicit revised retry.
- Input/permission handling, transport reconnect and isolated service interruption. Recoverable external jobs remain distinct from interrupted local work.
- Real-service desktop and phone UI review; repeat affected lifecycle race scenarios five times on the final candidate.

Run scoped formatting/lint/types/guards and required CI without weakening assertions/budgets. Every unavailable, failed or skipped required gate remains incomplete. Old evidence supports regression design but is not acceptance of the replacement.

Update canonical prompts for every capable root/child agent, task-capable tool parameter descriptions, and website walkthroughs with real screenshots. Explain yield/completion, automatic idle wakes, foreground/background cancellation and uncertain recovery.

Record processes, ports, generated data and cleanup ownership before qualification launches; settle and remove disposable state after each family. Preserve canonical product history and existing prohibited/manual-only boundaries. Merge, release and installation remain separate actions.
