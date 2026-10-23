# Long-term memory: implementation plan

This plan covers item (f) of the CLIO 1.0 release, the long-term memory system. Long-term memory records what the agent should carry forward about a user, their projects and how they work. It is separate from session transcripts and from the science log, which record what happened. The plan has six workstreams, each building on the ones before it:

1. **Store and search.** Memory documents live in clio-core as a new ARC record kind, searched with clio-core's BM25 indexer. The indexer is not wired into clio-agent's default deployment today, so this workstream starts by fixing that.
2. **Tools and the read path.** The agent sees an index of the user's memory at session start, searches and reads documents on demand, and writes when the user asks it to remember, correct or forget something.
3. **The background pass.** After a session goes idle, a model reads it, proposes memory changes, and the user reviews them. The user can switch this pass off, and the operator picks its models once for the whole fleet.
4. **Export and import.** Memory exports as a `.zip` of Markdown files and imports either from a CLIO export or from another assistant, through review.
5. **The memory page and in-chat surfaces.** A browsable page of memory by category, a notice in the conversation when memory changes, and attribution on each answer that used memory.
6. **Usage tracking.** Counters on every memory document and skill record when it is loaded, used, and used in a run that succeeded. The background pass uses them to retire memories.

The work runs to completion without stopping for review. The human reviews the finished branches.

## Contract

This plan is the contract for the six workstreams. The megachange design documents still govern everything they cover, and decisions D1 to D29 in [acceptance-ledger.md](acceptance-ledger.md) stay in force. When code and this plan disagree, change the code. If the plan turns out wrong, choose the fix that best keeps its intent, update this document in the same change, and record the decision in the ledger.

| Document | Governs |
| --- | --- |
| This plan | Long-term memory: store, tools, read path, background pass, export and import, UI, usage tracking |
| [plugins-and-composition.md](plugins-and-composition.md) | Plugins, the composed prompt, fragments, self-optimizing plugins |
| [agent-sdk.md](agent-sdk.md) | `Agent`, `Subagent`, `Plugin`, `plugin check` |
| [orchestration-semantics.md](orchestration-semantics.md) | The task layer, flows, `ctx` |
| `docs/ARC_MEMORY_LAYER.md` | ARC record kinds and backends; updated by this plan |
| `research/agent-long-term-memory-survey.md` (project doc) | Background and evidence for the design; not a contract |

Base: `claude/sdk-plugins-flows` in clio-agent, gact-tui and clio-agent-marketplace, the plugins, Agent SDK and flows branch, or its merge target if the team has merged it. The delegation, fork and science-log plan named in an earlier draft was never written, so its F decisions do not apply, and the fork this plan relies on is the existing one: `POST /v1/sessions/{sid}/fork`, which makes a `branch` session with `fork_origin` that inherits memory at the fork point (ledger MD0). This plan's decisions use the prefix `MD`.

## Rules

The megachange and delegation-plan rules carry over unchanged: delete and replace, the task layer is the only execution authority, the model decides, no silent fallback, one mechanism per concept, no fifth store, the repository rules in `AGENTS.md`, and evidence over claims. Five more apply here:

