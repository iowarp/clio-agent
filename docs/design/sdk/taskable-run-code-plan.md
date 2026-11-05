# Taskable `run_code`: implementation plan

`run_code` exists only as a blocking call. The model waits on the script, the turn stays busy, the script holds its scope's kernel, and `code.call_timeout_s` (600 s) stops it. That is right for a short script and wrong for the work CLIO exists for: watching a multi-hour simulation, a Slurm job, a parameter sweep with many scripts in flight, a 3D printer feeding a dashboard (clio-agent #1908). This plan makes `run_code` launchable as a task. A background script is owned work on the task layer with a handle, its own kernel and the task's lifetime, so many can run at once for hours. It can report progress and wake its owner without ending.

The plan also gives a running script the two ways to keep a dashboard live, and teaches the model to compose them: values through the surface's data model, and images through a frame stream shown by a generic A2UI component.

The work runs to completion without stopping for review. The human reviews the finished branches.

## Contract

This plan is the contract for taskable `run_code` and for frame streams. The TD decisions of `tool-calling-plan.md`, the BR decisions of the browser plan and its addenda, D1 to D29 in `acceptance-ledger.md`, and the F, M and L decisions stay in force except where this plan changes them. When code and this plan disagree, change the code. If the plan turns out wrong, choose the fix that best keeps its intent, update this document in the same change, and record the decision in the ledger.

| Document | Governs |
| --- | --- |
| This plan | Background `run_code`: launch, kernels per task, lifetime, approval, reporting, presentation, live surfaces, frame streams and the stream component, how the model learns them |
| `docs/design/sdk/tool-calling-ledger.md` | This plan's decisions (prefix `CT`) and scenarios (prefix `CK`), in their own section |
| `docs/design/sdk/tool-calling-plan.md` | Synchronous `run_code`, the kernel, dispatch, stubs, `callers`, grants |
| `orchestration-semantics.md` and the agent-task-native design | Owned work, handles, the mailbox, cancellation, wake rules |
| Browser plan, BR decisions | The kernel's binding registry, which this plan adds an entry to; the live view, whose frame path moves onto the stream service |
| `scientific-visualization-plan.md` | The view contract the stream component implements |
| gact-tui `CLAUDE.md` | UI rules, including that status is never encoded only as an icon |
| clio-schemas | The A2UI catalog and wire schemas: the `frame-stream` component and the `stream://` scheme start there; clio-agent pins it (`clio-schemas==` in `pyproject.toml`) and gact-tui generates its types from it (`scripts/generate_clio_schema_zod.mjs`) |

**Base.** `claude/tool-calling-long-horizon` (read at `cd9e8df9`, not yet merged into `develop`). Build on its head and rebase onto `develop` when it merges. This base has no kernel binding registry. The `task` binding needs one, and the stream service is extracted from the browser's live view, both on the browser branch (`claude/gracious-goldberg-gagoo4`). If the browser branch has merged, build on it. If not, add the registry exactly as the browser plan's Phase 3 describes it and build the stream service new, and the browser branch moves its live view onto it when it rebases; there is one registry and one frame path, not two.

`CT` and `CK` collide with no other prefix (checked 2026-11-05 across clio-agent, gact-tui, clio-schemas and the marketplace). The scenarios were first numbered `K`, which the task-replacement qualification already uses (`K01` to `K06`).

## Rules

The megachange rules hold: delete and replace, the task layer is the only execution authority, the model decides, no silent fallback, one mechanism per concept, no fifth store, agent-facing caps come from the existing result room rather than fixed numbers, evidence over claims. The PTC rules hold, in particular one execution path for tools. Six more apply:

1. **One tool, two modes.** Background is a mode of `run_code`, not a second code tool. The script language, the stubs, dispatch, the fence and the grant are the same in both.
2. **A background script is owned work.** It is admitted, observed, cancelled and settled through `gact/native_work.py` and the common Core history, like `connected_data_index`. Nothing in `gact/code_mode/` keeps its own registry of running scripts.
3. **No model in the loop to keep it alive.** A background script runs without turns. The model is involved only when the script ends or when the script itself wakes it.
4. **Synchronous behaviour changes only by losing its limits.** A call without `background` runs in the scope's kernel and blocks its turn as today. `code.max_nested_calls` and the kernel discard at `code.call_timeout_s` are removed (rule 6); Stop cancels a synchronous script as it cancels any call in the turn.
5. **One frame path.** Every stream of images into the UI, whatever produces it, goes through the stream service. No component opens its own socket to a producer.
6. **The person stops work, not a limit.** CLIO is a human-observed agent, so no count, rate, wall-time or memory limit ends a script, synchronous or background, and no timeout ends work that is waiting on a person: a question waits until it is answered or its work is cancelled, whether it was asked overnight or before lunch. A timeout that stays anywhere in this work has its reason recorded in the ledger. A script ends when it finishes or when the agent or the person cancels it. The plan's job is to make a running script obvious and easy to stop: its state, elapsed time, nested-call count, last progress, memory use and a cancel control, wherever it is shown.

## Out of scope

Do not implement, stub or reserve fields for these:

- Resuming a background script after a server restart. A kernel's state does not survive one; the task settles as lost.
- Moving a background script to another machine, or running one through clio-relay. That belongs to the relay and task rework.
- Promoting a running synchronous script to background.
- Changes to `set_loop`, `loop_wakeup` or the person's `/loop` command. Whether background scripts make the agent's loop tools redundant is a separate decision, taken after this lands.
- A dashboard producer API beyond the existing A2UI tools and the stream binding.
- Refetching a `dataUri` artifact when its file changes. The data model is the one live channel for values.
- Frames through the data model or as one artifact per frame. Frames go through a stream.
- Audio, video codecs (H.264, VP9) and WebRTC. A stream carries still images; a producer that has video decodes it to frames.
- The trame remote view as a stream source. It joins the stream service in the remote visualization work.

