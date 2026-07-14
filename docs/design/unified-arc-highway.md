# The Unified ARC Highway — one operation-sourced log, thin projections

**Status:** FIRST CONSUMER LIVE (2026-07-12): ReActV2 is the production expert loop — the History prefix is a projection of the materialized ARC live plane, ARC ops are the sole prefix-reset authors, full semantic-event parity, and the claude_code stateful-delta transport rides the strict-prefix wire (TTFT 2.63x rated on delta calls, from 4.4x; see #901, commit 25e8347). NEXT CONSUMER (not started — do not claim): collapse the gact message/session materializations onto the log per §4's strangler order, one consumer at a time, byte-equal proofs before any switch; the V2 slices (S2 fold seam, S5 reference discipline, classic-pin fallback pattern) are the template. · **Owner umbrella:** [#893](https://github.com/iowarp/clio-agent/issues/893) step 5, executing [#737](https://github.com/iowarp/clio-agent/issues/737)
**Scope:** internals refactor under a FROZEN external contract. No `src/` in this slice.
**On the record (owner, 2026-07-11):** *"this unification has been attempted before and failed."* Consequently: design → adversarial review → projections proven **equivalent** (per-surface normalization spec, §4.1) against the live stores on real sessions → **one consumer migrated at a time** → **no big-bang cutover.**

> This revision closes an adversarial review. Every "confirmed finding" from that
> review is answered **in this doc** (a design that needs oral tradition has failed).
> The two structural corrections the review forced are called out up front because
> they change earlier drafts' central claims:
>
> - **C1 — the substrate does NOT "already exist" for free.** The collapse needs
>   (a) a *raw (non-op-logged) append lane* for the canonical scope to avoid a
>   documented `record → op_logger → arc.op → record` recursion (§2.2, §2.9), and
>   (b) **new atom kinds** — a message/part-level family carrying the wire identity
>   (`msg_`/`part_` ids, `created_at`, `stream_source`, usage, `expert_handoff`) that
>   today lives ONLY inside the `final_message` byte-copy the migration deletes
>   (§2.3, §2.8.c). Earlier "no schema migration required" was wrong: no migration of
>   *existing* records, but the target log carries strictly more kinds than today.
> - **C2 — write-path steps and read-path steps have DIFFERENT proof harnesses.**
>   A diff over already-captured sessions can only validate *read-path* changes; a
>   change to what gets *written* (step-1 fold, redaction-at-ingest, part atoms —
>   including step 5's recorded echo-collapse op — streaming atom) escapes it and
>   needs a **live dual-run A/B** harness
>   (§4.1.B). Conflating the two is how F3 (silent divergence) returns.

---

## 0. Thesis in one paragraph

Today the conversation/trajectory is built as **~4 parallel materializations** from **~2.5 source representations** with **three different cleaning operations** (filter vs fold vs assemble) — the [#737](https://github.com/iowarp/clio-agent/issues/737) thesis, quantified by the #893 memory profile at **~5.1 MB on disk per session across two subsystems** and **~2.36 MiB resident RAM per session, unbounded** (`893_memory_profile.md` Q3/Q4). This doc pins ONE canonical **operation-sourced log** — the *minimum common product* of every current view — and makes each of the four stores a **projection = f(log)**. Content atoms and context **operations** (`delete`/`summarize`/`report_tokens` recorded action→result) both live on the log; the **context view** is the log with operations *applied*, the **trace view** is the log with operations *visible*, and reproducibility is *replay the log → identical context at every step* (owner ruling, 2026-07-11). The win (memory + the #731/#732/#733/#736 drift-bug class becoming structurally impossible) lands ONLY if projections carry **references, not byte-copies**, are **bounded/re-derivable** (#737 catch 4), and the **ingest write becomes must-succeed-or-typed-fail** (§3.4) — because once a scope is the ONE copy, a dropped write is permanent loss in *every* view at once.

The substrate is *close* but not free: ARC's `SegmentStore` already carries (a) content segments, (b) a reserved `_events` semantic-event log (`arc/live.py:71`, `EVENTS_SCOPE`), and (c) an `op_logger` that records every context op action→result (`arc/segments.py:814-874`). The unification collapses a parallel write into a fold of that log — but it MUST add a raw append lane, new part-atom kinds, an ingest-reliability contract, and a per-surface equivalence spec, and it MUST NOT add a fifth store (RULE 4). Those four gaps are exactly where the last attempt died; they are designed here, not deferred.

---

## 1. The frozen external contract, enumerated precisely

These surfaces are the campaign's **acceptance tests**. The design may change **nothing** observable about them — with the single, explicitly-owned exception of §6.25 compaction's internal *archive* semantics (Q6), whose *wire* stays byte-identical. Internals change freely underneath; equivalence PROVEN before any consumer switches (same discipline #880 used: the render never changed, the stream got honest).

| # | Frozen surface | Precise semantics | Where its acceptance test lives / must be created |
|---|---|---|---|
| 1.1 | **SSE wire event vocabulary** | The exact set of `data.type` values clio may emit — the normative fenced block, `§7.3a ∪ §7.3b` restricted to concrete types (`SPEC.md` §7.7, lines 2035-2112). | **Exists but does NOT detect a DROP.** `apps/core/tests/spec_vocabulary.test.ts:67-89` is *static* set-equality between the SPEC block and `WIRE_EVENT_TYPES` (no server emission). `contract/conformance/vocabulary_checks.go::Drift_EventVocabulary` asserts every *observed* live type ∈ block but is opt-in (`conformance/README.md:56`, "empty SpecPath ⇒ skipped") and only catches an *added* type. If the migration silently stops emitting `arc.op` or `session.compacted`, **both pass.** Drop-detection is the job of the CREATE harness in 1.2 (a golden that asserts the *presence* of each expected type on a captured session). |
| 1.2 | **SSE streaming shape** | Flow per turn: `message.created`(flat `Message.to_wire()`) → `message.part.added` → N× `message.part.delta`(`{text_append}`) → `message.part.completed`(**`final_text` authoritative**) → `message.completed`; batch fallback carries `stream_source:"batch"` + `stream_fallback` reason; per-turn terminal invariant **`message.completed` exactly one per turn EXCEPT the ask-user pause (none — §6.23, see 1.16)** (§7.4, §7.4a, §7.5, lines 1871-1948). | Conformance suite `contract/conformance/`. **CREATE:** a captured-session golden diff harness (`sse.log`) **plus** the wire-type *presence* set from 1.1. NOTE: this is a **normalized** diff, not byte-for-byte — `part.delta` boundaries, event ids, `created_at`/`duration_ms`/`tokens`/`cost_usd` are non-normative (SPEC §7.4a makes `final_text` authoritative); the normalization spec is §4.1.A. |
| 1.3 | **`GET /messages` reload** | `{messages: Message[] newest-first, next_cursor}` with `before`/`limit`/`include_system`; may carry one live in-flight assistant message; **"reload is byte-identical to the live stream"** via `to_wire()` (§6.3, lines 843-891, esp. 880-881). | `tests/test_gact/` for `routes/messages.py`. **CREATE:** a **reload==live** equivalence test — the assembled-by-reference projection's `to_wire()` must equal the live stream's `final_text`-authoritative content for the same turn (persistence gate, §4.2 step 5). Persisted-file comparison is normalized (the on-disk form is `model_dump(exclude_none=True)`+`json.dumps(indent=2,sort_keys=True)`, `messages.py:100-103` — NOT `to_wire()`; §4.1.A). |
| 1.4 | **Context semantics (what the agent sees)** | The compiled working set the ReAct loop reads each iteration: `render_working_set` render-ordered `(order, logical_time)`, tombstone-aware, per-scope (`arc/segments.py:876-905`, `arc/memory.py:1105`); plus RULE 6 filter→compact→enrich→assemble cross-turn enrichment (`arc/context_compiler.py:75-124`, distinct from the working-set fold — #737 catch 2). | `tests/test_arc/`. **CREATE:** a **golden-prompt** equivalence test on captured sessions **and** (because step 1 is a *write-path* change, §4.2) a **live dual-run A/B** on exotic/error inputs (§4.1.B) — the captured corpus cannot validate a write-path change (C2). Behavioral backstop: `grind-clio-case` ≥0.8. |
| 1.5 | **Tree / context searchability** | `GET /messages/search` → `SearchMatch[]` (gated, §6.3 line 853); `GET /v1/memory/search` (`MemorySearchResponse`, §6.19); ARC scope search `search_segment_scopes` + per-scope `.search` companions (`arc/segments.py:411-429`); `retrieval.py::ContextRetriever` ranking. `_events` is EXCLUDED from search text (`arc/live.py:102-109`). | `tests/test_arc/` + `tests/test_gact/test_fork_and_search.py`. **CREATE:** a test that the derived index (§2.7) reproduces identical `SearchMatch` ordering/scores on captured sessions AND that step 1 does not orphan the index writer (§2.7 names its new owner). |
| 1.6 | **Trace reproducibility** | The durable trace = the event log with operations visible; a run is **fully reproducible** by replay. `arc/replay.py`; the semantic spine + `x_clio_semantic_trace_backend` (`none`/`file`/`factory`) govern durable capture (§7.6). | `tests/test_arc/` replay. **CREATE:** a **replay-equivalence** test — replaying the log yields the identical context at every `logical_time` (the *definition* of the trace view; strongest single guard). |
| 1.7 | **Context operators + `arc.op` wire** | The ops `append`/`insert`/`delete`/`summarize`/`replace` (`arc/segments.py:465-812`); `arc.op` is served ONLY for context-*mutating* ops (plain `append` stays OFF the wire — `globals.py:678`) and carries an **allow-listed payload** `{id, kind, token_count}` per segment, **never content/args/text** (`globals.py:693-700`). `report_tokens` is a first-class action→result event. | `tests/test_arc/` op semantics. **The vocabulary tests (1.1) assert type NAMES only — they do NOT test the payload allow-list or the append-suppression.** **CREATE:** a served-payload test that `arc.op` frames carry only `{id,kind,token_count}` and that plain `append` is not served (today enforced only in code, `globals.py:678,693-700`; grep shows no test asserts the served payload shape). |
| 1.8 | **Compaction** | `POST /compact` → replaces the visible ledger with ONE synthetic `type="compaction"` part (`summary`, `compacted_message_ids`, `auto=false`), carrying `msg_compact_*` id + `memory_event_id`; emits `session.compacted` + a `memory.compacted` semantic event (§6.25 lines 1664-1695; part shape §4.5 line 532). | Compaction route tests. Under the log model this becomes a `summarize` op *appended* (§2.5); the wire (`session.compacted`, the part, its ids) must stay byte-identical. See Q6 (archive semantics move; wire frozen). |
| 1.9 | **Delegation / `expert_handoff` envelope** | Terminal part of a delegated turn carries `metadata.expert_handoff` = **`output`** (child's answer BYTE-FOR-BYTE, never server-authored) + **`workflow_state`** (typed dict); one `delegate.started` + one `delegate.completed` per delegation, never deduped by the client (§4.5 lines 553-599). | Delegation tests; #880 baseline-0 CI guard that no code authors text into a model-output field. A byte-copy→reference migration MUST preserve `output` verbatim; `workflow_state` is recorded action→result (§2.8.d, schema-version-pinned). |
| 1.10 | **Memory stats** | `GET /v1/memory/stats` → `MemoryStats` incl. `cache{hits,misses,hit_rate,capacity}`, per-session `{messages_retained, tokens_retained, tokens_budget, token_pressure, threshold_state, compaction_recommended}`, `global{conversations_total, invocations_total}` (§6.19 lines 1459-1509). | `tests/test_gact/` for `routes/memory.py`. `token_pressure`/`tokens_retained` derive from folded `token_count` — the fold must reproduce the ~4-chars/token heuristic over byte-identical content (`arc/live.py:152-156`) or these drift (§2.8.b caveat c). |
| 1.11 | **Message-ledger mutations** | Undo/rewind (§6.2:787-818): event order per rollback = per-message `message.deleted` → `session.undo`/`session.rewind` → `session.updated`; `memory_scope:"gact_visible_transcript_only"` — undo deletes the gact transcript but **NOT ARC memory** (`routes/sessions.py:342,365`). `DELETE /messages` (§6.3:850-851); `session.cleared` (§7.3a). **Fork** (§6.2:782): INCLUSIVE truncation, no settings inheritance. **Export/import** (§6.2:785-786): blob `{version:"1", session, workspace, messages, context_files}`. Today served by mutable-ledger primitives `MessageStore.replace_session`/`delete_session` (`messages.py:59-75`). | **Exists, uncited before:** `tests/test_gact/test_session_rollback.py`, `test_message_delete.py`, `test_rollback_body_parsing.py`, `test_fork_and_search.py`, `test_session_export.py`. These gate §2.5's **message-level delete/tombstone op** — the append-only log needs an explicit transcript-scoped tombstone op distinct from the ARC-memory `delete` (their `memory_scope` differ). |
| 1.12 | **SSE subscription / resume semantics** | §7.1:1714-1735: preamble always `id 0`; real ids ≥1 **from a single process-global counter shared across sessions and global events** (`itertools.count(1)`, `events.py:47-54`) — strictly ascending per session but non-contiguous; replayed events keep their ORIGINAL id + `replay:true` (`events.py:83-94`); heartbeats never recorded (`transient`, `events.py:77-81`); global events (`session_id=""`) merge into every session's replay by id. Buffer = 256 events/session (`events.py:131`). | **Exists, uncited before:** `tests/test_gact/test_sse_resume.py`. This surface is **OUT of the log's derivation scope** — see §2.8 cross-cutting (the resume buffer stays a transport-plane bus structure; `Last-Event-ID` is a bus sequence id, NOT `logical_time`). |
| 1.13 | **`GET /v1/metrics`** | §6.16 counters. Today `metrics_counters` is "seeded once from the loaded ledger [`load_all()`] then kept live by the session_store write seams" (`app.py:1294-1299`). Removing `load_all` (§3.1) removes the seed source **by construction**. | **Exists, uncited before:** `tests/test_gact/test_metrics.py`. Derivation: counters = fold over the boot **index** (which carries per-session counts) + the live write seams — never a `load_all` re-walk (§3.1, Q7). |
| 1.14 | **Diffs** | §6.9/§6.10 + §4.5:529: the persisted Part's status is **frozen at `"pending"`**; apply/reject mutate only the §6.10 diff rows + emit `file.diff.*`; **`GET /messages` never reflects apply state** — `GET /diffs` + `file.diff.*` are authoritative. A clean re-derivation would "helpfully" serve the *current* status = a byte-level reload break the harness must be told to PRESERVE. | **Exists, uncited before:** `tests/test_gact/test_diffs.py`. The part atom (§2.3) freezes `status="pending"` at write time; apply/reject are separate diff-row events, never folded back into the message projection. |
| 1.15 | **Context-file ledger** | §6.2:781 delete cascade: `DELETE /sessions` "cascades: messages, context-file ledger, ARC footprint"; export blob carries `context_files`. | **Exists, uncited before:** `tests/test_gact/test_context_files.py`. The context-file ledger is a projection with its own delete cascade (§2.6 retention). |
| 1.16 | **User questions + turn-retry attempts** | §6.23: ask-user pause emits **NO `message.completed`** (the 1.2 terminal-invariant exception); `user_question.resumed` queues a user message `{question_id, session_id, queued_user_message_id, source_turn_id}`. §6.24: `GET /attempts` → `TurnAttempt[]` + five `turn.retry_*` lifecycle events carrying the full flat `TurnAttempt`. Both ride session/turn state the projections replace. | **Exists, uncited before:** `tests/test_gact/test_ask_user_retry.py`. A replay that assumes one terminal per turn mis-projects paused turns; the log's turn-lifecycle atoms must model the pause (no terminal) and the retry-attempt rows explicitly (§2.1 content-event taxonomy). |
| 1.17 | **SSE-served semantic-event subset** | §7.6:1980-2007: only `react.step.completed`, `expert.extract`/`response.completed`, `expert.lifecycle.started`, `delegation.*`, `memory.search.completed`, **plus any event with status `failed`/`error`/`cancelled`** reach SSE; everything else (turn/agent/hook lifecycle, `tool.call.*` mirrors, `lm.token.delta`, `memory.compacted`, `arc.op`) is captured-not-served. `semantic` detail redacts genuine credentials only and KEEPS reasoning on `react.step.completed`/`expert.extract.completed`. | **Exists, uncited before:** `tests/test_gact/test_semantic_events.py`. The unification moves this exact boundary (events become log records; *serving* becomes a projection): the served-subset allow-list + detail redaction is a frozen wire surface, pinned by this test, not re-decided in the projection. |

**Invariant for the whole design:** every number, byte (normalized per §4.1.A), and ordering visible through 1.1–1.17 is held constant. The proof obligation is a *diff against real sessions* under the §4.1 normalization spec — captured-corpus for read-path changes, live dual-run for write-path changes (C2) — not a re-derivation argument.

---

## 2. The canonical store — the operation-sourced log (minimum common product)

### 2.1 What it is

ONE append-only, normalized log per session, holding two event families under a **single order key**:

1. **Content events** — the normalized atoms of *what was produced*: user input, thinking, text, `tool_call`, `tool_result`, answer, error, `file_diff` (`status="pending"` frozen, 1.14), `routing_decision`, delegation start/return, **message/part-level atoms** carrying wire identity (new — §2.3), turn lifecycle **including the ask-user pause (no terminal) and retry-attempt rows** (1.16).
2. **Context operations** — *what was done to the context*, recorded **action → result**: `insert`, `delete` (ARC-memory tombstone), **`transcript_delete`** (gact-visible-transcript tombstone for undo/rewind/`DELETE`/`clear`, `memory_scope:"gact_visible_transcript_only"` — 1.11), `summarize` (fold N→1), `replace` (1:1), `report_tokens`, `reset` (per-turn working-set reset), `fork` (inclusive truncation marker, 1.11), `state_merge` (workflow_state result, §2.8.d). Each records target ids, produced atom id(s), and `derived_from` provenance. **`append` is NOT an op record** — an atom's own presence + `logical_time` + position *is* the append (this is what keeps the log the minimum common product; §2.6 resolves the "op-record-IS-atom-or-duplicates" question).

This is the *minimum common product*: the smallest event set from which **every** view in §2.8 is derivable. It is **operation-sourced**, not a content-snapshot store — any design where compression/deletion silently *rewrites stored content* fails reproducibility by construction (owner ruling).

### 2.2 It substantially exists — but needs a raw append lane, part atoms, and an ingest contract

| Canonical-log ingredient | Where it lives TODAY | Gap to close |
|---|---|---|
| Content-event stream | ARC `_events` scope: `semantic_event` segments, VERBATIM (`arc/live.py:236-293`, `112-144`) | Written from the semantic bus in parallel to the working-set segments. Make the working-set a *fold of it* (§2.8.b) — a **write-path** change (C2). |
| **Message/part wire atoms** | **NOT on the log.** `turn.started` logs only `{text}`+`subject.message_id` (`turn.py:238-249`); the full assistant `Message` exists only inside `final_message` on `turn.completed` (`turn_finalize.py:615-622`) — the byte-copy step 5 deletes | **NEW atom kinds** carrying `msg_`/`part_` ids, `created_at`, `stream_source`, usage, `expert_handoff`, `msg_compact_*`+`memory_event_id`. Minted once, stored durably, so eviction+rehydration reproduce them (§2.3, §2.8.c). |
| Context-operation stream | `op_logger` fires on every `apply` (`arc/segments.py:814-874 _finish_write`) → `arc.op`, routed DIRECTLY to sink+bus, **not** through `arc.record` (`globals.py:627-633`) precisely to avoid the `record→op_logger→arc.op→record` recursion | Op records become first-class log atoms via **explicit `record_op(...)` calls at the mutation sites** on a **raw append lane** — NOT the `_finish_write` callback (which would re-form the recursion). See §2.9. |
| Single order key | `logical_time`, store-assigned monotonic (`arc/segments.py:445`; `schema.py:119`) | Collapse `sequence`/`logical_time`/event-id (#737 Consequence 1) onto `logical_time`. |
| Identity / scoping | `Segment.id` + `session_id`/`turn_id`/`expert_span_id`/`run_span_id` (`schema.py:143-156`) | Populate span ids at the write path (currently `""` defaults). |
| Redaction | credential redaction at read time (`SPEC.md` §7.6) — secrets DO reach the store today (`globals.py:620-621`) | Move genuine-credential redaction to the **write path** (Q3) so no secret reaches any consumer; keep `detail_level` a read-time filter over already-safe content. This also unblocks the corpus (§4.1.C). |
| **Ingest reliability** | Every producer is **best-effort-with-swallow**: `LiveRuntimeContext.fold` bare `except: pass` (`live.py:274-277`); working-set append logs+continues (`runtime.py:168-174`); delegation writes likewise (`turn_delegation_arc.py:76-78,129`) | Once the log is the ONE copy, a dropped write is permanent multi-view loss. **Promote to must-succeed-or-typed-fail** in the SAME step that removes each old write path (§3.4). |
| Chunked O(chunk) append | `_events` chunk family (`arc/live.py:74-109`, `arc/memory.py:886-904`) | Keep as the append primitive; but §2.10 bounds its concurrency/read cost. |

**External operators (owner ruling, 2026-07-12 — load-bearing for Q1):** an entire context-management
system EXTERNAL to the agent operates by communicating with **clio-core** directly:
`external operator -> operation() -> context_blob -> agent sees effect`. Therefore:
(a) op records carry **actor attribution** (agent-originated vs `external_operator`) — an external
op is a first-class action→result log atom like any other, or reproducibility breaks exactly where
the external system touches the context; (b) the in-process context projection is NOT the only
writer's view — it must **observe externally-applied ops** (the "agent sees effect" half): the
projection checks a log epoch/version at defined points (turn boundary at minimum; a clio-core
change-notification if/when available) and folds any externally-appended ops before composing
context — this is an epoch CHECK on the hot path (cheap), never a fold-from-scratch; (c) this is
why clio-core stays the DEFAULT log home (§5.2 Q1): the external operators speak to clio-core —
a file-backed default would leave them nothing to operate on. Under the loud LocalFS degradation
(#897) the external-operator pathway is unavailable and the degradation row must SAY so.

**Consequence:** the canonical store is the ARC event log **extended (part atoms, transcript ops, raw lane, ingest contract) and unified** on the existing `SegmentStore → ARCStore → CTE/LocalFS` seam. It adds NO new store (RULE 4).

### 2.3 Schema (the normalized atom + op record) — new kinds, no migration of old records

Reuse `Segment` (`arc/schema.py:103-156`) as the physical record; the canonical log is the `_events` chunk family holding:

- **content atom** — existing kinds (`SegmentKind`, `schema.py:61-74`) PLUS a **new `message_part` family** whose `content` carries the full wire fields: `{message_id, part_id, created_at, role, kind, stream_source, usage, expert_handoff?, compaction?{msg_compact_id, memory_event_id}, status}`. `file_diff` parts freeze `status="pending"` here (1.14). These ids/timestamps are **minted once at turn time and stored** — re-derivation after eviction reads them, never re-mints, so 1.3/1.8 identities survive rehydration.
- **op record** — a `semantic_event`-kind atom whose `content` is `{op, scope, target_ids, produced_ids, derived_from, logical_time, token_count?, memory_scope?, schema_version?}`, written on the raw lane (§2.9).

**No migration of existing records** (additive fields decode with `""`/`None` defaults, msgspec back-compat, `schema.py:150-153`) — but the target log carries strictly **more kinds** than today (the `message_part` family and the `transcript_delete`/`fork`/`state_merge` ops). The earlier "no schema migration required" claim is retracted (C1): what is required is *additive new kinds*, provisioned before the read consumers that depend on them.

### 2.4 Ordering, identity, scoping

- **Order:** `logical_time` (store-wide monotonic). Chunk order == global order (a chunk fills to capacity before the next opens, `arc/live.py:297-307`), so concatenated `render` across chunks equals a single-scope render.
- **Identity:** `Segment.id` is the stable op target; message/part ids live *in the atom content* (§2.3).
- **Scoping:** `session_id` (partition) / `turn_id` / `expert_span_id` (disambiguates OVERLAPPING concurrent experts) / `run_span_id` (`schema.py:150-156`).

### 2.5 Operations as action→result

| Op | Recorded as | context view (apply) | trace (keep) | SSE |
|---|---|---|---|---|
| `append`/`insert` | atom on log (append) / op-record (insert only) | atom enters working set | visible | atom's content event streams; plain `append` NOT served as `arc.op` (`globals.py:678`) |
| `delete` | tombstone target ids, `memory_scope:"arc"` (`segments.py:597-626`) | render skips tombstoned | visible | dropped |
| **`transcript_delete`** | tombstone target *message* ids, `memory_scope:"gact_visible_transcript_only"` (undo/rewind/`DELETE`/`clear`) | gact transcript projection skips them; **ARC memory untouched** (`routes/sessions.py:342,365`) | visible | `message.deleted`→`session.undo`/`rewind`/`cleared`→`session.updated` (1.11) |
| `summarize` | delete(ids)+insert(summary), `derived_from`=replaced ids (`segments.py:628-711`) | working set shows summary | folded + originals visible | ONLY `session.compacted` marker (§6.25) |
| `replace` | tombstone original + new at slot, 1:1 provenance (`segments.py:713-791`) | render shows new | both visible | new content event streams |
| `fork` | inclusive-truncation marker at target id, no settings inheritance (1.11) | new session projects atoms ≤ target | visible | `session.created` for the fork |
| `report_tokens` | token-accounting event (NEW) | feeds `MemoryStats.tokens_retained` | visible | not served (feeds §6.19 polling) |
| `reset` | per-turn working-set reset (NEW) | working-set cleared for the turn | visible | not served |
| `state_merge` | workflow_state RESULT + inputs + `schema_version` (NEW, §2.8.d) | typed dict on message/rows | inputs + result visible | not served |

**as-of-T reproducibility** is already implemented: `render(..., as_of=T)` returns atoms ≤T not tombstoned at T (`segments.py:876-905`). Replaying to any `logical_time` yields the identical context — the trace view and 1.6's acceptance.

### 2.6 Storage, retention, and erasure

- The log lands on `ARCStore` (`arc/storage.py`): **clio-core** (the ClioCoreStore backend — CTE is clio-core's tiering component) is **today's default** (`make_arc_store` default="cte"); **LocalFS** msgpack chunks only on explicit `CLIO_ARC_STORE=local`. Offload through clio-core is proven sha256-identical (`tests/test_arc/test_clio_core_offload_spill.py`). Canonical-store residency is clio-core's tiering job (owner ruling); LocalFS keeps the O(chunk) append as the compatibility floor.
- **Size re-quantified.** The old `arc.op` embedded FULL segment dicts (`globals.py:660,693-700`) so a *separate* trace could replay without prior state — potentially 2× every atom's bytes. On the **unified** log this duplication is **removed**: atoms and ops share ONE ordered stream, so op records **reference atom ids, never embed bytes**, and replay-without-prior-state still holds because the referenced atoms *precede* the op on the same log. Net on-disk: content atoms once + small op records (ids only) ≈ today's 3.68 MB `_events` copy **minus** the retired 1.25 MB gact-messages copy and 0.17 MB working-set copy = a *reduction*, not the growth the naive "op-records-carry-full-dicts" reading implies. The harness (§4.1) records the measured per-session bytes against the 5.1 MB baseline as an exit metric.
- **Retention / erasure** (the append-only tension the review flagged):
  - `DELETE /sessions` cascade (1.15, §6.2:781) = drop the session's log partition + its projections (messages, context-file ledger, ARC footprint). The log partition is a chunk family; dropping it is `drop_scope` over the family (`live.py:311-324`) — a *partition delete*, not an in-log rewrite, so append-only-within-a-session is preserved.
  - **Secret erasure.** Because redaction moves to the **write path** (Q3), a genuine credential never lands on the log — so append-only + no-in-log-erase is sound. For pre-Q3 data, `DELETE /sessions` partition-drop is the erasure path; there is no requirement to surgically excise a byte from a retained partition.
  - **LocalFS growth** is bounded by the same `DELETE`/retention TTL that bounds today's `messages/*.json`; the log does not grow *faster* than the copies it replaces (it replaces them). clio-core installs offload past the hot cap.

### 2.7 The derived search / tree index — who writes it after step 1

The search surface (1.5) is a **derived index over the log**. TODAY the per-scope `.search` companion is written at *scope-persist time* (`segments.py:411-429`), and the `_events` family is search-EXCLUDED (`live.py:102-109`). **Step 1 removes scope-persist** (working set becomes a fold), which would orphan the index writer. Resolution: the index becomes an **ingest-time projection** — the raw-append lane feeds a derived indexer that maintains the `.search` companion + the invocation `BTreeIndex` (`arc/memory.py:214`) as content atoms land. It is re-derivable (drop+rebuild) and inherits the projection residency policy (§3), NOT a separate lifecycle. This is an explicit sub-task of step 1's exit criterion (§4.4), not an afterthought.

### 2.8 Derivation: view = f(log), for EACH of the four current stores

**(a) ARC semantic event log** (`_events`, the 3.68 MB largest copy) — **BECOMES the canonical log.** No derivation; it is the floor. It absorbs the op stream, the message/part atoms, and the single order key; it stops being one of four copies and becomes the one.

**(b) ARC live plane / working-set segments** — **`context view = fold(log)`** via `render_working_set` (`segments.py:876-905`). This is a **write-path** change (C2), and the review identified four ways the log-as-written-TODAY does not contain the fold's inputs byte-exactly. Each must be closed IN step 1 (not discovered mid-migration):
- **(caveat a) representation unify.** Working-set stores `_arc_obs_value(obs)` (JSON-natives kept, exotics via `str()`, `context_tokens.py:30-38`) while the react-step event stores through `_encode_safe` (recursive pydantic/dataclass/set/tuple coercion, `live.py:112-144`). For any non-JSON-native tool output these differ, so a fold cannot reproduce the segment bytes. **Step 1 unifies the encoder at ingest** — one coercion function feeds both, retired to a single path. Validated by the **live dual-run A/B on exotic outputs** (§4.1.B), because happy-path captures hide this (the single-fixture-over-generalization lesson, on record).
- **(caveat b) failure paths.** `thought`/`tool_call` segments are written BEFORE the tool runs (`runtime.py:269-279` precedes execution at 284 precedes the react-step emit at 308); a crash/hang mid-step leaves the old working set with segments the log has no step-event for. **Step 1 emits a `step_open` atom at step entry** (mirroring the pre-execute segment write) so the fold has the same pre-execution atoms on error paths. Validated by dual-run on injected tool crash/timeout.
- **(caveat c) token accounting.** Segment `token_count` is the ~4-chars/token heuristic over the content dict at write time (`runtime.py:150-156`). The fold must reproduce it over byte-identical content or `MemoryStats.tokens_retained`/`token_pressure` (1.10) drift. Unified encoder (caveat a) makes the content byte-identical; the fold re-runs the SAME heuristic.
- **(caveat d) precedent is weaker than claimed.** `LiveRuntimeContext`'s current projections are deliberately LEAN (`reasoning_len` is a length, tools reduced to `{name, ok}`, `live.py:508,537-543`) — no current consumer is byte-exact. Step 1 is the FIRST byte-exact fold; the "substrate already exists" claim is scoped accordingly (C1).

**(c) gact sessions + messages** — **`persistence = assemble(message/part atoms) by reference`.** A `Message` is grouped part atoms rendered `to_wire()` on read; the persisted projection stores atom ids, not bytes — killing the `final_message` byte-copy (#737 catch 4). **The wire identity (`msg_`/`part_` ids, `created_at`, `stream_source`, usage, `expert_handoff`, `msg_compact_*`+`memory_event_id`) lives in the `message_part` atoms (§2.3), minted once and stored** — NOT in `final_message` (which step 5 deletes) and NOT re-minted on rehydration. This is why the part-atom family (write-path) must land BEFORE persistence-by-reference (read-path) and BEFORE `final_message` is removed (§4.2 ordering). `reload == live` because both the SSE spine and `GET /messages` project the SAME atoms. User messages get their own part atoms too (today `turn.started` logs only `{text}`+id, `turn.py:238-249` — insufficient; the new user `message_part` atom carries parts/`created_at`/metadata).

**(d) workflow_state carriers** — **`workflow_state = the recorded RESULT of a `state_merge` op`.** `_merge_workflow_state_mapping` is parameterized by the **live pack's** `WorkflowStateSchema` (`rank`/`normalize_section`/`sticky_true_fields_for`) and is order-sensitive (`merge.py:76-121`) — so a re-fold under a *newer* pack schema yields a *different* dict, breaking §4.5 `workflow_state` bytes. Per the owner's action→result ruling ("a compress event records what was folded and what it produced"), the merge **records its RESULT** as a `state_merge` op with `{inputs, produced, schema_version}` (§2.5). Re-derivation replays to the recorded result, never re-folds; `schema_version` pins the semantics. Materialized onto the message and both delegate rows (§4.5:563-572).

**Cross-cutting derivations the migration must preserve:**
- **compaction** = a `summarize` op applied (context view), kept (trace), surfaced as `session.compacted` (SSE) — §2.5, §6.25, Q6.
- **`reload == live`** (§6.3) = both the SSE spine and `GET /messages` project the *same* part atoms → the drift-bug class (#731/#732/#733/#736) is structurally impossible **at the END state**. The ordering in §4.2 is designed so the interim NEVER holds two independent representations (§4.2 note).
- **SSE resume cursors are NOT a projection of the log.** The 256-event replay buffer is a **transport-plane bus structure** (`events.py:47-92,131,270-311`) whose ids are process-global sequence integers reset on boot, and whose contents are a SUPERSET of log records: `message.part.delta` is skip-listed from the log (`_EVENT_LOG_SKIP`, `memory.py:70`) and `permission.requested`/`session.status_changed`/global `lm.provider.*`/`mcp.server.*` have no log record (some are session-less, `memory.py:824-834`). A "tail-window over the log" cannot re-emit events the log never held, nor reproduce delta chunk boundaries. **Design statement:** the resume buffer stays an independent bounded bus buffer, OUT of the log's derivation scope; `Last-Event-ID` maps to the **bus sequence id, NOT `logical_time`**; the process-global id space and the global-event merge (1.12) are preserved unchanged. The earlier "maps to logical_time" claim is retracted — it would break §7.1 observably. (This is also §5.1 non-goal: the transport plane is #891's front.)

### 2.9 The raw append lane (avoiding the documented recursion)

`arc.op` is deliberately routed DIRECTLY to the sink, NOT through `arc.record`, because feeding it back re-enters the op-logger (`record persists a segment → op_logger → arc.op → record …`) — "the circularity that previously forced a thread-local re-entrancy guard" (`globals.py:627-633`; `memory.py:65-70`). Putting op records ON the log naively (append via `SegmentStore.apply` → `_finish_write` → `op_logger`) **re-forms exactly this loop** — the strongest candidate for "where the last attempt died."

**Design:** the canonical `_events` scope is written by a **raw append lane** (`append_raw`) that **never invokes `op_logger`**. Consequences:
- **Content atoms** and **op records** are both appended raw — a write-to-the-log never generates a write-log-of-that-write. There is no `_finish_write` on the canonical scope.
- **Op records are produced by explicit `record_op(delete|summarize|replace|reset|report_tokens|transcript_delete|fork|state_merge, …)` calls at the mutation sites** (compaction, per-turn reset, dedup, undo) — NOT as a `_finish_write` callback. The `op_logger` callback path is retired for the canonical scope.
- `append` needs no op record at all (§2.1); only the non-append ops emit one. This removes the ~190 per-turn "plain append" `arc.op` frames (`globals.py:674`) entirely.
- The served `arc.op` wire (1.7) is emitted from the SAME `record_op` calls, preserving the mutating-only + `{id,kind,token_count}` allow-list, and its **new served-payload test** (1.7) guards it.

This is the mechanism the earlier draft called a mere "gap to close." It is load-bearing and specified here.

### 2.10 Concurrency and read-cost budget

Today concurrent experts append to their OWN scopes under per-scope locks (`segments.py:323-333`); `_events` receives only lean semantic events; reads are free from the resident dict (`893_memory_profile.md` finding 2). Routing every content atom + op record of all concurrent experts through the single per-session `_events` chunk risks (a) serializing parallel experts on one lock (whole-chunk re-encode per append, `segments.py:411-429`), and (b) inverting "reads are free" — first `GET /messages` becomes a full-log load+fold (`_segs` loads the whole scope record, `segments.py:337-363`; `_turns` concatenates across chunks, `live.py:347-349`).

**Budget + mitigation (stated, not hand-waved):**
- **Write:** partition the active chunk **by `expert_span_id`** (`_events/<span>/<n>`), merged by `logical_time` on read — so concurrent experts do not contend on one lock. Ordering is preserved because `logical_time` is store-wide monotonic (§2.4). This is a sub-task of step 1.
- **Read:** first-GET rehydration is O(session log); bounded by the LRU (subsequent GETs free) and by index-only boot (§3.1) so only *touched* sessions pay. **Exit budget:** first-GET rehydration for a P-turn session ≤ the current cold `load_all`-per-session cost; measured in the §4.1 harness as a standing metric. If a fold exceeds budget, materialize a per-session compacted snapshot atom (a `summarize`-style checkpoint) so rehydration reads the snapshot + tail, not the full history — provisioned as Q8.

---

## 3. Projections — materialization + residency policy

A projection is **evictable and re-derivable**. The log holds the bytes once; projections carry references or bounded materialized views (#737 catch 4).

### 3.1 The boot fix — sequenced FIRST, independent of the collapse

The profile is unambiguous: **boot is eager and unbounded.** `build_app` calls `MessageStore.load_all()` — globs+parses *every* `messages/*.json` into `app.state.messages` before the port binds (`app.py:1293`, `messages.py:26-37`), plus `sessions._load()` (`sessions.py:178-201`) and `metrics_counters.rebuild` (`app.py:1299`) — **~2.36 MiB RAM/session, linear, no cap** (238 MiB → 1.42 GiB at 500 sessions).

**Policy:** boot loads the **INDEX ONLY** (session list + per-session offsets/counts, small) — NEVER `load_all`. A session's message projection materializes **lazily on first `GET /messages`** into a **bounded LRU + TTL** resident set. `metrics_counters` seeds from the index counts (1.13), not a `load_all` re-walk.

**Sequencing (review correction):** this is **step 1 of the strangler (§4.2), implemented against the EXISTING `messages/*.json` store**, BEFORE any unification. It is independent of the collapse, carries zero unification risk, and delivers the *entire measured memory win* on its own. Implementing it against the log (post-collapse) instead would either rebuild it at step 5 or block it on the collapse — both wrong. This is #889; this doc fixes its policy so the slice is mechanical.

### 3.2 Projection residency policy (per view)

| Projection | Materialization | Residency policy | Re-derivation trigger |
|---|---|---|---|
| **persistence** (`GET /messages`) | assemble part atoms by reference, `to_wire()` on read | **LRU(capacity)+TTL**; boot = index only | `GET /messages` / SSE attach |
| **context view** (working set) | fold ops per scope | hot LRU cap 1000 (`arc/memory.py:207`); scopes lazy (`segments.py:337`) | first scope access; **pressure/TTL eviction (NEW)** |
| **SSE resume buffer** | transport-plane bus buffer (256) — **NOT log-derived** (§2.8) | already bounded; drop-on-full | reconnect → `GET /messages` refetch |
| **search/tree index** | ingest-time derived `.search` + `BTreeIndex` (§2.7) | re-derivable; inherits session eviction | rebuild on demand |
| **workflow_state** | recorded `state_merge` result (§2.8.d) | rides the persistence projection | with the message projection |
| **context-file ledger** | projection with `DELETE`-cascade (1.15) | inherits session eviction | on access |

The two uncapped ARC structures (`_scopes`/`_loaded`, `_inv_index`) are bounded only by *lifecycle* (`release_session`, `arc/memory.py:1189`) — an idle-but-unreleased session stays pinned. #889 adds a pressure/TTL trigger that calls the existing `release_session` (write-through). **#762 subtlety the migration must respect:** `release_session` today RETAINS `_events` when the trace backend is `none` because then the log is the only copy (`memory.py:1236-1250`); under the unified model the log is ALWAYS the canonical copy, so eviction of a *projection* must never touch it — but see §4.3 for the *inverse* case (trace ENABLED erases `_events`), which governs backfill.

### 3.3 No silent fallback (projection side)

Every eviction, downgrade, or rehydration-miss emits a **typed reason** reaching the trace/API — modeled on the `stream_fallback` catalog (`gact/streaming.py`; memory `feedback_silent_fallbacks.md`). A bare `except: pass` on a projection miss is a bug even when the output looks fine (`context_compiler.py:164` already does this; extend to eviction/rehydration).

### 3.4 No silent fallback (INGEST side — the newly-fatal path)

The review's sharpest correctness point: **every producer of the canonical copy is best-effort-with-swallow TODAY** — `LiveRuntimeContext.fold` bare `except: pass` (`live.py:274-277`), working-set append logs+continues (`runtime.py:168-174`), delegation writes likewise (`turn_delegation_arc.py:76-78,129`). Today a dropped write is a cosmetic trace gap because a parallel copy survives. **Post-cutover it is permanent loss in prompt+reload+trace+search simultaneously**, and the fallback the code relies on (the local trajectory dict, `runtime.py:144-146`) is one of the copies the migration DELETES.

**Contract:** in the SAME step that removes an old write path (exit criterion §4.4.c), its canonical-log append is promoted to **must-succeed-or-typed-fail**: on append failure the runtime emits a typed `ingest_failure` reason (reaching trace+API) and **fails the turn** (or degrades to an explicit typed read-only state) — never silently continues. Until the old write is removed, best-effort remains acceptable *because the old copy is still the fallback* — the promotion and the removal land together, never apart.

**Dual-write crash reconciliation (§4.3):** during dual-write, **the log is authoritative.** If the old-store write lands but the log append fails, the turn is failed with `ingest_failure` (no half-committed state is served). If the log lands but the old-store write fails, the log is the surviving canonical and the projection re-derives; the diff harness (§4.1) flags the old-store gap but does not fail (the old store is being retired). This asymmetry is deliberate and typed.

---

## 4. Migration plan — strangler, because the last attempt died

Past attempts died these ways; each is a named guardrail:

- **F1 — accretion of a 5th store.** Mitigation: reuse the ARC `_events` chunk family + a raw lane (§2.2, §2.9). No new store/file. The legacy read path is *sunset-bounded* (§4.3), not permanent. CI ratchets (`scripts/check_file_size.py`) enforce owner-module discipline.
- **F2 — big-bang.** Mitigation: one consumer at a time (§4.2), each behind a **session-scoped** flag with **session-boundary** rollback (§4.4). No step switches two read consumers at once.
- **F3 — silent divergence.** Mitigation: the equivalence diff is a **standing CI gate**, split into read-path (captured corpus) and write-path (live dual-run) obligations (§4.1) with a per-surface **normalization spec** — because an over-strict "byte-for-byte" gate that can never go green gets hand-weakened ad hoc, which IS F3.

### 4.1 Step 0 — the equivalence harness (BEFORE any consumer switches)

The review showed a naive "projected == captured bytes" gate can NEVER be green (wall-clock fields, bus ids, non-normative delta boundaries, a persisted format that is `model_dump`+`sorted-json` not `to_wire()`, and dual-clock ambiguity during dual-write). So the harness is defined in three precisely-scoped parts:

**4.1.A — Per-surface normalization spec (what "equivalent" means).** Diffs run after this normalization; anything masked here is declared non-normative:
- **SSE (1.2):** compare the **coalesced, `final_text`-authoritative** stream (SPEC §7.4a) — collapse N `part.delta` into the completed part's `final_text`; **mask** `id`, `created_at`/`updated_at`/`occurred_at`, `duration_ms`, `tokens`, `cost_usd`; **exclude** non-logged bus types (`message.part.delta`, `server.heartbeat`, `session.status_changed`, `permission.*`, `delegate.*` timing). Assert: the set+order of *served, logged* event types and their normative payloads. Type **presence** (drop-detection, 1.1) is asserted here.
- **persistence (1.3):** compare **normalized** `to_wire()` of the projection against `to_wire()` re-parsed from the persisted `messages/*.json` (`Message(**payload)` then `to_wire()`) — NOT the raw file bytes (the file is `model_dump(exclude_none=True)`+`json.dumps(sort_keys=True)`, `messages.py:100-103`, a different serialization). Mask server-assigned timestamps only where §6.3 permits; ids/parts/`output` are NORMATIVE (verbatim).
- **context view (1.4):** byte-identical `render_working_set` output (this one IS byte-exact — it is internal, no wire masking).
- **trace (1.6):** replay-to-T == live context at T. **Clock disambiguation for dual-write:** the projection's `logical_time` is authoritative; the parallel old-store's separate clock is IGNORED by the harness (it is being retired). "As-of-T" is defined against the LOG clock only, so the dual-clock ambiguity the review raised does not arise — the old clock is never a comparand.
- Assertions use the **consumer's** semantics (JS trim ≠ py strip; memory `feedback_prove_the_invariant_on_the_real_object.md`) on the value the system SERVES.

**4.1.B — Two proof obligations (read-path vs write-path — C2).**
- **Read-path changes** (persistence-by-reference, workflow_state fold, search index) — validated by the **captured-corpus diff**: project from the existing captured log, diff against the captured live output under 4.1.A.
- **Write-path changes** (step 1 fold, redaction-at-ingest, part atoms — including step 5's recorded echo-collapse op — streaming atom) — the captured corpus was produced by the OLD writer and lacks the new records, so it CANNOT validate them. These require a **live dual-run A/B**: the old writer and the new writer run on the SAME turn inputs (including deliberately **exotic tool outputs and injected tool crash/timeout** — the paths happy-path captures never exercise, §2.8.b), and their projections are diffed under 4.1.A. This harness is NEW and is a first-class deliverable of step 0.

**4.1.C — Corpus governance.** Real sessions persist FULL content and secrets reach the store today (`globals.py:620-621`); committing raw captures = credentials in git + LFS-quota + staleness (memory `reference_stale_web_fixtures.md`, LFS is owner-gated). Policy: the corpus is **generated at harness time by a scripted live run** (`grind-clio-case`), passed through the **write-path redactor** (Q3) before it touches disk, and stored in a dedicated non-committed test-corpus dir keyed by wire era; a refresh procedure regenerates it whenever the wire changes. No raw session bytes enter git or LFS.

This harness is the acceptance instrument for §1 and the standing F3 gate.

### 4.2 Strangler order (which consumer first, why)

Ordering corrected per the review (memory win first; part atoms before persistence-by-reference; streaming atom before killing `final_message`, to eliminate the cross-pipeline `reload==live` window):

1. **Boot index-only + LRU/TTL rehydration (#889), against the EXISTING store** (§3.1). *First* — independent of the collapse, delivers the whole measured memory win, zero unification risk. Gate: memory profile re-run (boot ≤ index; bounded resident set) + 1.3/1.13 unchanged.
2. **Collapse the dual ARC** — working-set = fold of the log (§2.8.b), unify the ingest encoder (caveat a), emit `step_open` on error paths (caveat b), and add the ingest-time search indexer (§2.7) + expert-span chunk partition (§2.10). *A write-path change.* Gate: golden-prompt (1.4) on the corpus **+ live dual-run A/B on exotic/error inputs** (4.1.B) + `grind-clio-case` ≥0.8 + ingest promoted to must-succeed as the old `append_segment` write is removed (§3.4).
3. **Serving-layer dedup as recorded op — ALREADY DELIVERED by prior work (#767 PR3, commit `9078aa2`, 2026-07-02); no migration slice to run.** This step was scoped against the #736 `_dedup_cross_agent_text` serving-time string pass, which **no longer exists**: #767 PR3 deleted it (`finalize()` persists the ledger VERBATIM — a post-hoc text-drop pass is structurally impossible), and the cross-agent answer collapse is now a **write-time op-identity mechanism** — `TurnTranscript.turn_answer_stream(responder, *covers)` + `FieldStream.finish()` (`gact/transcript.py`): the surviving occurrence is the producer's already-landed part *by construction*, decided at the write site, independent of serving order. That satisfies the owner's "decisions are recorded, never re-derived" ruling **structurally** — there is no fold-time re-decision left to record an op against, and serving code carries no dedup logic. (The deleted pass was order-dependent — first arrival wins, by cross-author `.strip()`-equality — so a fold-time re-derivation could never have reproduced it under a concurrent-expert race; the op-identity shape is the correct end state, not merely the incumbent.) **Residual:** the parent-restates-child echo OUTSIDE the answer channel (`turn-transcript.md` PR3 disclosed adaptation (d)) is NOT closed by this and is resolved at **step 5**, where parts are atoms on the log — see the step-5 consideration below. #767 PR4's `restates_part_id` client-collapse tag is **REJECTED** for that residual (owner ruling: the server owns the clean stream — the server emits de-duplicated data; the UI renders verbatim and never owns dedup).
4. **Message/part atom family at finalize** — write the `message_part` atoms (§2.3) carrying wire identity, **dual-written alongside `final_message`** (not yet replacing it). *Write-path.* Gate: dual-run that every wire field in `final_message` is reproducible from the part atoms (the pre-condition for step 5).
5. **Persistence + SSE spine both project the part atoms; kill `final_message`** — gact messages assemble by reference; the SSE spine emits from the SAME atoms. *Read-path* (the writes landed in step 4). This ORDER eliminates the cross-pipeline window the review flagged: reload and live are never two independent representations, because step 4 already made the part atoms the single source. Gate: `reload == live` (1.3) + instrument the live-copy count to quantify the win. **Step-5 consideration — the residual #736 echo (deferred here from step 3):** once parts are atoms on the log, a parent part that verbatim-restates a terminal child's already-landed part is collapsed **server-side by part identity**: the detection site records the decision ON THE LOG (a typed op / `derived_from` reference naming the child's surviving part atom — the decision is recorded at detection, never re-derived at fold time), and BOTH served projections (the SSE spine and `GET /messages`) emit the collapsed view. This is the accepted replacement for the REJECTED #767 PR4 `restates_part_id` client tag: same detection point, but the collapse is recorded on the log and applied by the server's projection — never delegated to the client (owner ruling: the server owns the clean stream).
6. **workflow_state fold → recorded `state_merge` result** (§2.8.d). Gate: delegation tests + #880 baseline-0 guard (1.9) + `schema_version` replay test.
7. **Mutable live-edge streaming atom** — identity-stable atom that grows in place; read-models coalesce deltas (#737 catch 3). *Last* — the write-amplification risk (per-token `replace` = tombstone storm); schema-provisioned now (§2.3), implemented after equivalence lands. Unlocks streaming thinking.

**Ordering note (reload==live):** steps 4→5 are adjacent and in this order specifically so the system is NEVER in the #731/#732/#733/#736-prone configuration where `GET /messages` projects atoms while the live SSE is a different pipeline. The message/part identity that `to_wire()` must reproduce lives in the step-4 atoms, not in the `final_message` step 5 deletes.

### 4.3 Dual-write, replay-backfill, and the legacy tail

- **New/live sessions:** **dual-write** during each strangler step — write the old store and the log, run the §4.1 diff continuously; cut the READ to the projection when the diff is clean over N sessions (**N ≥ the captured-corpus size AND ≥ one full grind-case scenario set**, stated so it is not open-ended); then remove the old write (with the §3.4 must-succeed promotion). Log-authoritative on crash (§3.4).
- **Historical sessions — decided, not best-effort.** The premise "the log already exists as `_events` for past sessions" is **conditional on the trace backend**: when the durable trace is ENABLED (`trace.backend=file/factory`), `release_session`/`clear` ERASE the `_events` family (`live.py:311-324` gated by `memory.py:77-89`, #762), so for those installs the past-session log lives ONLY in the durable trace. Therefore historical sessions are handled by a **one-time eager backfill at upgrade**, from whichever source exists: `_events` (trace=none installs) OR the durable trace (trace=file/factory installs). Sessions predating both (pre-`_events` era) are marked `legacy_format` (typed) and served by a **thin, read-only legacy adapter** over their original `messages/*.json`. That adapter is **sunset-bounded** by the retention TTL (it serves only pre-migration sessions, which age out) — it is a migration tail, NOT the permanent dual read path F1 forbids. This resolves the review's "pick one": we neither best-effort-project a frozen surface (contract break) nor keep the old read path forever (F1).

### 4.4 Per-step exit criterion & rollback

A step is done ONLY when: (a) its §4.1 diff (correct obligation per 4.1.B) is green; (b) the relevant §1 acceptance test passes; (c) the old write path is *removed* AND its ingest promoted to must-succeed (§3.4); (d) the live gates pass after the full sequence. No deferral-by-issue: residuals are in-scope slices.

**Rollback — session-scoped, real, and bounded (review corrections):**
- **(a) not self-contradictory.** The per-step flag rollback exists during the DUAL-WRITE window (old write still present). Criterion (c) removes the old write only after that step's gates are green; a regression surfaced by step N+1 rolls back N+1 (still dual-writing), not N. Once a step's old write is removed, "rollback" = code revert + replay-backfill (the log survives, §4.3) — stated so no one expects a flag to resurrect a deleted write path.
- **(b) session-scoped, so no double-apply.** The flag is evaluated at **session creation** and pinned for the session's life. A session is EITHER old-regime OR new-regime end-to-end — never both. Worked example (step 5's echo-collapse projection): rolling it back cannot double-collapse a session — sessions created under step 5 keep the recorded-op collapse (their projection applies it once); sessions created after rollback are served by the pre-step-5 assembly (the echo renders as it arrived — honest, never collapsed twice). No session ever gets both passes. *(This example originally paired step 3's "dedup-as-op" against the old "assembly dedup"; step 3 is delivered by write-time op-identity — #767 PR3, see §4.2 — so it no longer has an old/new regime pair to flag.)*
- **(c) never mid-session.** Rollback applies to NEW sessions only (a consequence of (b)); an in-flight session's read path never flips, so live ops (`reset`, autocompact) always target ids that exist in the store the read path used. The unsound mid-session flip the review constructed is prohibited by construction.
- **(d) versioned format, no silent skip.** The `messages/*.json` and log-chunk formats carry a `schema_version`. A rolled-back reader that meets a newer-version file **raises a typed `unreadable_version` reason** — it does NOT silently skip the row (today `messages.py:87-93` `except: continue` silently drops malformed rows; that path is replaced with a typed reason so a version mismatch is loud, not data-disappearance). A down-migration converts new→old format for the rollback window.

### 4.5 Explicit failure-mode checklist (from past attempts)

- 5th store → reuse `_events` + raw lane; legacy adapter sunset-bounded (§4.3). ✔
- big-bang → 7 session-scoped steps, one read consumer each (§4.2). ✔
- silent divergence → split harness + normalization spec + every degradation typed (§4.1, §3.3, §3.4). ✔
- the recursion → raw append lane, explicit `record_op` (§2.9). ✔
- ingest silence now fatal → must-succeed-or-typed-fail (§3.4). ✔
- write-path change hidden from the corpus gate → live dual-run A/B (§4.1.B). ✔
- reload==live cross-pipeline window → step 4 before 5 (§4.2 note). ✔
- non-deterministic fold → record the result + `schema_version` (§2.8.d). ✔

---

## 5. Non-goals & open questions

### 5.1 Non-goals

- **Speed / transport work** ([#891](https://github.com/iowarp/clio-agent/issues/891) cache/TTL/pooling, [#895](https://github.com/iowarp/clio-agent/issues/895) thinking-level knob) — a separate front of #893. **The SSE resume buffer / event-bus transport plane (§2.8, 1.12) is part of this front, explicitly OUT of the log collapse.**
- **No render changes.** `apps/web/RENDERING_SPEC.md` and `CANONICAL-CONVERSATION.md` are READ-ONLY; the client renders verbatim.
- **No wire changes.** The §7.7 machine-checked vocabulary is frozen; no new/removed `data.type`. (`report_tokens`/`reset`/`transcript_delete`/`state_merge` are log-internal ops, off the served allow-list — Q5.)
- **CTE tiering mechanics** ([#890](https://github.com/iowarp/clio-agent/issues/890), [#892](https://github.com/iowarp/clio-agent/issues/892)) are prerequisites, not this doc's design.

### 5.2 Open questions (each with a recommendation)

1. **Physical residence of the canonical log by default — CTE or LocalFS?**
   *RESOLVED (owner ruling, 2026-07-12):* **clio-core is the default** home of the canonical log (as today — `make_arc_store` default="cte"); it owns residency/tiering. **If clio-core is not installed or fails to initialize, the system LOUDLY degrades to LocalFS**: a typed degradation reason on the trace, a doctor DEGRADED row, never silent and never a refusal to run (this CHANGES today's hard-raise in `make_arc_store` — implementation slice chained on #893). `CLIO_ARC_STORE=local` remains the explicit selection for dev/diagnostics. The collapse must hold on BOTH backends (the equivalence harness runs on both).

2. **Streaming live-edge atom (§4.2 step 7) — this collapse or fast-follow?**
   *Rec:* schema-provision now, implement LAST after equivalence lands. It is the one primitive with genuine write-amplification and its own acceptance (streaming-thinking cadence); don't let it gate the memory win.

3. **Move secret redaction to the write path — safe given `detail_level` (§7.6)?**
   *Rec:* yes — redact only genuine credentials irreversibly at ingest (so no secret reaches any consumer, and the §4.1.C corpus can be generated safely); keep `detail_level` a read-time filter over already-safe content. This does not change what `semantic` serves today. **Confirm** the credential detector's precision (a false positive irreversibly drops real content) — recommend a conservative detector + a dual-run false-positive check.

4. **Persistence-by-reference vs `reload == live` (§6.3 line 880).**
   *Rec:* references are internal; the projection materializes `to_wire()` on read, so bytes are unchanged (§4.1.A normalization). Identity lives in the step-4 part atoms, minted once. Gate = 1.3; if it can't be green under 4.1.A, do not ship step 5.

5. **`report_tokens`/`reset`/`transcript_delete`/`fork`/`state_merge` — new served wire kinds?**
   *Rec:* NO new wire types. `report_tokens`/`reset`/`state_merge` are unserved; `transcript_delete`/`fork` surface via EXISTING wire (`message.deleted`/`session.undo`/`session.cleared`/`session.created`, 1.11) — no `arc.op` vocabulary change. The served-payload allow-list is guarded by 1.7's NEW served-payload test.

6. **Compaction: append-a-`summarize`-op vs the current in-memory archive + ledger replace (§6.25).**
   *Rec:* append a `summarize` op; the visible-ledger replace becomes a *projection*, not a rewrite of stored content — the one place today that rewrites stored content (§6.25 line 1688), which reproducibility forbids. The WIRE (part, `session.compacted`, `msg_compact_*`+`memory_event_id`) stays byte-identical (1.8). **Owner sign-off required:** moving archive semantics from process-lifetime in-memory to durable log-replay is the single owned exception to "change nothing observable" (the archive is not an external surface; the wire is, and it is preserved).

7. **Boot index shape — reuse `sessions.json` or a new offset index?**
   *Rec:* reuse `SessionStore` metadata records (`sessions.py`) as the boot index (already small, already loaded); add per-session log offsets **and the `metrics_counters` counts** to it (so 1.13 reports from the index, no `load_all` re-walk) — NOT a new file (RULE 4).

8. **First-GET rehydration cost (§2.10) — full fold vs checkpoint snapshot?**
   *Rec:* start with the full fold under the §2.10 read budget (LRU makes it a one-time cost per touched session); if a P-turn session exceeds budget in the harness, add a per-session compacted **checkpoint atom** (a `summarize`-style snapshot) so rehydration reads snapshot + tail. Provision the checkpoint atom kind now; implement only if the measured budget is missed.

9. **Legacy-format sessions predating `_events` (§4.3) — sunset TTL value?**
   *Rec:* the legacy read-only adapter sunsets with the existing session-retention TTL. **Confirm** the TTL with the owner; if installs disable retention, the adapter must be explicitly bounded (a max legacy-session count with a typed eviction reason) rather than growing unbounded.

---

## Appendix — code-cited claim ledger (for the reviewer)

| Claim | Citation |
|---|---|
| Eager unbounded boot load | `gact/app.py:1293-1299`, `gact/messages.py:26-37`, `gact/sessions.py:178-201`, `893_memory_profile.md` Q1-Q3 |
| Reads served from resident dict (free) | `gact/routes/messages.py:329-383`; `893_memory_profile.md` finding 2 |
| Persisted format ≠ `to_wire()` (silent skip on bad rows) | `gact/messages.py:87-93,100-103` |
| `_events` = the log, verbatim, chunked | `arc/live.py:71,74-144,236-293`; `arc/memory.py:837,866-904` |
| Working-set is a best-effort parallel write from raw preds | `gact/agents/runtime.py:144-174,269-308`; `gact/turn_delegation_arc.py:76-78,129` |
| Ingest is best-effort-with-swallow (bare pass / warn-continue) | `arc/live.py:274-277`; `gact/agents/runtime.py:168-174`; `gact/turn_delegation_arc.py:76-78,129` |
| Encoder split: `_arc_obs_value` str() vs `_encode_safe` | `gact/runtime/context_tokens.py:30-38` vs `arc/live.py:112-144` |
| Write-before-execute (tool_call segment precedes tool run precedes react-step emit) | `gact/agents/runtime.py:269-279,284,308` |
| Token heuristic ~4 chars/token | `gact/agents/runtime.py:150-156` |
| Lean projections (reasoning_len, `{name,ok}`) — not byte-exact | `arc/live.py:508,537-543` |
| Per-turn reset targets ids from the read path's render | `gact/agents/runtime.py:190-193`; `arc/segments.py:597-626` |
| `arc.op` routed direct (recursion avoided today) | `gact/runtime/globals.py:602-707,627-633`; `arc/memory.py:65-70` |
| `arc.op` served only for mutating ops; payload allow-list `{id,kind,token_count}` | `gact/runtime/globals.py:678,693-700` |
| `arc.op` embeds FULL segment dicts (durable side) | `gact/runtime/globals.py:660` |
| SSE ids = process-global `itertools.count(1)`, replay keeps id, heartbeats transient | `gact/events.py:47-54,71,77-94,131,270-311` |
| `lm.token.delta` skip-listed; session-less events skip | `arc/memory.py:70,824-834` |
| `final_message` byte-copy on `turn.completed` (the messages-store derivation today) | `gact/turn_finalize.py:615-622` |
| `turn.started` logs only `{text}`+message_id (no user parts/created_at) | `gact/turn.py:238-249` |
| workflow_state merge is pack-schema- and order-parameterized | `gact/workflow_state/merge.py:76-121` |
| `.search` companion written at scope-persist; `_events` search-excluded | `arc/segments.py:411-429`; `arc/live.py:102-109` |
| Per-scope locks; whole-scope load/render | `arc/segments.py:323-333,337-363`; `arc/live.py:347-349` |
| as-of-T reproducible render; single order key | `arc/segments.py:876-905,445`; `arc/schema.py:119` |
| LRU cap 1000; segment/index uncapped (lifecycle-only) | `arc/memory.py:207,214`; `893_memory_profile.md` Q5 |
| `release_session` retains `_events` under trace=none; erases when trace enabled (#762) | `arc/memory.py:77-89,1189-1265`; `arc/live.py:311-324,30-35` |
| undo/rewind `memory_scope:"gact_visible_transcript_only"` | `SPEC.md` §6.2:787-818; `gact/routes/sessions.py:342,365`; `gact/messages.py:59-75` |
| Reload byte-identical to live via `to_wire` | `SPEC.md` §6.3 lines 880-881 |
| Wire vocabulary frozen; vocab tests are static/opt-in (no drop-detect) | `SPEC.md` §7.7; `apps/core/tests/spec_vocabulary.test.ts:67-89`; `contract/conformance/vocabulary_checks.go`; `conformance/README.md:56` |
| Streaming shape; `final_text` authoritative; ask-user pause = no terminal | `SPEC.md` §7.4/§7.4a; §6.23; §7.3a:1789 |
| Semantic-event served subset + reasoning kept | `SPEC.md` §7.6:1980-2007 |
| Compaction wire (part + event + ids); in-memory archive rewrite | `SPEC.md` §6.25:1664-1695; §4.5:532 |
| Diffs frozen-at-pending; apply state only via `GET /diffs` | `SPEC.md` §4.5:529; §6.9/§6.10 |
| `expert_handoff` = `output` verbatim + `workflow_state` | `SPEC.md` §4.5:553-599 |
| Turn-retry attempts + user-question resume | `SPEC.md` §6.24; §6.23; `user_question.resumed` payload |
| SSE subscription/resume semantics (process-global id, global-event merge) | `SPEC.md` §7.1:1714-1735 |