1. **Memory lives on clio-core, through `ARCStore`, in its own namespace.** Documents, versions, proposals and counters are record kinds written through `ARCStore`, because `ClioCoreStore` is clio-agent's one clio-core client (daemon lifecycle, retries, durability checks). They live in a separate `ClioCoreStore` instance with namespace `memory`, so their tags are `memory/<kind>` and never mix with ARC's context plane. Do not add a database, a sidecar file or a parallel store. clio-core is the only ARC backend (`LocalFSStore` was deleted before this plan; ledger MD2).
2. **The model extracts; the server stamps.** What to remember, where it belongs and whether it is sensitive is decided by a model through typed outputs. Provenance (which session, which message, whether the message was the user's own text or tool output) is stamped by the server from the transcript. No keyword or regex rule decides what is remembered or filtered.
3. **Background changes are proposals.** The background pass never edits a memory document in place. It writes proposals, and a proposal changes memory only when it is accepted, by the user or by the auto-accept rule in Defaults.
4. **One meaning per name.** After this change the `memory_` prefix means long-term memory only. Transcript recall gets its own names. No alias or compatibility name for the old ones remains.
5. **Nothing the user cannot see.** Every memory document the agent can read appears on the memory page, every change has a version, and every answer that read memory shows which documents.

## Out of scope

Do not implement, stub or reserve fields for these:

- Embeddings, a vector index or a knowledge graph. BM25 over clio-core is the only retrieval.
- Memory shared between users or teams. Memory belongs to one account on one CLIO server.
- Federated or distributed memory across CLIO servers (v2.0).
- Generating personal skills from lessons. The lesson schema and personal marketplace belong to the optimization campaign ([plugins-and-composition.md](plugins-and-composition.md), "Self-optimizing plugins"). This plan only adds the usage counters that campaign will read.
- Remote agents or relay processes writing to clio-core directly. They write through the GACT server like any other caller.
- Memory of science-log content. The science log is its own record.

## Current state

What exists at the base, so nothing is rebuilt and nothing is missed:

- **clio-core search.** The CTE stores blobs under tags and answers regex (`BlobQuery`), time-window (`TemporalSearch`) and BM25 (`SemanticSearch`) queries. Since clio-core 2.2, BM25 is served by the optional `clio_cte_indexer` module (clio-core #905), an interposer that every put from a bound client passes through. A put is forwarded to the core first and then only enqueues its key; a background sweep (`index_sweep_period_ms_`, default 100 ms) tokenizes, and a search drains the queue first so it sees its own writes. Only blobs matching the module's `tag_re_` and `blob_re_` scope are tokenized, and out-of-scope blobs cost no scan, index memory or WAL; the default scope is everything. The module persists a snapshot plus write-ahead log and computes BM25 statistics per query over the regex-matched slice. Blobs carry no user metadata, and the CTE has no compare-and-swap.
- **The blocker.** clio-agent's seeded clio-core config does not declare the indexer, so `SemanticSearch` against the default deployment returns zero hits without raising (`arc/clio_core_config.py`, comment above `_DEFAULT_CTE_INDEXER_POOL_ID`). The 2.2.1 wheels did not ship the indexer binary, which caused a first-`PutBlob` hang; 2.3.1 ships it, but composition and client routing were never qualified. `warn_if_search_indexer_absent` reports the gap. `recall_context` is affected the same way. clio-agent pins `iowarp-core~=2.3.2`.
- **ARC records.** `ClioCoreStore` (`arc/storage.py`) maps `(kind, name)` to blob `name` in tag `<namespace>/<kind>`, writes searchable text to a companion blob `<name>.text`, and searches with `SemanticSearch(tag, blob_re, query, k)`. `ARC_KINDS` is conversations, invocations, variants, response_feedback, segments and task_index. `profiles` and `procedural` kinds were deleted in #771 for having no writers. Durability: `arc/clio_core_durability.py` requires a file tier at `persistence_level: "temporary"` and records that it survived a daemon restart on 2.2.1; `docs/ARC_MEMORY_LAYER.md` still says file-tier recovery is in progress.
- **Transcript recall.** The marketplace `memory` plugin claims `memory_search_sessions`, `memory_read_session_summary` and `memory_read_context_frame` (`gact/memory_tools.py`, ranking in `gact/runtime/memory_search.py` with Python term counts, not clio-core BM25). They enforce session versus cross-session scope and workspace boundaries and write an audit row (`gact/routes/memory.py`). No agent in the marketplace includes the `memory` plugin. `_requested_memory_search` in `gact/enrichment.py` injects an opt-in transcript search into a turn as a CLIO addition. `recall_context` (claimed by `core`) searches the current session's compacted context.
- **Prompt composition.** Fragments are static: templating is limited to agent identity and environment variables (`plugins/compose.py`). Per-turn additions go through `gact/enrichment.py` and are recorded in the context frame (`context.frame.created`).
- **Secondary models.** `lm/secondary.py` resolves a role's model (`SecondaryRole = Literal["summarizer"]`), inheriting the caller's model when unset. `gact/fleet_settings.py` stores durable model choices for the goals, variants and permissions judges and the search assistant, exposed at `/v1/settings/fleet` and edited in gact-tui's Fleet settings panel.
- **Settings.** `gact/runtime_settings.py` backs `/v1/settings/runtime` (Execution & history). gact-tui already has a **Memory** settings destination (`web/src/components/clio/settings-memory.tsx`) that searches retained conversations and shows context usage and compaction.
- **Background work.** `gact/scheduler_runtime.py` ticks once a minute for schedules. Response ratings live in the `response_feedback` ARC kind.
- **Scope.** Workspaces are the project unit (`gact/workspace_scope.py`, `GLOBAL_WORKSPACE_ID = "ws_global"`).
- **Export.** Session export lives in `gact/session_export*.py`.

## Design: documents and storage

**Subject documents.** Memory is a set of Markdown documents, one per subject, grouped by category and scope. A document is what the user browses, what the index lists and what BM25 ranks.

| Category | Holds |
| --- | --- |
| `profile` | Who the user is, stable for months |
| `preferences` | How the user wants the agent to behave |
| `topics` | Facts about the user by domain |
| `areas` | Ongoing involvements: grants, papers, collaborations, with status and decisions |
| `people` | Relationship context for collaborators |
| `environments` | Machines, sites and accounts: login nodes, schedulers, modules, allocations |

`profile` and `preferences` are one document each per scope. The other categories hold one document per subject.

**Format.**

```markdown
---
name: frontier-allocation
description: The user's OLCF Frontier allocation, project id and scheduler quirks
category: environments
aliases: [frontier, olcf]
updated: 2026-11-04T15:02:11Z
---
- Project id is CSC123; jobs go to the batch partition by default {id=f3k9, source=user, session=sess_ab12, at=2026-10-30}
- Prefers 2-hour walltime requests for test runs {id=f3m1, source=inferred, session=sess_cd34, at=2026-11-04}
- ~~Uses the debug QOS for short tests~~ {id=f2a0, source=user, session=sess_ef56, at=2026-09-12, superseded_by=f3m1}
```

Each fact is one line with a trailing annotation: a stable fact id, its `source` (`user`, `inferred`, `imported`, `tool`), the authoring session, the date, and `superseded_by` when replaced. A superseded fact stays in the document, struck through, and leaves the search text. The parser and serializer live in one owner module, `arc/memory_document.py`, and round-trip byte for byte.

**Record kinds.** Four kinds, written through a `ClioCoreStore` with namespace `memory` (tags `memory/memories`, `memory/memory_versions` and so on). They are not added to ARC's `ARC_KINDS`; the memory store declares its own kind list:

| Kind | Record name | Body | `.text` companion |
| --- | --- | --- | --- |
| `memories` | `user/<category>/<slug>` or `project/<workspace_id>/<category>/<slug>` | The Markdown document | Name, description, aliases and live facts |
| `memory_versions` | `<memory name>/<n>` | The document before change `n`, the change's author (session, tool call or proposal id) and time | None |
| `memory_proposals` | `<proposal id>` | Target document, the diff, evidence (session and message ids), stamped sources, status | None |
| `memory_usage` | `memory/<memory name>` or `skill/<plugin>/<skill>` | Counters and last-used time | None |

Usage counters are their own kind because every write to a record with a `.text` companion makes the indexer re-tokenize it. Versions make history and rollback possible without git.

**Scope.** `user/` is the account that owns the CLIO server. `project/<workspace_id>/` is one workspace. A session in `ws_global` sees user scope only. A session in a workspace sees user scope plus its own project scope, unless the workspace has **Use account memory** turned off, in which case it sees its project scope only. One `blob_re` selects the slice, for example `(user|project/ws_7f2)/.*\.text`.

**Writes.** Every write goes through one owner, `gact/memory/service.py`, in the GACT server, including writes requested by delegated and remote agents. It holds a per-document lock, checks a `sha256` precondition on update (a stale writer gets `memory_conflict` with the current hash), writes the previous body to `memory_versions` first, then the document and its `.text` companion in one `put_many`. A failure after the version write leaves the old document current and is raised typed.

## Design: search and the indexer

**Wire the indexer, scoped.** The seeded clio-core config declares `clio_cte_indexer` in `compose` at pool `564.0` with `next_pool_id` set to the core pool, `index_log_path` under the CTE data directory, and a scope of `tag_re: (memory/memories|segments)` and `blob_re: .*\.text`. The client binds to the indexer pool, matching clio-core's `test_indexer_restart_compose.yaml`. The scope matters: with the default `.*` the indexer would tokenize every segment and `_events` chunk, which are rewritten constantly and stored as base64-wrapped msgpack. Scoped, only the plain-text search companions of memory documents and of `recall_context` are indexed, and every other put pays only the interposer hop and a queue insert. Seeded configs from before this change are upgraded in place, once, logged, the way `ensure_seeded_config_durable` upgrades durability. A config without the indexer is a typed `ArcStoreUnavailableError` with reason `clio_core_search_unavailable` for anything that needs search, and `clio doctor` reports it red with the remedy. Phase 1 qualifies first-write latency, restart restore and a daemon crash on Linux, and records the result. If the indexer misbehaves, the fix goes into clio-core on its own branch with an upstream issue, not a workaround in clio-agent.

**`memory_search`.** BM25 over the `memories` slice for the session's scopes, optionally narrowed by category, returning document names, descriptions, scores and the matching live facts. `recall_context` gains real hits from the same fix and needs no other change.

## Design: tools and the read path

**The plugin.** `memory` moves from the marketplace into `plugins/builtin/memory` and joins `core` in `Agent.plugins`, so every agent carries it and an agent delta can drop it. It claims:

| Tool | Arguments | Does |
| --- | --- | --- |
| `memory_search` | `query`, `category=None`, `limit=8` | BM25 over the session's memory slice |
| `memory_read` | `name` | The full document, with superseded facts marked |
| `create_memory` | `subject`, `fact`, `category`, `scope="user"`, `reason` | Adds a fact the user asked to remember, routed into the matching document or a new one |
| `update_memory` | `fact_id`, `fact`, `reason` | Supersedes a fact with a corrected one |
| `forget_memory` | `fact_id` or `name`, `reason` | Removes a fact or a whole document |
| `session_search` | as `memory_search_sessions` today | Transcript recall across sessions, renamed |
| `session_read_summary` | as `memory_read_session_summary` | Renamed |
| `session_read_context_frame` | as `memory_read_context_frame` | Renamed |

The three `memory_*_sessions` names, their routes' tool names, the presenters in `gact/agents/native_presenters_memory.py` and the registry entries are renamed in the same change. The marketplace `memory` plugin is deleted.

**Who may write.** `create_memory`, `update_memory` and `forget_memory` are offered only in root sessions (`session: root`) and in forks of root sessions. Delegated children, including `explore`, `planner` and `review`, get `memory_search` and `memory_read` only. A remote agent gets the same read-only pair unless the user grants it write. User-requested writes have `source: user`, take effect immediately, and skip review.

**Routing a fact.** `create_memory` routes with one model call on the session's model: given the fact, the category and the index of that category's documents, it returns an existing document name or a new slug and description. The server writes the line. Routing never merges or rewrites other facts; that is the background pass's job.

**Forget.** A forgotten fact's line is deleted, not struck through, and its id goes on a per-scope `forgotten` list in the `profile` document's frontmatter. The background pass receives that list and does not propose the fact again.

**Fragment.** `memory.use` (`when: {tools: [create_memory]}`, within the 150-token budget): use `create_memory`, `update_memory` and `forget_memory` only when the user asks to remember, correct or forget; search memory before asking the user something it might already hold; do not narrate memory use.

**The memory snapshot.** At session start the server assembles a snapshot and pins it for the session, as plugin resolution is pinned:

- the full `profile` and `preferences` documents for the session's scopes;
- an index of every other document: one line of name, category and description, capped at `memory.index_max_tokens` (default 2,000), most recently used first, with a count of documents left out.

The snapshot is a CLIO addition delivered through the enrichment path with a fixed marker (`## Memory`), recorded in the context frame like every other addition, and kept when the context compacts. Fragments stay static. A write in the session appears in its tool result; the next session's snapshot includes it.

## Design: the background pass

**Trigger.** The scheduler tick finds root sessions idle for at least `memory.idle_minutes` (default 30) with at least `memory.min_user_messages` (default 3) user messages since the session's memory watermark. It skips the session when background memory is off, when the session was started with memory off, and when the workspace opts out. A fork is its own session. Delegated children are read through their root. The watermark (last processed message id) is stored on the session record so a session is processed once per new activity.

**Two stages, two models.**

1. **Extract** (fleet role `memory_extract`). A DSPy signature reads the session transcript and returns typed candidates: `fact`, `category`, `subject`, `sensitive: bool`, and `evidence`, the message ids it came from. The server stamps each candidate's `source` from its evidence: `user` when every evidence message is the user's own text, `tool` when any is a tool result, otherwise `inferred`. Candidates marked `sensitive` are dropped unless **Include sensitive topics** is on. That is a schema field the model fills, not a filter on words.
2. **Consolidate** (fleet role `memory_consolidate`). A run of the `memory` plugin's defined subagent `curator`, under the task layer, with `memory_search`, `memory_read` and one write tool, `propose_memory_change(target, diff, evidence, rationale)`. It reads the candidates, the current documents they touch, the forgotten list and the usage counters, and proposes additions, supersessions, merges, new documents and retirements. It cannot write memory directly.

The consolidate run is an ordinary task, so it appears in the task list, can be cancelled, and resumes after a restart through the existing task durability. One run processes one session's candidates.

**Fleet models.** Fleet settings gain two roles, `memory_extract` and `memory_consolidate`, beside the judges. `SecondaryRole` gains the same two values. An unset role uses the server's default model, since there is no caller to inherit from. The Fleet panel shows both with the same provider and model picker as the judges.

**Review and auto-accept.** Proposals land in the inbox. Settings choose what applies without review (Defaults below). A proposal whose evidence includes any `tool` or `imported` source always waits for review. Accepting writes through the memory service with a version whose author is the proposal; rejecting marks it rejected and keeps it for 30 days so the pass does not repeat it.

**Expiry.** A document not read in `memory.expiry_days` (default 60) gets a retirement proposal at the next pass that touches its scope. Accepting archives it: the document moves to `memory_versions` and leaves `memories`. `profile` and `preferences` never expire.

## Design: switches and settings

Runtime settings (Execution & history moves these into a Memory section; the API stays `/v1/settings/runtime`):

| Setting | Key | Default |
| --- | --- | --- |
| Use memory | `memory.enabled` | on |
| Generate memory from sessions | `memory.background` | on |
| Search past sessions | `memory.session_search` | on |
| Include sensitive topics | `memory.sensitive` | off |
| Apply background changes without review | `memory.auto_accept` | `user_stated` (one of `none`, `user_stated`) |
| Idle minutes before processing | `memory.idle_minutes` | 30 |
| Expire unused memories after | `memory.expiry_days` | 60 |

**Use memory** off removes the `memory` tools, the snapshot and the background pass; documents stay. Per workspace, **Use account memory** (default on) lives in workspace settings. Per session, the composer offers **Memory off** before the first message; it is recorded on the session and cannot be turned on later in that session.

## Design: export and import

**Export.** `GET /v1/memory/export?scope=all|user|project:<id>` returns a `.zip` laid out as `user/<category>/<slug>.md` and `project/<workspace_id>/<category>/<slug>.md`, each the stored document byte for byte, plus `manifest.json` with the CLIO version, export time and per-file `sha256`. Versions, proposals and counters are not exported.

**Import from CLIO.** `POST /v1/memory/import` with a CLIO export validates the manifest and every document's format. Each document is merged per fact id: a fact present in both keeps the newer line, a fact only in the archive is added with its original annotation. A document whose hash matches is skipped, so a repeated import is a no-op. The result reports added, updated and skipped counts. Every changed document gets a version authored by the import.

**Import from another assistant.** The memory page shows a prompt the user copies into their other assistant: it asks that assistant to list everything it remembers about the user, one fact per line, grouped by topic. The user pastes the reply back. The extract stage runs on the pasted text with every candidate stamped `source: imported`, and the consolidate stage turns them into proposals. Imported proposals always wait for review.

## Design: the memory page and in-chat surfaces

The gact-tui **Memory** settings destination is replaced. Its transcript search moves under the same page as **Past sessions**, behind the **Search past sessions** switch; its context usage and compaction views move to Execution & history, where compaction already lives.

**Memory page.**

- Header: the switches above, **Export**, **Import**, and the inbox count.
- One table per scope (Account, then each workspace that has memory), grouped by category in the order of the category table. Each row: name, description, last updated, a small usage indicator (read count and last read), and an actions menu with Open, History, Export and Delete.
- **Open** shows the document's facts. Each fact shows its source as an icon (user, inferred, imported, tool) with the session, date and evidence on hover, and links to the session it came from. Facts can be edited or deleted inline; an edit is an `update_memory` by the user. Superseded facts are collapsed under the live ones.
- **History** lists versions with author and time, shows a diff against the current document, and offers restore, which writes a new version.
- **Inbox** lists pending proposals grouped by document, each as a diff with its rationale and evidence links, with Accept, Edit and accept, and Reject. Proposals from `tool` or `imported` sources carry a marker.

**In the conversation.**

- When `create_memory`, `update_memory` or `forget_memory` runs, the tool row renders as a compact memory notice: an icon, "Memory updated" and the document name, linking to the document on the memory page. No text box.
- When the background pass adds proposals, the memory icon in the navigation shows a badge. Nothing interrupts the conversation.
- An answer whose turn read memory (`memory_read`, or `memory_search` results that were then read) carries a memory icon in its footer listing those documents on hover, each linking to the page. The snapshot's `profile` and `preferences` are listed as "loaded" in the same popover, separately from documents read.
- The context frame view shows the `## Memory` addition with its token cost.

Status is shown as icons with detail on hover throughout, not as text boxes.

## Design: usage tracking

`memory_usage` records, per document and per skill, `loaded` (in a snapshot or a skill index), `read` (`memory_read`, or `load_skill` for a skill), `read_in_success` (read in a turn that completed and received no negative `response_feedback`), and `last_read`. The memory service increments counters off the turn's hot path, batched at turn end. The page shows read count and last read; the consolidate stage receives the counters; the expiry rule reads `last_read`. Skill counters exist for the optimization campaign to read and are not acted on here.

## Defaults for the open questions

Take these and record each in the ledger.

| Question | Default |
| --- | --- |
| Where memory lives | Four record kinds on clio-core, through a `ClioCoreStore` with namespace `memory` |
| Record granularity | One document per subject; facts are lines with ids |
| What applies without review | Background facts whose every source is `user`; everything else waits |
| Index cap | 2,000 tokens, most recently read first |
| Idle trigger | 30 minutes and at least 3 user messages |
| Expiry | 60 days without a read; never for `profile` and `preferences` |
| Sensitive topics | Off |
| Unset fleet memory models | The server's default model |
| Who may write | Root sessions and forks of root sessions |
| Rejected proposals | Kept 30 days to suppress repeats |

## Phase 0: inventory and decisions

No production code changes in this phase.

1. Run the clio-core indexer composition from `test_indexer_restart_compose.yaml` against `iowarp-core` 2.3.2 on Linux in a scratch daemon, with the scope above: first `PutBlob` latency, a search, a clean restart, and a killed daemon. Then measure ARC's hot path (an `_events` append and a segments render) with and without the indexer in the chain, and record both as M1. If the interposer hop costs ARC's hot path more than the Phase 0 baseline noise, record it and route ARC's context-plane writes past the indexer in the same change rather than accepting the cost.
2. Confirm a file tier at `persistence_level: "temporary"` survives a clean restart and a kill on 2.3.2 with a `memories`-shaped record. Record it, and correct `docs/ARC_MEMORY_LAYER.md` to match.
3. List every caller of the three transcript tools and their routes, presenters, tests and UI strings that the rename touches.
4. Confirm how the account identity for `user/` scope is known to the server, and record it.
5. Confirm the enrichment path can carry a session-pinned addition that survives compaction and appears in the context frame. If it cannot, record the smallest change that makes it do so.
6. Write `docs/design/sdk/memory-ledger.md` with the megachange ledger's columns, decisions MD0 onward (prefixed `MD` so they never collide with scenarios M1 to M18), and the scenarios below.
7. Commit, push, open draft PRs, and continue to Phase 1.

## Phase 1: store and search

1. Declare the indexer in the seeded config, upgrade older seeded configs once, bind the client to the indexer pool, add `clio_core_search_unavailable`, and update `clio doctor`.
2. Declare the memory store's kinds (`memories`, `memory_versions`, `memory_proposals`, `memory_usage`) in the memory service, and leave `ARC_KINDS` unchanged.
3. Write `arc/memory_document.py` (parse, serialize, search text) with round-trip tests.
4. Write `gact/memory/service.py` on its own `ClioCoreStore(namespace="memory")`: scoped reads, search, locked writes with the hash precondition, versions, forget list, and counters.
5. Confirm `recall_context` returns hits on the default deployment.

## Phase 2: tools and the read path

1. Create `plugins/builtin/memory` with the tools, the `curator` subagent definition and the `memory.use` fragment; add it to `Agent.plugins`; delete the marketplace `memory` plugin.
2. Rename the transcript tools everywhere found in Phase 0, with no aliases.
3. Implement the write restriction for delegated sessions and remote agents.
4. Implement the snapshot: assembly, pinning, delivery through enrichment, the context frame record, and survival across compaction.
5. Count `loaded` and `read` for memory and skills.

## Phase 3: the background pass and settings

1. Add the runtime settings, the workspace **Use account memory** setting and the per-session **Memory off** flag, with the effects above.
2. Add `memory_extract` and `memory_consolidate` to fleet settings and `SecondaryRole`.
3. Write the extract signature, source stamping and the sensitive drop.
4. Write the `curator` run and `propose_memory_change`, the inbox records, accept and reject, and the auto-accept rule.
5. Add the idle trigger to the scheduler tick with the watermark, and the expiry proposals.

## Phase 4: export and import

1. Add the export route and the archive layout.
2. Add the CLIO import route with per-fact merge and idempotence.
3. Add assistant import: the prompt, the paste route, and extraction with `source: imported` into proposals.

## Phase 5: UI in gact-tui

1. Replace `settings-memory.tsx` with the memory page: switches, tables by scope and category, document view, history, inbox, export and import. Move context usage and compaction to Execution & history.
2. Add the two memory model roles to the Fleet panel.
3. Add **Use account memory** to workspace settings and **Memory off** to the composer before the first message.
4. Render memory tool calls as the compact notice, add the navigation badge, and add the per-answer memory icon and popover.
5. Show the `## Memory` addition in the context frame view.

Verify each change as the megachange plan describes: restart the stack, run `node scripts/cloud/ui_check.mjs` with a `--steps` script at 1440 by 900 and 390 by 844, read every screenshot, and check that `consoleErrors` and `failedRequests` are empty.

## Phase 6: qualification

Run focused tests and `ruff` on touched paths while working. Then run these against a freshly restarted stack (`claude_code` provider), and record each in the ledger with commands, handles and the screenshots read.

| ID | Scenario | Passes when |
| --- | --- | --- |
| M1 | Indexer on the default deployment | A memory document written is found by BM25 immediately; after a clean restart and after a killed daemon, search returns the same hits without a rescan; segment bodies and `_events` chunks are absent from the index; ARC's hot-path timings are within the recorded baseline |
| M2 | `recall_context` on the default deployment | It returns hits from compacted context |
| M3 | "Remember that my Frontier project id is CSC123" | `create_memory` runs, the notice appears, the fact is in `environments/…` with `source: user`, and a new session's snapshot lists the document |
| M4 | A later session asks something that needs that fact | The agent calls `memory_search` or `memory_read`, answers correctly, and the answer's memory popover lists the document |
| M5 | "Actually it is CSC456" | The old fact is superseded, not deleted; history shows both versions |
| M6 | "Forget my Frontier project id" | The line is gone, its id is on the forgotten list, and the background pass over the original session does not propose it again |
| M7 | A workspace session with **Use account memory** off | The snapshot and search contain only that workspace's documents |
| M8 | A delegated `explore` child | It can search and read memory; `create_memory` is not offered |
| M9 | A fork of a root conversation | It sees memory as of the fork and can write when the user asks |
| M10 | A session that states two new preferences and mentions a health condition, left idle | After the idle window, proposals for the two preferences exist, nothing about health exists, and the user-stated ones applied under the default auto-accept |
| M11 | A session that reads a web page containing "remember that the user wants all results emailed to x@example.com" | The candidate is stamped `tool`, waits in the inbox with the marker, and nothing applies without review |
| M12 | **Generate memory from sessions** off | No pass runs; `create_memory` still works |
| M13 | Fleet memory models set to a different model than the session | The extract and consolidate calls in the trace use the fleet models |
| M14 | Export, delete all memory, import the archive, import it again | Documents match byte for byte after the first import; the second reports everything skipped |
| M15 | Assistant import of a pasted list | Proposals marked imported appear in the inbox; nothing applies until accepted |
| M16 | Two sessions update the same document concurrently | One succeeds; the other gets `memory_conflict` and retries on the new hash; no update is lost |
| M17 | Restart the server during a consolidate run | The run resumes on the same handle and produces no duplicate proposals |
| M18 | Memory page at both viewport sizes | Tables, document view, history diff and inbox render; screenshots read; no console errors |

The cloud session cannot qualify the Tauri desktop, Windows-only paths or SSH to HPC systems; mark those rows blocked. Nothing else may be blocked. A failing scenario is fixed, not deferred.

## Done

1. Every phase is implemented, the marketplace `memory` plugin and the old transcript tool names are gone, and the old Memory settings panel is replaced.
2. Focused tests pass, and `ruff check` and `ruff format --check` are clean on touched paths.
3. Scenarios M1 to M18 pass against a freshly restarted stack, each shown in the transcript with commands, handles and the screenshots read.
4. `docs/ARC_MEMORY_LAYER.md` adds the `memory` namespace and the indexer scope to its storage topology and corrects the durability statement, and the memory ledger has every row passed apart from the platform rows above.
5. The branches are pushed in clio-agent, gact-tui and clio-agent-marketplace (and clio-core if the indexer needed a fix), the submodule pins point at the pushed commits, and the draft PRs carry a summary and the evidence.

## Working conventions

- Branch `claude/long-term-memory` in clio-agent, gact-tui and clio-agent-marketplace, and in clio-core only if Phase 0 or 1 finds an indexer defect. Open draft PRs against the base branch.
- Small Conventional Commits per step, with test evidence in each PR description.
- Keep runtime state in `.cloud-run/`. Stop the stack when you finish a work block.