## Current state

Read on `claude/tool-calling-long-horizon` at `cd9e8df9` and the browser branch:

- **`run_code` is synchronous only.** `gact/code_mode/tool.py` takes the scope's kernel from `kernels.kernel_for(session_id, scope, ...)`, holds `kernel.lock` for the whole script, and discards the kernel when the script passes `code.call_timeout_s` or is cancelled. `kernels.py` keys kernels by `(session_id, scope)` with `lock = threading.Lock()  # one script at a time per kernel`, and its idle reaper skips a kernel whose lock is held. Nothing in `gact/code_mode/` uses `TASK_CALL` or a `TaskConfig`.
- **Scripts have limits today.** TD17 caps a `run_code` call at 500 nested calls (`code.max_nested_calls`), and `code.call_timeout_s` (600 s) discards the kernel of a script that runs longer. Each script's nested calls run in its own dispatcher, with its own thread pool (`tools.call_workers`, 32) and its own per-server gates (`code_mode/dispatch.py:117-120`); each model step gates its own calls separately (`agents/tool_concurrency.py:55-100`). Nested calls therefore never share a pool with the agent's calls, and nothing bounds the calls to one server across scripts.
- **The kernel bridge is text.** `kernel_main.py` speaks newline-delimited JSON on a duplicated descriptor. It has no binary message.
- **Native task tools exist.** `gact/native_storage.py` declares `connected_data_index` with `task=TaskConfig(mode="required")` and runs its body through `NativeWork.run(_clio_submission, tool, arguments, body)`. `SubmissionContext` carries the permissions, confinement, attribution and `execution_limits` captured at submission, and `NativeWork` has a `runtime_loss_observer`. Stop leaves accepted indexing running.
- **The owner can be woken.** `send_agent_message` delivers through `conversations.mail.peer`: a busy recipient receives it at its next iteration and an eligible idle one wakes. Progress, by contrast, "creates no mailbox wake" (`gact/conversations.py`). Settled task results reach the next turn under `## Background task results` (`gact/enrichment.py`, at most 8 blocks per turn).
- **Permission questions belong to a turn.** A question is an in-memory row keyed by session (`permission_gate._ask_person`, `permission_gate.py:475-558`), answered through `POST /v1/permissions/{pid}`. Three things tie it to a turn: the session is taken from the active tool session and falls back to `sessions.most_recent()` when there is none (`runtime/globals.py:131-139`); the cancel token is the owner session's current turn's (`cancel_events`, so Stop on an unrelated turn would deny it); and every question expires after 600 s with a typed deny (`permission_timeout.py:31`). The one turn-independent, durable question owner is Core's `Question` task kind (`gact/questions.py:38-127`, used by `hook_defer`).
- **Mail comes from sessions.** `MailItem` has only `sender_session` and `sender_human` (`arc/task_types.py:345-363`); `peer` always records the calling session (`session_mail.py:513-521`); content is text only (`:472`), and a peer message renders as "Agent session <sid>". An idle recipient wakes through `listen_mail` → `TurnRunner.signal` (`conversations.py:104-108`, `turn_runner.py:89-137`).
- **The task handle is not in the call context.** `TASK_CALL.submission_handle` is declared and never set (`tools/task_call_context.py:79`); `NativeWork` passes the handle to its body as an argument and sets only the session, workspace, write root, read-only mode and result room (`native_work.py:122-142`).
- **Restart.** A clean shutdown cancels running native work (`native_task_settlement.request_shutdown`, `:420-502`); only a crashed or killed process reaches `runtime_loss_observer`, through `native_task_recovery.restore` (`:97-145`).
- **Children.** Nothing cancels a child session's owned work when the child settles; owned work outliving explicit completion is the task-replacement design (`docs/design/task-replacement-plan.md:130`).
- **Nested calls are recorded in clio-core.** The PTC plan intended them as observer events only, with `parent_call_id`; measured, each one writes about 7 clio-core records (transcript part atoms, task-event highway, artifact index, task lookup), 34 ms a call (ledger CT5).
- **A surface's stored state is its transcript parts.** `a2ui_store` rebuilds a surface by replaying its parts in order; when a part cannot be written it raises `A2UITranscriptFrozenError` ("no surface state was persisted", `a2ui_store.py:659`). Each applied batch is published as `a2ui.surface.upserted` with the whole surface, which gact-tui's reducer replaces wholesale (`packages/core/src/v3/reducer.ts`). Every non-transient publish enters the event bus's bounded replay log (`gact/events.py`); `Event` has a `transient` flag that keeps an event out of it.
- **Every data-model update is a transcript part.** `update_a2ui_data_model` goes through `a2ui_store.apply_batch_outcome` and persists a new part through `emit_surface_part` (`a2ui_producer/_emit.py`). With a turn running, the part goes into that turn's ledger, and once that ledger has settled the update is refused with `a2ui_transcript_frozen`. With no turn running, it is appended to the session's live assistant message (`tool_observer._append_live_assistant_part`). A script updating at 1 Hz for eight hours would add 28,800 parts to the transcript and to every export. `expected_revision` is optional, so a script can update without reading the revision first.
- **Nothing tells the model a surface can be live.** `create_a2ui_surface`'s description presents a surface as a view the agent creates and revises in place during its turns. Neither it nor `update_a2ui_data_model` says a surface keeps its id and can be kept current by a running script, and `update_a2ui_data_model` is a deferred visuals tool, so a script finds it only through `tools.search`. A chart bound by `dataUri` to an artifact is validated once at creation, and no gact-tui code was found that refetches it when the file changes: a script appending to a CSV behind a chart produces a chart that never moves, with no error.
- **Viewer files bind through the data model.** `a2ui_producer/_bound_files.py` lets a viewer's file property (`meshUri`, `rasterUri`, `dataUri`, `url` and others) be bound to a data-model path. `A2UI_URL_SCHEMES` is `https`, `artifact`, `resource` and `study`.
- **One frame path exists, inside the browser** (verified on the merged branch). `gact/browser/live.py` streams JPEG frames from Playwright's screencast over a WebSocket admitted with the `clio-bearer.` subprotocol and the origin guard, only while a viewer is connected, with the person's input sent back on the same socket.
- **No image can come from outside.** The desktop CSP's `img-src` allows `'self'`, `data:`, `blob:` and OpenStreetMap tiles only, and A2UI has no component that shows frames.

