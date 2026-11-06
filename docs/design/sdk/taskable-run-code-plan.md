# Taskable `run_code`, live dashboards and streams: implementation plan

**Status: accepted (CT25, 2026-11-05) and built (CT26 to CT38).** This version replaced v3. It follows the owner's addenda A, B and C (the planning conversation of 2026-11-05) and his answers recorded in the ledger (CT16 onward). The "Current state" section describes the branch before implementation; the design sections state what was built, with a pointer where the build changed the plan. Where a later decision in the ledger (CT25 onward) disagrees with this document, the ledger wins.

## The goal

A user asked CLIO to watch her 3D printer the way OctoPrint does: temperatures, progress and state, updating on their own (clio-agent #1908). CLIO could only take snapshots when asked. This plan makes that work as a general CLIO semantic, with nothing printer-specific in CLIO. It needs four pieces, and this plan is all four:

1. **Async tasks.** `run_code` becomes taskable, so a script can watch something for hours without holding the conversation.
2. **Views and dashboards unified.** A study view is a dashboard, built and reviewed like every dashboard.
3. **Live updates.** Whether a view updates is a property of the source it reads, with one rule for every source. A script's writes to a file become new versions of its artifact.
4. **Streams.** A stream is an artifact whose latest content is held in memory, for state that changes too often to be worth writing to disk at every change, such as a printer's status. GACT holds it and writes it to clio-core as versions by the stream's flush policy (CT25 (5), CT28 (b)). Streaming ships as a CLIO plugin package.

Together they let the agent write a small script that reads the printer, keeps its status in a stream, and shows it on a dashboard that updates by itself. The canonical script is general:

```python
while True:
    data = get(source)
    write(f, data)
```

`f` is a file or a stream. A dashboard bound to it follows it. Nothing in the script knows about dashboards.

## Contract

This plan is the contract for the four pieces. The TD decisions of `tool-calling-plan.md`, D1 to D29 in `acceptance-ledger.md`, and the F, M and L decisions stay in force except where this plan changes them. When code and this plan disagree, change the code. If the plan turns out wrong, choose the fix that best keeps its intent, update this document in the same change, and record the decision in the ledger.

| Document | Governs |
| --- | --- |
| This plan | Background `run_code`, study views as dashboards, liveness by source, file writes as versions, streams and the streaming plugin, how the model learns them |
| `docs/design/sdk/tool-calling-ledger.md` | This plan's decisions (prefix `CT`) and scenarios (prefix `CK`), in their own section |
| `docs/design/sdk/tool-calling-plan.md` | Synchronous `run_code`, the kernel, dispatch, stubs, `callers`, grants |
| `orchestration-semantics.md` and the agent-task-native design | Owned work, handles, the mailbox, cancellation, wake rules |
| The artifact designation rulings (#966) | How a file becomes an artifact and a version |
| gact-tui `CLAUDE.md` | UI rules, including that status is never encoded only as an icon |
| clio-schemas | The A2UI catalog (the `dataUri` grammar, which gains live references) and the artifact models (which gain the stream kind); clio-agent pins it (`clio-schemas==` in `pyproject.toml`) and gact-tui generates its types from it |
| The clio-agent marketplace | The `run-a-study`, `create-dashboard`, `review-visual-presentation` and `task-protocol` skills, the science log fragment, and the streaming plugin |

**Base and branch.** The branch is `claude/keen-dirac-hf724m` in every repository, on its current base (clio-agent `1928f984`, which carries the browser merge and the kernel binding registry). They stay as they are.

`CT` and `CK` collide with no other prefix (checked 2026-11-05 across clio-agent, gact-tui, clio-schemas and the marketplace).

## Rules

The megachange rules hold: delete and replace, the task layer is the only execution authority, the model decides, no silent fallback, one mechanism per concept, no fifth store, agent-facing caps come from the existing result room rather than fixed numbers, evidence over claims. Five more apply:

1. **One tool, two modes.** Background is a mode of `run_code`, not a second code tool. The script language, the stubs, dispatch, the fence and the grant are the same in both.
2. **A background script is owned work.** It is admitted, observed, cancelled and settled through `gact/native_work.py` and the common Core history. Nothing in `gact/code_mode/` keeps its own registry of running scripts.
3. **The person stops work, not a limit.** No count, rate, wall-time or memory limit ends a script, synchronous or background, and no timeout ends work that waits on a person. A script ends when it finishes or when the agent or the person cancels it. A running script is made obvious and easy to stop instead: its state, elapsed time, nested-call count, last progress, memory use and a cancel control.
4. **Liveness belongs to the source.** A view does not decide whether it updates; the source it reads does. Every source is fixed or live, and the UI applies one rule to all of them. There is no second liveness mechanism beside it (in particular, no script calling `update_a2ui_data_model` on a timer as a live channel).
5. **The plugin owns the stream semantic; core owns only what a plugin cannot provide.** The stream tools, the kernel binding, the skills and the prompt text are in the streaming plugin. Core adds the stream artifact kind, its storage in clio-core, the push to subscribed views, the client rule, and the extension points the plugin needs, and nothing stream-specific beyond those.

## Out of scope

- Images, cameras, video and scientific visualization. A stream carries what an artifact carries (a JSON status, a table of recent readings, a series). Images may come later as artifacts; nothing here prepares for them.
- The browser. Its shared tab is a finished, separate feature. Four delivery problems found while reading `gact/browser/live.py` are filed separately as iowarp/clio-agent#1918.
- Moving the NDP event feed onto streams. It is the same kind of source and will likely move later.
- Resuming a background script after a server restart, moving one to another machine, or running one through clio-relay.
- Promoting a running synchronous script to background.
- Changes to `set_loop`, `loop_wakeup` or `/loop`.

## Current state

Read on the branch heads (clio-agent `d51bef74`, gact-tui `af2ff5d10`, marketplace `31e2357`) on 2026-11-05, read-only.

### Part 1: `run_code`

Built on this branch and kept (commits in the ledger, CT1 to CT9, CT13, CT15):

- `run_code(code, background=False, label="")`. With `background=True` it asks the gate with the code shown, stages the launch, and submits owned work on a `code` native root (`gact/native_code.py`, task kind `Code`, worker `run_code_task`); the handle is returned at acceptance, and `wait_tasks`, `observe_tasks`, `get_task_result` and `cancel_tasks` work on it. The synchronous branch does not touch the task layer.
- Each background script has its own kernel keyed `(session_id, "task:" + handle)`, discarded when the task settles. The scope's kernel stays free.
- No count or clock ends a script in either mode (`code.max_nested_calls` and the `code.call_timeout_s` discard are removed). The task row shows elapsed time, calls and memory (31 MB idle, 55 MB with numpy and h5py, measured).
- A permission question waits for the person with no expiry, and the wait counts as turn progress. A background script's question belongs to its task: only cancelling the task ends the wait.
- The `task` binding (bound only in background kernels, through the kernel binding registry): `task.progress` updates the task row and wakes nobody; `task.notify` wakes the owner with an `attention` mail attributed to the task, at most once per `code.notify_min_interval_s` (30) per owner, coalesced and never dropped.
- Repeated successful calls from a background script are recorded as one counted record per `code.background_record_window_s` (60); errors and questions are recorded alone; the gate checks every call (CT5).
- Output goes to a scratch file as it is written; results go through the cut-result path with `result_spilled`.
- A child's background script outlives the child (CT6). A clean restart settles it as cancelled; a crash as interrupted (CT7).

Not yet built at the time: the Work rail task row and its collapsed nested calls, the launching row's link, the notify message's rendering, and the teaching. All built since (CT29 to CT38; gact-tui `9d4bef797` row state in words, `a00542830` grouped call counts, `525dfeb03` the launch row's and messages' task links).

**Cost of a nested call.** A script's call to a tool writes about 7.3 clio-core records (CT5, `sess_ba2a1d84b12b`): 3.6 to `segments` (the call's transcript parts and task events, grouped per chunk), 0.7 to `task_lookup`, and 3.0 to `artifact_index`. The last three are the provenance transform every completed call records, read-only calls included (`tool_observer.py:662-671` → `transforms.record_transform` → `registry_store.add_transform`, `:462-474`): the session's transform page, the transform record marked pending, and the record again to clear the mark. Each is its own round trip on the writer thread, ahead of the group commit (`highway_writer.py:207-213`, `:294-298`). Whether a read-only call needs a transform record at all is part of the clio-core interaction work, not of this plan.

### Part 2: study views and dashboards

- **A chat surface and a dashboard are different things, correctly.** `create_a2ui_surface` (`a2ui_producer/create.py:25-186`) makes an inline view stored in the session's transcript. `publish_dashboard_report` (`dashboard_reports.py:84-137`) compiles a dashboard without a chat view (`dashboard_document.py:59-131`), stores it as a report artifact (`dashboard_reports.py:44-81`) and gact-tui renders it in the side panel (`dashboard-view.tsx`). Both render through `ClioA2UISurface` and both call `validate_component_data_references`, but the dashboard path passes no notes, so an empty study source is accepted silently (`dashboard_document.py:90`).
- **Study views are chat surfaces.** The code already calls them dashboards (`science_log/views.py:1`, `entries.py:156`, the marketplace's `method.md:9`). `run-a-study` (`plugins/science-log/skills/run-a-study/SKILL.md:103-124`) tells the agent to make one with `create_a2ui_surface` and attach it with a `view` entry. `create-dashboard` and `review-visual-presentation` never mention studies or `study://`.
- **A dashboard cannot be attached as a view.** The `view` write gate (`science_log/store.py:189-251`) requires a live chat surface of the writing session in `a2ui_store`, and refuses with "create it with create_a2ui_surface first".
- **Views are read from the owner's transcript.** `study_views` (`views.py:17-48`) reads each view entry's surface from `a2ui_store`; the Science log tab (`study-views.tsx:14-110`) renders it from a REST snapshot, which `use-science-log.ts:14-36` polls every 4 s while a branch runs. The same surface also appears inline in the owner's chat.

### Part 3: liveness

- **A data view can only read a pinned version or a study source.** The catalog (clio-schemas 0.6.0) allows a chart, table or map `dataUri` only as `artifact://artifact_<id>` (one immutable version) or `study://<study>/(measurements|entries|records/<name>)`. Export rewrites a workspace path to the pinned version id (`_export.py:119-174`), and `_data_reference.py:94` validates only that form. A `latest` reference fails the catalog with `a2ui_validation_failed`.
- **`latest` resolves in one place.** `references.py:76-178` parses `artifact://<ws>/<name>@<ref>` (no `@ref` means `latest`); only `GET /v1/sessions/{sid}/references/resolve` uses it, once per request. `POST /v1/artifacts/{id}/table-query` reads one immutable version (`artifact_table_query.py:400-498`) and gact-tui caches it forever (`IMMUTABLE_QUERY`, `table-query-rows.ts:298-319`).
- **Study sources are the only live data sources.** `POST /v1/studies/{study}/table-query` rebuilds rows on every call (`study_sources.py:31-103`). gact-tui caches study sources as mutable, refetches them on `science.entry` (`science-log-updates.ts:49-67`), and polls `records/` every 4 s (`table-query-rows.ts:160-166`), because study records have no change event: their rows come from tool results and are rebuilt on read (`sources.py:157-187`).
- **The version event never reaches views.** `artifact.version.added` (`versions.py:328-380`) goes out as `semantic.event` (`semantic_events.py:654-660`), so gact-tui's handler for the name (`use-session-live-stream.ts:329-337`) never runs; a `semantic.event` invalidates only iterations, work and provenance.
- **Nothing notices file writes.** A file becomes an artifact only when something names it (designation, #966.1: "never by a filesystem scan", `designation.py:3-4`). A script overwriting `f` changes bytes on disk and the registry never hears. A script can already call `create_artifact(path=f)` (it is not direct-only): that mints a new version when the bytes changed, but also emits `artifact.proposed` and counts against `artifacts.proposals_per_turn` (8 names). A dedicated call must not report the file as a `written_path` result, since a file older than the call goes to the drift path and becomes a gap version (`versions.py:666-711`).
- **What a version costs.** About 4 `artifact_index` puts (`a/<id>`, the session page, the whole record, the record again on confirm), 2 synchronous `_events` appends (`artifact.version.added`, `artifact.alias.moved`) and a session-record rewrite (`registry_store.py:382-403`, `minting.py:262-313`). The record holds every version and is rewritten twice per version, so bytes grow with the square of the chain's length. Nothing bounds an artifact's versions (`registry_store.py:590-598` only undoes crash orphans).

### Part 4: streams

- **CLIO's clio-core has no in-memory data tier.** The seeded `cte.yaml` (`clio_core_config.py:144-173` at the branch head) composes a 1 GB RAM pool that is clio-core's working arena and file tiers for data, all at score 1.0 and `temporary` persistence; `clio_core_growth.py` adds more file tiers at runtime. Several comments still describe a DRAM tier that is not configured (`clio_core_config.py:125-128`, `:338-339`; `storage.py:390-392`, `:456-460`; `clio_core_writes.py:70`).
- **A put cannot choose its tier.** The Python binding's `PutBlob`/`AsyncPutBlob` take no score. `Tag.ReorganizeBlob(blob, score)`, `GetBlobScore` and `RegisterTarget` exist; CLIO never calls them. `put(..., tier=...)` ignores its tier. The durability check refuses a configuration with no non-RAM persistent tier (`clio_core_durability.py:53-100`). iowarp-core 2.4 ships a `clio_cte_stream` module (page blobs, deferred appends) with no Python binding, not composed by CLIO.
- **GACT pushes only through the session stream.** One SSE stream per session (`events.py`, `routes/misc.py:369-540`); `Event.transient` keeps an event out of replay. There is no per-view subscription and no server-side latest-wins; `subscriber_count` is per session.
- **What a plugin can contribute.** Tools, kernel bindings (stdlib source whose `export` calls `tools.*`, `plugins/kernel_bindings.py:42-196`), skills and injections. A binding can reach only `tools.*` calls, which pass the gate and the observer (about 7.3 records each, counted per window in background scripts). Reserved host calls, which skip the gate and the observer, can be registered only by core (`dispatch.py:106-160`), and any script code can call a registered one.

## Design: Part 1, taskable `run_code`

As built (current state). The remaining design:

**Presentation.** Status is an icon with a short text label, never an icon alone, with detail on hover on desktop and on tap at phone width. A background script is a task row in the Work rail: label, running state and elapsed time, nested-call count, memory, last progress message, output on expand, and a cancel control always visible. Its nested calls sit under it through `parent_call_id`; consecutive calls to one tool show as one row with a count, matching the counted record. The launching `run_code` row links to the task row. A notify arrives in the conversation as a message from the task, with its label and a link to the task; a coalesced wake shows its notifies grouped by task.

**Notify path (decided, CT38).** A notify is an `attention` mail attributed to the task handle, as a settled task's notice is, not `conversations.mail.peer`: `peer` mail records a sending session, and a task is not one. Notifies of several scripts in one wake are one mail per task (`e5f7fefd`).

## Design: Part 2, a study view is a dashboard

A study view is a dashboard attached to a study. It is authored the way every dashboard is, through `create-dashboard` and its `review-visual-presentation` loop, published with `publish_dashboard_report`, and saved as a report artifact.

- **Linking.** A `view` entry names the dashboard artifact (its report id), not a chat surface. The write gate checks that the dashboard exists in the study's workspace instead of a live surface.
- **Listing and rendering.** `study_views` lists the dashboards the view entries name, at their latest report version, and the Science log tab renders them with the dashboard renderer. The 4 s poll of the science log read for views goes: a dashboard's sources update it (Part 3).
- **Validation.** Publishing a dashboard reports empty study sources as a chat surface does (`study_sources_unchecked`), instead of accepting them silently.
- **Teaching.** `run-a-study` and the science log fragment point the agent to `create-dashboard` for views. `create-dashboard` covers study sources, pinned and live artifact bindings, streams, and when to use each.
- **Old views stay readable (CT25 (3)).** New views are dashboards: the gate's surface check and the skill's instruction for chat-surface views are gone. A view entry that already names a chat surface is still read from the owner's transcript and shown read-only (`science_log/views.py`); nothing converts it.

## Design: Part 3, liveness is a property of the source

Every `dataUri` source is fixed or live and names its change event:

| Source | Kind | Changes on |
| --- | --- | --- |
| An artifact pinned to a version (`artifact://artifact_<id>`) | Fixed | Never |
| An artifact addressed by `latest` (`artifact://<ws>/<name>@latest`) | Live | A new version of that artifact |
| `study://<study>/measurements` and `entries` | Live | `science.entry` |
| `study://<study>/records/<name>` | Live | A change event for the record, added here |
| A stream (Part 4) | Live | Every write, announced to watching views |

**One client rule.** gact-tui reads every source through one client: a fixed source is cached as immutable; a live source is refetched when its change event arrives on the session stream (for a stream too: the event notifies and the view re-reads its bounded rows, Part 4). The study-specific caching, the refetch wiring and the `records/` polling are replaced by the rule. Chat surfaces and dashboards render the same components, so both get it.

**Pinned or live is the agent's choice.** A view that shows a file at one moment binds the pinned version, as today. A view that follows a file binds its `latest`. The work:

- clio-schemas: the chart, table and map `dataUri` grammar accepts a named artifact reference with `@latest`.
- The producer side (`_export.py`, `_data_reference.py`): accepts a `latest` reference, validates it against the artifact's current version, and the tool result says which bindings are live and which pinned.
- Table query: the client re-resolves `latest` to a version id on each version event and caches each version's rows under its id; no table query by `latest` was added (CT26 (c)).
- The version event reaches views: `artifact.version.added` is delivered under a name the client handles, scoped to the artifact, so the one rule can refetch the views bound to it.
- Study records gain a change event when a tool result adds rows to a record.

**A changed file becomes a new version.** A script's writes to `f` must produce new versions of `f`'s artifact for a view on `latest` to follow it. How the change is noticed and how often a version is minted are open questions 1 and 2.

## Design: Part 4, streams

**What a stream is.** An artifact whose latest content is held in memory instead of written to disk at every change: GACT holds it (`gact/streams/store.py`) until clio-core can keep a blob in memory by choice (`docs/design/clio-core-requests.md`, issue 7). It is an artifact in every other way: identity, provenance, `artifact://` references, `latest`, version events and export. It carries any content an artifact can (a JSON status record, a table of recent readings, a series). It is for state that is rewritten often and where the latest value, or a short window of recent values, is what matters. A dashboard reads a stream the way it reads any artifact.

**Content and versions.** A stream keeps its latest content, or a bounded window of recent content, as declared when it is created. Writing to a stream does not mint a durable version per write: the content is written to clio-core as the artifact's next version on open, every `flush_s` (default 30 s) while it changes, at stop and at a clean shutdown (CT28 (b)).

**Delivery.** A view subscribes to a live artifact through GACT, and GACT pushes when it changes. A stream announces a change: a write sends a transient `stream.updated` event, at most once per `streams.push_interval_s` (latest wins), only to sessions whose views hold a lease, and the view re-reads its bounded rows, so a view's `dataQuery` applies unchanged (CT28 (c)). Nothing is sent while no view watches. A disk artifact pushes a change notice and the view refetches through the table-query path (Part 3). Views go through GACT, never straight to clio-core, so the desktop does not depend on where the agent runs, and authentication and the desktop CSP stay as they are. As built, a view on screen holds a lease on the stream (`POST /v1/streams/{id}/watch`, 30 s, renewed every 15 s, released on unmount), and the announcements go through the session stream as transient events, coalesced per stream on the server.

**Persistence.** clio-core writes down to disk; what a stream needs is a flush policy, declared when it is created: whether a flush keeps every update or only the latest content and its window. When a stream's producer stops, its last content is what a saved dashboard and an export show. Where the content lives was open question 5, answered (c): GACT holds it (CT25 (5)).

**Who writes.** Any producer that can write to clio-core: a background script, an MCP server, a cluster job through clio-relay. For this plan the background script must work. How a script writes is open question 6.

**The agent reading a stream.** The agent reads a stream's latest content the way it reads any artifact, so it can answer "how is the printer doing" from the live state without waiting for the script.

**The streaming plugin.** One CLIO plugin package contributes the stream tools (create a stream with its retention and flush policy, write, read the latest content, close), the kernel binding a script writes through, the skills and the prompt text. Core provides only what a plugin cannot (Rule 5): the stream kind in clio-schemas and the registry, its storage in clio-core, the subscription registry and push, the client rule, and, if open question 6 picks the binding route, a way for a plugin to declare a host call that core implements.

## Design: teaching

The agent has to find these semantics from a request that names none of them ("keep an eye on my printer and show me how it's doing"). It is taught where each decision is made, for understanding, not as handcuffs:

- **`run_code`'s description** gains one line for `background`: for work that outlives the turn (watching a job, a device or a running computation); returns a handle; the script may write a file or a stream that a dashboard follows, and calls `task.notify` on a condition worth attention.
- **The `code.run` fragment** gains two sentences: use `background=True` for work that outlives the turn; notify only on a condition worth the person's or the model's attention.
- **The streaming plugin's tool descriptions and results** say what a stream is, when to choose it over a file (state rewritten often, where the latest value matters) or a fixed artifact (a result), and that a dashboard bound to it updates by itself.
- **`create-dashboard`** explains fixed and live sources, pinned and `latest` bindings, study sources and streams, and when to use each.
- **`task-protocol`** gains a worked example: a background script that polls a status endpoint and writes the status into a stream, a dashboard bound to it, and a notify on a threshold; and one that watches a job and notifies on completion.
- **The system prompt** changes only if the qualification runs show the model missing the semantic after the skills and tool messages, and then for understanding.

Each change goes through the prompt dump check (`GET /v1/sessions/{sid}/agent?exact=true`, the prompt last sent in a session of that agent) for the standard and science agents, and the cached prefix stays stable within a process.

## Open questions for the owner

Each with what was found and a recommendation.

1. **How a script's file write becomes a version.** (a) Explicit: the script says so through a gated, recorded call naming the file. `create_artifact(path=f)` already works from a script but emits `artifact.proposed` and counts against the 8-names-per-turn cap; a dedicated call would mint a producing version attributed to the script's `run_code` task. (b) A workspace watcher: needs nothing from the script, but contradicts the designation ruling (#966.1) and has no writer to attribute a change to outside an observed call, so changes would mint gap versions. **Recommendation: (a), a dedicated call in the `task` binding or the artifact tools.**
2. **Version rate.** Every write a version (about 4 index puts, 2 synchronous event appends and a growing whole-record rewrite each; 28,800 versions in eight hours at 1 Hz, with no retention), or rapid rewrites collapsed into one version per interval. **Recommendation: one version per interval for files, and state that changes every second goes in a stream.**
3. **Study views already attached as chat surfaces.** Convert them to dashboards, or show them as they are until the study adds new ones. **Recommendation: show them as they are, read-only, and make every new view a dashboard;** a conversion would author dashboards the agent did not review.
4. **Artifact and study reads.** Two endpoints under one client rule, or one source endpoint. **Recommendation: two endpoints under one rule now;** the rule is what makes liveness general, and one endpoint can follow without changing views.
5. **Where a stream's content lives.** CLIO's clio-core has no in-memory data tier and a put cannot choose one. (a) Add a RAM data tier to CLIO's clio-core configuration and move stream blobs onto it with `ReorganizeBlob` after each put (the durability check and the boot RAM check change accordingly). (b) Ask clio-core for a put with placement or a Python binding for `clio_cte_stream` (recorded in `docs/design/clio-core-requests.md` for the owner to file). (c) GACT holds a stream's latest content and window in its process and writes it to clio-core by the flush policy, until (a) or (b) exists. **Recommendation: (b) as the target and (c) to build now behind the same artifact interface;** (a) changes every CLIO install's clio-core configuration for one feature.
6. **How a script writes to a stream.** (a) Through `tools.*`: one gated, recorded call per write, counted per window (CT5), about 7.3 clio-core records a call today plus the stream write. (b) Through the kernel binding: one gated open, then writes to a host call core implements, with no gate or observer per write; needs a way for a plugin to declare a host call. **Recommendation: (b);** the gate checks the open, and per-write records are what the stream exists to avoid.
7. **A stream's default retention and flush.** Latest only, or a window; flush every update, or only the latest and its window. **Recommendation: latest only by default, a window when created with one; flush the latest content on an interval and when the producer stops.**

## Phases

**Phase 0 (implementation decisions, no production code).** For each open question as answered: confirm the exact code paths named above on the branch head; measure what a stream write costs by the chosen route; check how a stream reference and a `latest` reference resolve through `_export.py` and `_data_reference.py`; confirm the subscription design against the session stream's throughput. Record each in the ledger.

**Phase 1: Part 1 presentation and teaching.** The Work rail task row, collapsed nested calls, the launching row's link, notify rendering; the description line and the fragment sentences.

**Phase 2: a study view is a dashboard.** The `view` entry links a dashboard; the gate, `views.py` and the Science log tab follow; dashboard publish reports empty study sources; the marketplace skills and fragment point to `create-dashboard`; new chat-surface views are refused, and old ones stay readable, read-only (CT25 (3)).

**Phase 3: liveness by source.** `latest` references in clio-schemas (its own pin), export and validation reporting live and pinned bindings; the client re-resolving `latest` on each version event (no table query by `latest`, CT26 (c)); the version event delivered to views; the study record change event; one client rule in gact-tui with the study-specific caching and polling removed.

**Phase 4: file writes become versions,** by the mechanism and rate chosen.

**Phase 5: streams.** Core: the stream kind (clio-schemas and the registry), its storage by the answer to question 5, the subscription registry and push, the stream as a live source in the client rule, and the host-call extension point if question 6 picks it. The plugin: the stream tools, the binding, the skills and the prompt text.

**Phase 6: teaching,** checked with the prompt dump.

**Phase 7: qualification** (below), ending with the showcase.

Verify each UI change as the megachange plan describes: restart the stack, run `node scripts/cloud/ui_check.mjs` with a `--steps` script at 1440 by 900 and 390 by 844, read every screenshot, and check that `consoleErrors` and `failedRequests` are empty.

## Qualification

Against a freshly restarted stack (`claude_code` provider), each recorded with commands, handles, numbers and the screenshots read. Every request is a plain request that names no tool. The fixture is `scripts/cloud/fixtures/printer_status.py`, started on its own port (`python scripts/cloud/fixtures/printer_status.py --port 8799 --print-s 240`; `stack.sh` does not start it): a printer status endpoint (state, temperatures, progress following a scripted curve) whose print job finishes after a set time. No camera endpoint.

| ID | Scenario | Passes when |
| --- | --- | --- |
| CK1 | "Watch the fixture job and tell me when it finishes; it takes about 40 minutes" | One background `run_code`; the turn ends at acceptance; the script runs past 600 s; the owner wakes once, on completion, and reports it |
| CK2 | Ten background watchers over ten fixture jobs in one request | Ten handles and kernels running together; a synchronous script run meanwhile runs at once |
| CK3 | Thirty watchers making nested calls as fast as they can while the agent reads files | The agent's own calls start without waiting on the watchers' |
| CK4 | Stop the turn right after CK1's launch | The script keeps running and still wakes the owner on completion |
| CK5 | "Stop watching the fixture" during CK1 | `cancel_tasks` on the handle; the kernel is gone; the result carries the output so far |
| CK6 | Restart the server cleanly during CK1, then kill it during another run | Cancelled on the clean restart, interrupted after the kill, each with its output so far; nothing resumes |
| CK7 | A script that notifies every second for a minute | At most one wake per 30 s; each wake lists the coalesced notifies; none lost |
| CK8 | Ten watchers each notifying every few seconds | The owner wakes at most once per 30 s in all; each wake groups the notifies by task |
| CK9 | A background script's nested call needs approval while no turn runs | The question appears as the task's; the person answers after more than ten minutes with no turn running; the script resumes; the model was not woken to ask |
| CK10 | Change the approval mode while a watcher runs | The running script keeps the permissions it was accepted with |
| CK11 | A child launches a background script that finishes after the child settles | The script keeps running; its settle wakes the child, which reports to its parent |
| CK12 | `observe_tasks` on a watcher after an hour | The output through the cut-result path with `result_spilled`, the call count, memory and the last progress |
| CK13 | A synchronous script past 600 s with more than 500 nested calls | It runs to the end; Stop on its turn cancels it and discards the kernel |
| CK14 | The Work rail with CK2's ten watchers, at 1440 by 900 and 390 by 844 | Each row shows state, elapsed time, calls, memory and last progress as icon and text, with a cancel control that works |
| CK15 | "Keep an eye on the fixture job and tell me when it's done" (implicit) | A background script is launched |
| CK16 | "What's the fixture job's status?" (a one-off question) | One synchronous read and an answer; no background script |
| CK17 | A study run from a plain request | Its views are dashboards built through `create-dashboard`, reviewed with `review-visual-presentation`, linked from `view` entries and shown in the Science log tab |
| CK18 | A dashboard over `study://…/measurements` while entries arrive | It updates through the one client rule, not a study-specific path |
| CK19 | A `records/` source while a tool adds rows | It updates on its change event; no polling in the network log |
| CK20 | One dashboard bound to a file's `latest`, another to a pinned version, while new versions are minted | The first updates, the second does not |
| CK21 | "Keep an eye on the fixture job and show me how it's doing" (no mention of live, charts or files), three runs | A background script writes a file and the agent builds a dashboard bound to its `latest`; the dashboard moves as the file changes |
| CK22 | Export CK21's session | The dashboard shows the file's last version offline |
| CK23 | A background script writes the fixture printer's status into a stream | A dashboard bound to it updates at every write with no model turns |
| CK24 | The same with no dashboard open, then opened | Nothing is pushed while closed (server log); on opening the latest status appears at once |
| CK25 | "How is the printer doing?" mid-run | The agent reads the stream's latest content and answers from it |
| CK26 | The script stops | The dashboard keeps the last status and shows that it stopped; a saved dashboard and an export show it too |

CK15, CK16 and CK21 test the model's judgement. Run each three times and record every run; when one fails, fix the descriptions, the skills or the fragments, not the request.

**The showcase: the printer case of #1908, end to end.** A plain request, "keep an eye on my printer and show me how it's doing", against the fixture printer. The agent launches a background script that polls it and writes the status into a stream, builds a dashboard bound to the stream that updates by itself, answers "how is it doing?" from the stream, and when the script stops the dashboard keeps the last status and says it stopped. Three runs, each recorded with handles, the dashboard's updates, the screenshots read at both sizes, and empty `consoleErrors` and `failedRequests`.

The OctoPrint case with a real printer on a LAN and a local agent is a local-phase row.

## Done

1. Every phase is implemented, with focused tests, `ruff check` and `ruff format --check` clean on touched paths.
2. Every scenario and the showcase pass on a freshly restarted stack, recorded in the ledger with their evidence.
3. The ledger holds every decision with its evidence and every judgement run.
4. The branches are pushed in clio-agent, clio-schemas, gact-tui and the marketplace, with the pins pointing at the pushed work (the marketplace submodule, `clio-schemas==`, gact-tui's generated types).

## Working conventions

- Use `claude/keen-dirac-hf724m` in every repository. No pull requests unless the owner asks.
- Small Conventional Commits per step, with test evidence.
- Keep runtime state in `.cloud-run/`. Stop the stack when a work block ends.