## Design: launch

**The schema.** `run_code(code: str, background: bool = False, label: str = "")`. This changes TD's "`code: str`, nothing else": record it as a CT decision. The schema stays fixed for the life of the process, so the cached prefix is untouched. `label` names the task in the Work rail and in task listings; empty means the first line of the script, truncated.

**Acceptance.** With `background=True`, `run_code` asks the permission gate with the code shown, as today. On approval it submits owned work through `NativeWork` and returns at acceptance with the handle, the label and the kernel's start state. The model keeps working. `wait_tasks`, `observe_tasks`, `get_task_result` and `cancel_tasks` work on the handle unchanged.

**The path (Phase 0, CT).** `run_code` stays a builtin; its synchronous branch is unchanged. Only `background=True` submits, through `BuiltinBoundary.native_call` → `NativeTools.submit`, to a worker declared with `TaskConfig(mode="required")` whose body runs through `NativeWork.run` — the chain `connected_data_index` uses. No CLIO tool has an optional task mode, and `bash`'s pattern would send synchronous calls through the task layer. This needs a native root kind for code (`tools/native_roots.py:18`, `arc/task_types.py:13`, `native_task_settlement.py:470`, `task_runtime.py:171-174`), the task handle set as `TASK_CALL.submission_handle` in the body, and one approval: `native_call` gates again (`builtin_boundary.py:398-419`), so `run_code`'s own approval is passed on as inherited, as the shell's is.

Phase 0 decides how `run_code` reaches `NativeWork`: either `run_code` is declared with an optional task mode and the `background` argument selects it, or the background branch submits to the same worker path `native_storage` uses. Whichever it is, there is one admission path, and the synchronous branch does not pass through the task layer.

**No limits.** Nothing bounds how many background scripts run, for how long, or how much memory they use (rule 6). Each kernel's resident memory is shown on its task row, so the person sees what a script costs and decides what to stop.

**No shared pool to separate.** The owner's requirement is that watchers never make the agent's own calls wait. Phase 0 found that already true: every script and every model step has its own gates and pool (current state). No new pool is added; CK4b checks the property on a live stack.

## Design: kernels per task

A background script gets a fresh kernel keyed `(session_id, "task:" + handle)`, spawned through `wrap_confined` with the same profile, write roots and network policy as the scope's kernel. It shares no variables with the scope's kernel; a script that needs earlier results reads them from files. The scope's interactive kernel stays free, so synchronous scripts run beside any number of background ones.

The idle reaper already skips a kernel whose lock is held, so a running background kernel is never reaped. The kernel stops when its task settles, when its session is deleted (through the `close_session` call the browser plan adds to the delete path), or when the task is cancelled.

## Design: lifetime

- **No call timeout.** `code.call_timeout_s` and `code.max_nested_calls` are removed for both modes (CT decision recording the change to TD17).
- **Stop does not cancel it.** Stopping the turn that launched a background script leaves it running, as storage indexing does. `cancel_tasks(handle)` cancels it; the kernel is discarded and the task settles as cancelled with its output so far.
- **Children and forks.** A background script launched by a child session belongs to that child and outlives it: its settle and its notifies wake the child, which may be waiting on exactly that (the owner's decision; no rule cancels owned work when a child settles). It is cancelled like any task, by the agent or the person, and the parent's Work rail shows it in the child's subtree. A fork inherits no background scripts.
- **Restart.** A clean shutdown cancels each running background script, which settles as cancelled with its output so far. After a crash, the generic native recovery settles it as interrupted (`native_runtime_interrupted`), as it does every native task. Neither is resumed.

## Design: approval

The launch approval shows the code and the person's choice covers the script's nested calls under the script's grant (`gact/code_mode/grants.py`), as for a synchronous script. For a background script the grant is bound to the task handle, not to the turn, so it holds for the task's life. The permissions are the snapshot `SubmissionContext.permissions` took at acceptance; changing the approval mode afterwards does not widen or narrow a running script. To change what it may do, the person cancels it.

**Questions with no turn running.** A nested call that needs to ask pauses the script and raises the ordinary permission question in the owner's conversation, whether or not a turn is running there. Today the gate takes `current_turn_id` from the running turn, so this is new work, not a check: a question owned by the task's handle, shown in the owner's conversation as a question from that task, answerable by the person with no turn running, and resolved through the same gate decision and grant rows as any other question. When a turn is running, the question appears in it as today. Waiting for the answer is not counted against anything. If the owner session is deleted while a question is open, the task is cancelled. The model is not woken to ask on the script's behalf.

The question is a Core `Question` task owned by the script's handle (the kind `hook_defer` uses), so it survives the absence of a turn, is answered through the same route and gate decision, and is cancelled with its task. Its session is the task's owner, never `sessions.most_recent()`, and only cancelling the task or its owner denies it, never Stop on an unrelated turn. No question expires: the 600 s expiry (`permission_timeout.py`) and the egress prompt's 600 s (`runtime/grants.py:96`) are removed for every question, turn-owned ones included (rule 6). A turn waiting on a person also counts as making progress: today the wait publishes nothing and is not a commitment wait, so with the expiry gone the turn's no-progress watchdog (`limits.turn_timeout_s`, 900 s, `turn_watchdog.py:117-154`) would cancel it instead. The wait is tracked like a declared commitment (`runtime/commitment_activity.track`).

## Design: reporting

**The `task` binding.** A background kernel gets a second entry in the binding registry, `task`, bound only in background kernels. It exposes:

- `await task.progress(message, done=None, total=None)`: progress on the task row through the existing progress path. It wakes nobody.
- `await task.notify(message, data=None, image=None)`: a mailbox message from the task to its owner session, through `conversations.mail.peer`, the same delivery as `send_agent_message`. A busy owner receives it at its next iteration; an idle owner wakes. The message carries the handle and label, so the model knows which script spoke. `data` is a JSON value, bounded by the result room with the overflow spilled to a file. `image` is a path in the session's scratch folder, attached to the message as an image part. Mail has neither today (current state): this adds a task sender to `MailItem` (the handle, rendered with the task's label, not as "Agent session") and an image part to its content.
- `await task.stream(label, content_type="image/jpeg", max_fps=10)`: opens a frame stream (below).

A synchronous script calling `task.*` gets a `NameError`, since the binding is not there; the fragment says so.

**Notify rate.** `task.notify` is the one way a script costs model turns, so wakes are bounded per owner session, not per task: at most one wake per `code.notify_min_interval_s` (default 30) for each owner, whatever number of background scripts it has. Notifies from any of its tasks inside the window are queued and delivered together with the next wake as one message, grouped by task with each task's handle and label, never dropped. Thirty watchers therefore cost at most one turn per window, not thirty. Each task's result says how many of its notifies were coalesced.

**One record per window of repeated calls.** A script that updates a surface every second must not leave 28,800 rows in the session's history. For a background task, consecutive successful calls to the same tool are recorded as one counted record per `code.background_record_window_s` (default 60): the tool, the count, the first and last time, and the last call's arguments and result. Errors, denials and any call that raised a permission question are recorded individually. The gate still checks every call; only the record is grouped. The Work rail's collapsed row shows exactly what is stored. Phase 0 first measures what one nested call writes today on the observer and event path (the current-state note says nested calls are observer events, not ARC records, so they may not reach clio-core at all), and the ledger records the measurement beside this decision.

**Output.** The script's stdout and stderr go to a file in the session's scratch folder as they are written. `observe_tasks` returns the output through the existing cut-result path, as the shell tool does: what fits the result room inline, the rest in the file, and the `result_spilled` note naming it. The final result is the same bounded observation a synchronous script returns (the output, the error, the value of a final expression, the nested-call count), cut the same way, delivered as a task result.

## Design: live surfaces

**Two live channels, one per kind.** A surface is live when something keeps feeding it. Values (temperatures, progress, a series) go through the surface's data model, and images go through a frame stream (below). A `dataUri` artifact is a snapshot, read when the surface is created or its components are updated. Files are not made to refresh a surface, since that would be a third live mechanism.

**Updates from a task are state, not transcript.** Today every data-model update persists a transcript part tied to a turn. That is right for an agent revising a view in its turn and wrong for a script feeding one for hours. An update whose call comes from a background task, identified by the task handle in the call context, therefore:

- changes the surface's stored data model in place in `a2ui_store` and publishes it as a transient `a2ui.surface.upserted`, the event viewers already handle, so they see it at once without filling the replay log; a client that reconnects catches up from `GET /v1/sessions/{sid}/a2ui/surfaces`;
- does not go through a turn's ledger, so it is never refused with `a2ui_transcript_frozen` and needs no turn running;
- persists at most one transcript part per surface per `a2ui.live_checkpoint_s` (default 60): a checkpoint that sets the whole data model at `/`. The ledger stays append-only (#767): a new checkpoint is appended, never written over the last one, and replay of a surface starts from its latest checkpoint. When the task settles, a final checkpoint is written.

An eight-hour feed therefore leaves the surface's creation part and one checkpoint per minute in the transcript, of which replay reads only the latest, and an export shows the latest values. Updates made by the agent in its own turn keep today's behaviour. **What Phase 0 found, and what it makes the work.**

- **There is no checkpoint replay to reuse.** A surface record keeps every `updateDataModel` as its own message slot, capped at 512 (`gact.ledger_retention.a2ui_messages.max`; past it the oldest non-structural slot is dropped, `a2ui.py:445-461`), and every `a2ui.surface.upserted` carries all retained slots. Replay from the latest checkpoint is built in `a2ui._apply_staged_message`: an `updateDataModel` at `/` drops the surface's earlier non-structural slots, as `updateComponents` is already materialized into one slot (`a2ui.py:434-443`); updateDataModel slots are already treated as re-derivable scratch (`a2ui_component_fold.py:83-93`).
- **The checkpoint writer.** Checkpoints are written with the store's default writer (`_persist_part`, `a2ui_store.py:518-535`), which appends a standalone message and never raises `A2UITranscriptFrozenError`. Through `emit_surface_part` a checkpoint would be refused while a turn is finalizing, or absorbed into a running turn's message. A surface with its creation part and checkpoints only folds correctly: each checkpoint is an update on an existing surface (`a2ui.py:433-479`).
- **Revisions stay monotonic.** gact-tui applies a surface only when its revision exceeds what it has applied (`processor-store.ts:595-602`), so in-place updates bump `surface.revision` and stamp the changed slot, and the revision after a restart is never lower than one a client already applied.
- **The task handle reaches the update path** as `TASK_CALL.submission_handle`, set in the background body (current state); `turn_id` cannot tell a task's call from a turn's, since it is copied into later contexts (`context.py:49-51`).
- **Reconnect.** The client reconciles from `GET /v1/sessions/{sid}/messages?limit=40` (`use-session-live-stream.ts:92-131`), not from `/a2ui/surfaces`. With one checkpoint message a minute, a surface's creation part leaves that page within an hour and the surface disappears from the client until the next upsert. The reconnect therefore also fetches `GET …/a2ui/surfaces`, which folds the full record.
- **No refetch storm.** Every `a2ui.*` frame refetches four pending-interaction queries today (`use-session-live-stream.ts:269-283`); a transient feed upsert does not.

**The composition.** A live view is a surface, a background script and the bindings between them. The agent creates the surface with its components, binds each live property to a data-model path, and launches the script with the `surface_id`. The script writes values into those paths and writes stream ids into the stream components' bound paths. Order does not matter: a stream component whose path is still empty shows that it is waiting.

**Ending.** The surface survives the script. When the script ends or is cancelled, the surface keeps its last values in the final checkpoint, each stream leaves its last frame as a snapshot (below), and the script's last update should mark the surface stopped (a status field the components show), so a frozen view is never mistaken for a live one.

**A chart reads the data model (found by the 2026-11-05 probe).** `clio.chart.v1`'s `data` is an inline array in the component, not a data-model binding, so a chart cannot read a series the script keeps in the data model; today only `dataUri: study://...` is a live chart source (polled every 4 s). The work adds a binding: in clio-schemas `data` also accepts a data-model path (a `DynamicValue` resolving to rows, under the same 10,000-row cap), the server's chart guard (`a2ui_producer/_chart_spec.py`, `_data_reference.py`) validates the bound rows' shape on each update, and gact-tui renders the bound rows and re-renders when they change. Metric, progress and text components already bind to data-model paths.

**Data shape.** A live series lives in the data model as a bounded list the script trims itself (for example the last 600 points of a 1 Hz feed), plus scalar fields for current values. The worked example shows this, so the model does not grow a list for eight hours.

## Design: frame streams

**What a stream is.** A named channel of still images owned by a task. The task writes frames; viewers in the UI see the latest one. It is the generic form of the browser's live view, and every producer of images into the UI uses it: a background script relaying a camera, a script rendering a plot per timestep, and the browser's screencast. The trame remote view joins later, in the remote visualization work.

**Opening.** `task.stream(label, content_type, max_fps)` calls a native tool, `open_frame_stream`, through `tools.*`, so opening is one gated, recorded call with the stream's label and owner. It returns a stream object whose `id` is `stream_<hex>` and whose URI is `stream://stream_<hex>`. `content_type` is `image/jpeg`, `image/png` or `image/webp`. At most `streams.max_per_task` (default 4) and `streams.max_per_server` (default 64) are open; past that, opening returns `stream_limit`.

**Writing.** `await cam.write(frame_bytes)` sends the frame to GACT over the kernel bridge as binary, not as a tool call: frames are data on a channel already opened and gated, the way stdout is. The text protocol stays as it is; Phase 0 chooses between a length-prefixed binary message on the protocol descriptor and a second descriptor for frames, and measures throughput at 640 by 480 JPEG and 10 frames per second. A frame larger than `streams.max_frame_bytes` (default 2 MB) or of the wrong type raises `stream_frame_invalid`. Writes above `max_fps` are dropped at the source and counted. `await cam.close()` ends the stream early.

**Delivery.** GACT keeps only the latest frame per stream in memory. `GET /v1/sessions/{sid}/streams/{stream_id}` upgrades to a WebSocket admitted like the browser's live route, with the `clio-bearer.` subprotocol and the origin guard. A viewer gets the latest frame at once and then each new one; a slow viewer skips to the latest rather than queueing. Nothing is sent while no viewer is connected, and writes stay cheap, since they only replace the latest frame. Frames go through GACT, so a stream works the same for a local agent, a remote one and one behind clio-relay's tunnel, and the CSP stays as it is. Frames are never written to the transcript.

**What moves from the browser (Phase 0 item 11).** The generic half of `gact/browser/live.py` becomes the service: admission (`_admitted`: origin through `origin_guard.browser_origin_allowed`, bearer through `auth.supplied_bearer_token`, accept-then-close refusals 4403, 4401 and 4404; the `clio.browser.v1` subprotocol echo), the viewer registry and its lifecycle, starting the producer on the first viewer and stopping it on the last, the latest frame given to a joining viewer, per-viewer drop-oldest delivery, state messages and the receive loop's parsing and view-only refusal. The screencast and its first-frame screenshot, action annotations, relaunch pause and resume, input playback with `person_acted`, and the page state stay with the browser source. Four faults are fixed in the move: the drop loop can discard a queued state message (it must drop only frames), state messages queue without bound, replies to input are written from the receive coroutine while the sender also writes (one writer per socket), and a closed page leaves its viewers' sockets open (the service sends `stopped` and closes them). Admission becomes one helper shared with the infrastructure transport (`transport_admission.transport_refusal` repeats the same order and codes).

**State beside frames.** The socket also carries the stream's state as JSON: `live`, `stopped` or `waiting`, the frame rate over the last ten seconds, the size, and the producing task's handle and label.

**Snapshots.** When a stream ends (its task settles, the script closes it, or the session is deleted), its last frame is registered as an image artifact with a `generated` edge from the task's call. The stream URI then resolves to that artifact, so a surface reopened later, or an exported session, shows the last frame with a stopped label instead of an empty box.

**The agent looking at a stream.** `read_frame_stream(stream_id)` writes the latest frame to the session's scratch folder and returns its path, for `view_image`. This is the stream's capture in the view contract of `scientific-visualization-plan.md`. A script that judges frames itself (a check for a failed print every few minutes) passes one to `task.notify(..., image=path)` instead of waking the model for every frame.

**The browser's live view.** The frame half of `gact/browser/live.py` (admission, viewer-gated sending, latest-frame delivery, state messages) becomes the stream service, and a browser page becomes a stream source whose frames come from the screencast. The browser's input messages stay a capability of that source on the same socket; a source without input, like a script's stream, refuses them. The browser plan's W scenarios for the live view must still pass after the move.

## Design: the stream component

A new A2UI component, `frame-stream`, in the kernel catalog. It implements the view contract:

- **Props.** `streamUri`, a `stream://` URI or a bound data-model path, which `_bound_files.py` exports like the other viewer file keys; `fit` (`contain` or `cover`); an optional `caption`.
- **Rendering.** The latest frame, drawn into a canvas from the socket, with its state shown as an icon and a short text label (for example "Live, 10 fps", "Waiting" or "Stopped"), and the producing task as a link to its row. Fuller detail is on hover on desktop and on tap at phone width.
- **Capture and introspection.** Capture is the current frame. Introspection returns the stream id, state, frame rate, size and the producing task.
- **Provenance.** The producing task and, after the stream ends, the snapshot artifact.

`stream` is added to `A2UI_URL_SCHEMES`. A `stream://` value is valid only in `streamUri`, and only for a stream in the same session or a snapshot it resolved to; anything else is refused with `a2ui_stream_invalid` at the export boundary, like a bad `artifact://` reference.

## Design: teaching the composition

The model has to find these patterns from a request that does not name them ("keep an eye on the printer and show me how it's doing"). It is taught where each decision is made, not only in a skill the model may not load:

- **`create_a2ui_surface`'s description** gains two sentences: a surface keeps its id, and a background `run_code` script can keep it current, values with `tools.update_a2ui_data_model` and images through a `frame-stream` component bound to a stream the script opens; a `dataUri` artifact is a snapshot and does not update when its file changes.
- **`update_a2ui_data_model`'s description** gains one sentence: called from a background script, it updates the surface in place and reaches every viewer at once, so it can run on every tick.
- **`run_code`'s description** gains one line for `background`: for work that outlives the turn (watching a job, a device or a running computation), returns a handle, and the script may keep a surface live, stream images with `task.stream` and call `task.notify` on a condition.
- **The `frame-stream` catalog entry** says what it shows, that its `streamUri` is usually bound to a data-model path the script fills, and that it leaves a snapshot when the stream ends.
- **The `code.run` fragment** gains two sentences: use `background=True` for work that outlives the turn, and call `task.notify` only on a condition worth the person's or the model's attention.
- **The `task-protocol` skill** gains a section with one worked example: create a surface with a chart, current-value fields and a `frame-stream`, launch a background script that polls a status endpoint and a camera snapshot endpoint, keeps a trimmed series in the data model, writes frames to a stream, marks the surface stopped on exit, and notifies with an image when something looks wrong. It also shows the wrong versions (a CSV behind a `dataUri` chart, an image artifact written per frame) and why they fail.

The surface descriptions change for every agent that has the A2UI tools, so the change goes through the prompt dump check (`clio-agent plugin resolve <agent> --exact`) for the standard agent and the science agent, and the cached prefix stays stable within a process.

## Design: presentation

Status follows the gact-tui rule: an icon with a short text label, never an icon alone, and fuller detail on hover on desktop and on tap at phone width.

The background script appears in the Work rail as a task row: its label, a running state with elapsed time, the nested-call count, its memory use, its open streams with their frame rates, and the last progress message, with a cancel control always visible and the output on expand. A synchronous script's row shows the same while it runs. A surface kept live by a script shows a "Live" label with an icon, linked to the script's task row, so the person can see what feeds it and stop it from there.

Its nested calls nest under the task row through `parent_call_id`. Consecutive calls to the same tool are shown as one row with a count and the last call's arguments and result, so a script that updates a surface every second for eight hours shows one `update_a2ui_data_model ×28,800` row. This is presentation only; every call still goes through the gate and the observer. Frames written to a stream are not calls and do not appear as rows; the stream's frame count is on the task row.

In the transcript, the launching `run_code` row shows the code and links to the task row. A `task.notify` arrives in the conversation as a message from the task, rendered with the label, any image, and a link to the task; a coalesced wake shows its notifies grouped by task.

## Phase 0: inventory and decisions

No production code changes in this phase.

1. Decide how `run_code` reaches `NativeWork` (an optional task mode selected by `background`, or a direct submission from the background branch) and record it.
2. Confirm `SubmissionContext` and `NativeWork` can carry a kernel's lifetime: cancellation reaches the body, `runtime_loss_observer` fires on restart, and Stop leaves accepted work running.
3. Measure what one nested call writes today on the observer and event path (records, bytes and time per call, whether any reaches clio-core), with a script making 1,000 nested calls, and record it beside the counted-record decision. Done (CT5): 7.3 clio-core puts and 34 ms a call; nested calls do reach clio-core.
4. Read the permission gate's turn dependency (`current_turn_id`) and write down how a question owned by a task handle is raised, shown and answered with no turn running, for Phase 1 to build.
5. Confirm `conversations.mail.peer` can be called with the task as sender, carries an image part, and wakes an idle owner; and decide where the per-owner notify window lives.
6. Confirm the task handle is visible in the call context on the `update_a2ui_data_model` path; that replay can start a surface from its latest checkpoint part (the whole model at `/`) without breaking replay or export; and that `a2ui_store.py:659` behaves correctly when a surface has only its creation part and checkpoints.
7. Confirm gact-tui's reducer handles a full-surface `a2ui.surface.upserted` arriving after a reconnect, following a catch-up from `GET …/a2ui/surfaces`.
8. Confirm that a `dataUri` chart does not refetch when its artifact's file changes, and that in-place data-model updates from a background script reach an open surface in gact-tui at 1 Hz without dropped or reordered updates.
9. Measure a background kernel's resident memory, idle and with numpy and h5py imported, for the task row's memory display (not a limit).
10. Choose the binary frame path on the kernel bridge and measure it at 640 by 480 JPEG and 10 frames per second, locally and through the remote tunnel, end to end into a viewer. Done locally (CT11: a second descriptor); the tunnel is a local-phase row.
11. Read `gact/browser/live.py` on the browser branch and list what moves into the stream service and what stays with the browser source. Done (Design: frame streams, "What moves from the browser").
12. List every timeout that ends work without a person deciding, and keep each one with a recorded reason or remove it. Done: the ledger's timeout inventory (CT13).
13. Write the ledger section with decisions CT1 onward and the scenarios below. Commit, push, open draft PRs, and continue.

## Phase 1: launch, kernels and approval

1. The `background` and `label` arguments, the acceptance path through `NativeWork`, the handle returned at acceptance.
2. Kernels keyed by task handle, their start and stop, their memory reported to the task row, and the background pool `code.background_concurrency`.
3. Remove `code.max_nested_calls` and the kernel discard at `code.call_timeout_s` (TD17 changed by a CT decision).
4. Cancellation, Stop leaving it running, subtree cancellation for children, `code_task_runtime_lost` on restart.
5. The grant bound to the task handle and the permissions snapshot.
6. Permission questions owned by a task handle, raised and answered with no turn running.

## Phase 2: reporting

1. The `task` binding in the registry, bound only in background kernels.
2. `task.progress` through the progress path; `task.notify` through `conversations.mail.peer` with image parts and the per-owner window and coalescing. Done except the image part (5e68ac32): a notify is an `attention` mail item attributed to the task, as a settled task's notice is; the mail item has no image part yet.
3. One counted record per window of repeated successful calls from a background task; errors, denials and questions recorded individually.
4. Streaming output to scratch, and `observe_tasks` and the final result through the cut-result path with `result_spilled`.

## Phase 3: live surfaces and frame streams

1. In-place data-model updates from a background task: the stored model changed and published as a transient `a2ui.surface.upserted`, no turn ledger, an appended checkpoint (written by `_persist_part`) every `a2ui.live_checkpoint_s` and on settle, a `/` update dropping earlier data-model slots, monotonic revisions; in gact-tui, the reconnect fetching `/a2ui/surfaces` and no pending-query refetch on transient upserts.
2. The stream service: `open_frame_stream`, latest-frame storage, the WebSocket route and admission, state messages, limits, snapshots on end.
3. The binary frame path on the kernel bridge and `task.stream`, `write` and `close`.
4. `read_frame_stream`.
5. The browser's live view moved onto the stream service, with its input as a source capability; the browser's live-view tests and W scenarios pass.

## Phase 4: presentation and teaching

1. `clio.chart.v1` `data` bound to a data-model path (clio-schemas, the server's chart guard, the gact-tui renderer).
2. The `frame-stream` component and the `stream://` scheme in clio-schemas, its catalog entry, `streamUri` binding through `_bound_files.py`, the `stream` scheme and `a2ui_stream_invalid`.
2. The sentences in `create_a2ui_surface`, `update_a2ui_data_model` and `run_code`, and the prompt dump check.
3. The task row in the Work rail, nested calls under it, collapsed runs of the same tool, the "Live" label on a fed surface, stream rates on the row, all as icon and label.
4. The launching row's link, notify messages with images in the conversation, grouped when coalesced.
5. The fragment sentences and the skill section with the worked example and the counterexamples.

Verify each UI change as the megachange plan describes: restart the stack, run `node scripts/cloud/ui_check.mjs` with a `--steps` script at 1440 by 900 and 390 by 844, read every screenshot, and check that `consoleErrors` and `failedRequests` are empty.

## Phase 5: qualification

Run focused tests and `ruff` on touched paths while working. Then run these against a freshly restarted stack (`claude_code` provider), and record each with commands, handles, numbers and the screenshots read. Every request is a plain request that names no tool. The fixture is a small status server under `tests/code_mode/fixtures/` served by `scripts/cloud/stack.sh`: a JSON endpoint whose temperature follows a scripted curve and whose job finishes after a set time, and a camera snapshot endpoint that returns a JPEG with a frame counter and the time drawn on it.

| ID | Scenario | Passes when |
| --- | --- | --- |
| CK1 | "Watch the fixture job and tell me when it finishes; it takes about 40 minutes" | One background `run_code`; the turn ends at acceptance; the script runs past 600 s; the owner wakes once, on completion, and reports it |
| CK2 | "Show me a live chart of the fixture temperature and warn me if it goes above 230" | The script creates or binds an A2UI surface and updates it every second without model turns; the chart moves in the UI; one wake when the threshold is crossed; the task row shows `update_a2ui_data_model` collapsed with a count |
| CK3 | After CK2 has run for an hour | The session's stored size has stayed bounded: the surface has its creation part and 60 checkpoints, not 3,600 parts, and replay reads the latest; the history holds about 60 counted records for `update_a2ui_data_model`, not 3,600; no update was refused with `a2ui_transcript_frozen` |
| CK4 | Start ten background watchers over ten fixture jobs in one request | Ten handles, ten kernels, all running together; a synchronous script run meanwhile in the same conversation runs at once |
| CK4b | Thirty watchers each making nested calls as fast as they can while the agent runs a turn that reads files | The agent's own calls start without waiting on the watchers' calls (their queue wait is recorded) |
| CK5 | Stop the turn right after CK1's launch | The background script keeps running and still wakes the owner on completion |
| CK6 | "Stop watching the fixture" during CK1 | `cancel_tasks` on the handle; the kernel is gone; the result carries the output so far |
| CK7 | Restart the server cleanly during CK1, then kill it during another run of CK1 | The clean restart settles the task as cancelled, the kill as `code_task_runtime_lost`, each with its output so far; nothing restarts either |
| CK8 | A script that calls `task.notify` every second for a minute | At most one wake per 30 s; each delivered message lists the coalesced notifies; none is lost |
| CK9 | Ten watchers from CK4 each notify every few seconds | The owner wakes at most once per 30 s in total; each wake groups the notifies by task |
| CK10 | A background script whose nested call needs approval, while no turn is running | The question appears in the owner's conversation as the task's question; the person answers it with no turn running, after more than ten minutes; the script resumes; time waiting is not counted; the model was not woken to ask |
| CK11 | Change the approval mode while CK2 runs | The running script keeps the permissions it was accepted with |
| CK12 | A child session launches a background script that finishes after the child settles | The script keeps running after the child settles; its settle wakes the child, which reports to its parent |
| CK13 | `observe_tasks` on CK2 after an hour | The output through the cut-result path with `result_spilled` naming the file, the nested-call count, the memory use and the last progress message |
| CK13b | Reconnect the UI during CK2 | The client catches up from `GET …/a2ui/surfaces`; the chart continues from the latest values; the replay log holds no feed updates |
| CK14 | The Work rail with CK4's ten watchers running | Each row shows its state, elapsed time, nested-call count, memory use and last progress, each as an icon with a text label, and a cancel control that stops it |
| CK15 | A synchronous script that runs past 600 s and makes more than 500 nested calls | It runs until it finishes; Stop on its turn cancels it and discards the kernel |
| CK16 | Export CK2's session | The launching cell, the task row with its collapsed nested calls, the notify message and the surface with its latest values show offline |
| CK17 | "Keep an eye on the fixture printer for me and show me how it's doing" (no mention of live, charts, camera or updates) | The agent builds a surface fed by a background script, unprompted, with values through the data model and the camera through a `frame-stream`; both move; the series is trimmed; no CSV-behind-`dataUri` chart and no artifact per frame appears |
| CK18 | Cancel CK17's script | The surface keeps its last values and shows that it stopped; the stream component shows the snapshot with a "Stopped" label; the snapshot is an artifact with a `generated` edge from the task's call |
| CK19 | "Show me the fixture temperature" (a one-off question) | One synchronous read and an answer or a static view; no background script is started |
| CK20 | During CK17, close the surface for five minutes, then reopen it | Nothing was sent while it was closed (server log); on reopening the latest frame and the latest values appear within a second |
| CK21 | During CK17, "does the print look all right?" | The agent calls `read_frame_stream` and `view_image`, and answers from the frame, citing its counter or time |
| CK22 | A script that writes 30 frames per second to a stream opened at `max_fps=10` | Delivered rate at or under 10; dropped writes counted on the task row; the script is not slowed |
| CK23 | A surface whose `streamUri` names another session's stream | Refused with `a2ui_stream_invalid` |
| CK24 | The browser plan's live-view scenarios (W9, W10, W16) after the move | All pass on the stream service |
| CK25 | Export CK17's session | The stream component shows its snapshot offline, and the surface its last values |
| CK26 | The Work rail, a fed surface and a stream component at 390 by 844 | Every status reads as an icon with a text label; detail opens on tap |

CK17, CK19 and CK21 test the model's judgement, not a mechanism. Run each three times and record every run. When one fails, fix the descriptions, the catalog entry, the fragment or the skill, not the request, and record the change and the rerun.

The OctoPrint case from #1908 (a real printer and webcam on a LAN with a local agent) and a real Slurm job through the `slurm` MCP server are recorded as local-phase rows. Nothing else may be blocked. A failing scenario is fixed, not deferred.

## Done

1. Every phase is implemented, and the browser's live view runs on the stream service.
2. Focused tests pass, and `ruff check` and `ruff format --check` are clean on touched paths.
3. Every cloud-phase scenario passes against a freshly restarted stack, each shown with commands, handles, numbers and the screenshots read; local-phase rows say so.
4. The ledger section holds every decision with its evidence, the Phase 0 measurements (kernel memory, frame path), and every CK17, CK19 and CK21 run.
5. The branches are pushed in clio-agent, clio-schemas, gact-tui and clio-agent-marketplace; the pins point at the pushed work (the marketplace submodule, clio-agent's `clio-schemas` requirement and gact-tui's generated schema types); and the draft PRs carry a summary and the evidence.

## Working conventions

- Use the branch the cloud session is set up to push to, with the same name in all four repositories. Open draft PRs against `develop`.
- Small Conventional Commits per step, with test evidence in each PR description.
- Keep runtime state in `.cloud-run/`. Stop the stack when you finish a work block.
