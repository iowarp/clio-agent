# Compaction checkpoint + chunked atom lane (#1339)

Status: landed on `fix/1339-compaction-checkpoint` (HEAD `4b8427f3`, four commits above
`0d0e3e2b` on `feat/1320-provenance-presentation`, level 9 of the release stack), not yet
merged. Slices A (`58fc57b4`, atom lane chunking), B (`993fe5dc`, checkpoint compaction),
and the two follow-on commits that proved both — `911b0aac` (store-put audit rows) and
`4b8427f3` (the live leg, `scripts/live_verification/leg_compaction.py`) — are described
here as landed, not as plan. Credit: the checkpoint semantics (append instead of replace,
`conversation_projection.model_context_messages` as the model-context rule, deleting the
`session_archives` dead write and the `preserve_a2ui` call on compact, the repeated-
compaction test shape) were first prototyped by Codex on `codex/arc-trace-compaction`
(not merged — see "What was deleted and why safe" for why).

## The defect

Before this branch, `POST /v1/sessions/{sid}/compact` (`routes/sessions.py::compact_session`)
destroyed the human-visible transcript in four ways:

1. It built the summariser input from `ledger[-50:]` text parts only — a hardcoded cap
   left over from the original `/compact` commit. Rows older than 50 were neither
   summarised nor kept, and tool calls/results never reached the summariser at all (only
   `part.text` was read).
2. It REPLACED the whole ledger with one synthetic `compaction` message
   (`deps.replace_session_messages` → `session_store._replace_session_messages`), which
   overwrote the per-session message file and dropped the ARC atom lane `_events/m`
   (`transcript_projection.on_ledger_replaced` → `drop_scope`) before re-minting only the
   summary.
3. It kept the "archive" in `app.state.session_archives`, a write-only in-memory dict
   built with `app.state.__dict__.setdefault("session_archives", {})` — zero readers.
4. It overwrote ARC's `conversations` record with the summary through
   `gact/compact_memory.py::store_compact_conversation` — a record nobody reads.
   `agent.py` constructs `self.context_retriever = ContextRetriever(self.arc)` and never
   calls it again; `ContextRetriever` (`arc/retrieval.py`) has no other caller in the
   codebase.

Two more defects sat next to it: `session_store._compile_session_conversation_history`
joined only `text|thinking|error` parts, so the summary (carried in `part.summary`, not
`part.text`) was invisible to the model on the next turn; and
`enrichment._record_context_frame` marked every row `included: True, reason:
visible_transcript` regardless of whether compaction had actually dropped it from the
model's view.

Automatic compaction (`reactv2._RetainingReActV2._maybe_autocompact`) was a wholly
separate mechanism: it folded the ARC working set via `arc.summarize_segments` and
emitted no transcript row, no semantic event, no SSE — a session compacted only
automatically had no visible record that compaction had ever happened.

A second, independent defect sat underneath the transcript: the atom lane was ONE
segment-store scope (`_events/m`), so every `message_part` atom append re-encoded and
re-PUT the whole lane (`_append_segment_raw` → `SegmentStore._persist` → `_put_scope`) —
Θ(N²) bytes per session, the same class of defect #771 already fixed for the semantic-
event log (`_events`, chunked via `arc.events_chunk_segments`) but never applied to
`_events/m`.

## The checkpoint contract

Compaction is now a CHECKPOINT, never a delete. A checkpoint is an ordinary assistant
`Message` carrying exactly one `compaction` part (`gact/routes/compaction.py::
build_compact_summary_message`, unchanged by this branch), APPENDED after the rows it
stands in for. History is retained in full — every prior row stays in the ledger, the
per-session message file, and the atom lane — for display, undo, and reload. Only the
MODEL-facing prompt shrinks at a checkpoint.

The rule for what the model sees is COVERAGE, not position — owned by one new module,
`gact/conversation_projection.py::model_context_messages`. A naive `rows[start:]` slice
at the latest checkpoint's ledger index would drop the compacting turn's own assistant
answer: because of turn-boundary placement (below), that answer lands BEFORE the
checkpoint that was built from a transcript taken before the answer existed, so the
checkpoint's own summary does not include it either. Coverage instead keeps exactly the
rows the latest checkpoint's `compacted_message_ids` name as covered, plus every row it
does not name, so nothing in the model context is silently dropped by position:

- No checkpoint row present → the whole ledger (compaction has never run).
- A checkpoint present → the checkpoint row itself, plus every row whose id is not in
  the transitive closure of `compacted_message_ids` starting from the latest checkpoint.

Coverage is TRANSITIVE across repeated compaction. A second checkpoint's
`compacted_message_ids` includes the first checkpoint's own row id (the first checkpoint
was part of the model context the second checkpoint's transcript was built from), so the
first checkpoint's row — and everything it had already excluded — is excluded from the
model context by the second checkpoint too, even though the second checkpoint's id list
never names those earlier rows directly. `model_context_messages` resolves this with a
plain worklist over `_covered_ids`, walking outward from the latest checkpoint's covered
set until the frontier is empty.

`model_context_messages` is attribute-or-dict tolerant (`_get`) so it works over both
`Message` models and legacy dict-shaped rows, and it is now the ONE place three other
call sites read the model context instead of the raw ledger:
`gact/session_store.py::_compile_session_conversation_history` (renders prior turns into
the current prompt — a checkpoint renders as `"Compacted context: " + part.summary`, via
the new `_prior_message_text` helper, instead of its empty `part.text`),
`gact/enrichment.py::_record_context_frame` (marks pre-checkpoint rows
`included=False, reason=compacted` instead of unconditionally `True`), and
`gact/compaction.py::compact_session_context` itself (the summariser's own input).

## One operation, two triggers

Both `POST /v1/sessions/{sid}/compact` (manual) and the proactive auto trigger now call
the same function: `gact/compaction.py::compact_session_context(app, sid, *, trigger,
focus="")`. It is blocking and off-loop only — every step is a store RPC and/or an LM
call.

| | Manual (`trigger="manual"`) | Auto (`trigger="auto"`) |
|---|---|---|
| Entry point | `routes/sessions.py::compact_session` — ~15-line delegation, `await run_off_loop(lambda: compact_session_context(...))` | `gact/compaction.py::maybe_autocompact()`, called from `reactv2._RetainingReActV2._maybe_autocompact` (a 3-line delegation) |
| Runs on | The route's off-loop executor thread | Inside `instrumented_forward`, the turn's own thread |
| Can fold the ARC working set? | No — `_fold_arc_working_set` reads the CALLING THREAD's contextvars via `reactv2_events._arc_scope()`, and the off-loop executor thread carries no react scope even mid-turn | Yes, when a scope is live |
| `arc_status` it reports | Always `no_active_scope` (unless ARC is unconfigured) | `not_configured` \| `no_active_scope` \| `working_set_too_small` \| `folded` |
| Focus text | Optional, from the request body | None (`focus=""`) |
| Failure handling | Raises `CompactionError`, turned into the matching HTTP status | Caught, audited `compaction.auto_failed`, swallowed — auto-compaction is a proactive optimization, never a hard turn dependency |

`arc_status` (`ARC_STATUSES` in `compaction.py`) is a typed description of ARC
working-set-fold reality, never a fabricated "stored": ARC's live plane only has scope
*inside* a turn (the per-turn reset in `reactv2_events.instrumented_forward`), so a
manual compact issued between turns — the common case — correctly reports
`no_active_scope`. `_fold_arc_working_set` itself can raise (it calls
`arc.summarize_segments`, a real store RPC); the caller does not hide that — see
"What was deleted and why safe" below for how the failure surfaces.

`compact_session_context`'s own flow: build the model context
(`model_context_messages(ledger)`), skip typed (`session_has_no_messages` when the
ledger is empty, `model_context_empty` when the model context resolves to nothing, or
when it resolves to rows whose parts render to no text at all — e.g. only `a2ui`
parts — checked BEFORE the LLM is ever called, not after), render the transcript
(`_build_transcript`, every part class the client already renders: text/thinking/error
verbatim, `tool_call`/`tool_result` as bounded 300-char facts, an earlier `compaction`
part's own `summary`), dispatch the `PreCompact` hook, call the LM
(`agent._run_chat_agent`, wrapped in `agent._call_with_transient_provider_retries` when
available), append the exact-evidence index (`delegation._compact_exact_evidence_index`),
build the checkpoint message, fold ARC, then land the checkpoint — either immediately
(`append_checkpoint`) or staged (`stage_checkpoint`), decided by whether the session's
turn minter is currently open.

There is no more deterministic `-50` cap anywhere in this path: the checkpoint IS the
bound now, by construction. An over-window transcript is not silently truncated; it
surfaces a typed audit row (`compaction.input_over_window`, carrying
`input_tokens`/`context_window`) and the call proceeds — truncation, if any, is left to
the LLM, never to clio's own code (RULE 3/CLAUDE.md "Prefer explicit over implicit";
also the no-silent-fallback ground rule).

## Placement and staging

A checkpoint is never inserted ahead of an in-flight assistant message — that would
reorder `reload` (which reads the per-session file/ledger) ahead of `live` (the SSE
stream mid-turn), and it would break `transcript_projection.assemble_session_messages`'s
grouping rule: `part_atoms.group_atoms_in_order` keys a message group by `message_id`
and anchors ordering on the FIRST atom it sees for that id from the atom lane's own
append order (`lane_chunking.lane_segments`, sorted by `logical_time`). Sealed atoms are
already on the lane by the time a turn's later parts are minted; inserting a checkpoint
row's atoms ahead of an open turn's own sealed-but-not-yet-finalized atoms would
interleave two messages' atoms out of turn order.

So while a turn's transcript minter is open (`part_atom_minter.turn_minter(app, sid) is
not None`), a checkpoint is STAGED instead of landed:

- `stage_checkpoint(app, sid, checkpoint, **fields)` holds the built checkpoint (and its
  memory-event fields) in `app.state.staged_compactions[sid]`, and returns
  `checkpoint_placement="staged_for_finalize"` to the caller immediately — a manual
  compact issued mid-turn does not block on that turn finishing.
- A second compact call while one is already staged for the same session returns the
  typed skip `checkpoint_already_staged` rather than silently overwriting or queuing
  (`compact_session_context`'s `turn_minter(...) is not None` branch).

The checkpoint is flushed — appended for real — at two points, both delegating to the
private `part_atom_minter._flush_staged_checkpoint`:

- **Primary flush**: `part_atom_minter.persist_finalized_message`, right after that
  turn's own assistant message persists (`_append_session_message`) — on both the
  eager-mint path (`atoms_minted=True`, `minter.mint_remainder(message)` already ran) and
  the legacy inline-mint path. Ledger order is then always `[..., assistant,
  compaction]`.
- **Backstop flush**: `part_atom_minter.close_turn_minter`, called by every turn-exit
  path (`turn_stream.settle_turn_transcript`) including ones that never reach
  `persist_finalized_message` at all — e.g. an ask-user early return with no assistant
  message to persist. Idempotent with the primary flush: a checkpoint already flushed
  there is simply not staged any more, so the backstop is a no-op in the common case.

A staged-flush failure must never fail the turn whose finalize triggered it — the turn's
own, already-real assistant answer must still settle. Both flush points catch around
`flush_staged_checkpoint`, audit `compaction.staged_flush_failed` (with the event id
peeked before the attempt, since `flush_staged_checkpoint` pops the staged entry before
it can raise), and drop the checkpoint — lost, never silently retried or left stuck; the
audit row is what tells "nothing staged" and "a caught failure" apart, not an ambiguous
return value.

| Reason | Meaning |
|---|---|
| `session_has_no_messages` | Skip: empty ledger |
| `model_context_empty` | Skip: model context has no rows, or renders to no text |
| `checkpoint_already_staged` | Skip: a checkpoint is already staged for this session |
| `compaction.input_over_window` | Audit only: transcript token estimate exceeds the context window; proceeds anyway |
| `compaction.auto_failed` | Audit only: the auto trigger's `compact_session_context` call raised; swallowed |
| `compaction.persist_failed` | Audit + typed 500 `memory_update_failed`: the ARC fold or `append_checkpoint` raised |
| `compaction.staged_flush_at_close` | Audit only: `close_turn_minter`'s backstop flush found and landed a staged checkpoint |
| `compaction.staged_flush_failed` | Audit only: a staged flush raised and was caught; checkpoint dropped |

## The wire

The wire is deliberately unchanged where gact-tui already depends on it, pinned by that
repo's `wire_shapes.test.ts`. `session.compacted` still carries exactly `{event_id,
archived_count, summary_chars, summary_message_id, version}` — `append_checkpoint`
additionally sets `trigger` on the payload, which is additive (existing clients that
don't read it are unaffected). `archived_count` is the same FIELD but a redefined
NUMBER: before, it was how many ledger rows were archived (deleted); now it is how many
model-context rows (`len(model_messages)`) the checkpoint covers — the ledger itself
never shrinks, so "archived" now means "covered", not "removed".

New, additive surface on the `memory.compacted` semantic event and the compact response:
`trigger` (`"manual"` | `"auto"`), `checkpoint_placement` (`"appended"` |
`"staged_for_finalize"`), `arc_status`, `input_tokens_estimated`, `context_window`. The
`compaction` `Part` itself (`gact/parts.py`, `sdk/types.py`) is byte-for-byte unchanged
on the wire — `summary`, `auto`, `compacted_message_ids` — only its docstring changed to
say history is retained, nothing deleted (#832's structured-part shape from before this
branch).

Landing a checkpoint publishes two events, in order: `message.created` (new — the only
way a web client learns of the new row without a full reload; feeds the v3
`message.upserted` projection, `protocol_v3.py`) with the checkpoint's `.to_wire()`
payload inline (parts included, not a reference), then the unchanged `session.compacted`.
`docs/SEMANTIC_EXECUTION_TRACES.md` now notes `memory.compacted` is "an appended
checkpoint — history is retained, never replaced."

## What was deleted and why safe

| Deleted | Why safe |
|---|---|
| `gact/compact_memory.py` (`store_compact_conversation`, the ARC `conversations`-record mirror) | Sole writer; its record has no reader. `agent.py` builds `ContextRetriever(self.arc)` once and never calls it; grep confirms no other reference to `ContextRetriever` in `src/`. |
| `app.state.session_archives` | Built via `app.state.__dict__.setdefault("session_archives", {})` in the old route and never read anywhere — a write-only dict. |
| The `ledger[-50:]` summariser cap | The checkpoint bounds the model context by construction now; there is nothing left to truncate defensively, and truncating already dropped tool calls/results the old cap never even considered. |
| `preserve_a2ui` on compact (`routes/session_a2ui_preservation.py`) | That helper existed to synthesize a "preserved" row for an a2ui part that would otherwise be destroyed by the replace. Compaction no longer replaces anything, so there is nothing to preserve FROM — the original a2ui row is simply still there, like every other row (proven by `test_compact_retains_the_a2ui_surface_and_every_row`, which asserts no `synthetic: a2ui_preservation` row exists any more). |
| The replace itself (`deps.replace_session_messages`) | Compaction's only write path is now `append_checkpoint`'s `_append_session_message` — an append, never a replace. |

A failure landing a checkpoint (the ARC fold OR the atom mint) is wrapped by
`_typed_persist_error` into the ONE typed 500 `memory_update_failed` a manual-route
caller sees (audited `compaction.persist_failed`, `landing_stage` distinguishing
`fold_arc_working_set` from `append_checkpoint`), instead of reaching the client as an
untyped `internal_error` — the review finding (`#1339 review F1`) this branch fixes over
the earlier draft.

## The `setdefault` non-bug verification

`session_store._append_session_message` — the single seam `append_checkpoint` uses to
land a checkpoint's ledger row — writes `app.state.messages.setdefault(session_id,
[]).append(message)` before it writes through to the durable per-session file
(`store.append`), and only AFTER both of those does `append_checkpoint` schedule the
atom mint (`run_transcript_job(..., on_message_appended)`, off-loop). A naive reading of
that `setdefault` raises a question: if `session_id` were evicted from `app.state.messages`
between the ledger append and the (separately scheduled) atom mint, would the `setdefault`
silently manufacture an empty list and lose every row minted before it?

It does not, for two independent reasons, both verified by reading the code (not
asserted):

1. `app.state.messages` is `gact/resident_ledgers.py::ResidentLedgerSet`, a bounded LRU
   cache over the SAME durable per-session file `_append_session_message` already writes
   through to on every call. Even a full eviction between the append and the mint only
   drops the RESIDENT copy; the next access rehydrates it byte-identically from disk —
   nothing is lost, only re-read (`ResidentLedgerSet`'s own docstring: "Eviction is
   always safe because the on-disk per-session file is the authoritative copy").
2. `ResidentLedgerSet` "never evicts an active session — one with a live SSE subscriber
   or an in-flight turn is pinned until it goes idle" (`build_resident_ledger_set`'s
   `is_active=lambda sid: _session_is_active(app, sid)`). A session with a checkpoint
   mid-flush (staged or about to be, per "Placement and staging" above) is by
   construction either mid-turn or has just finished one with an SSE subscriber likely
   still attached — exactly the condition the pin rules out evicting.

So the `setdefault` is not a race: append happens before the mint, and the active-session
pin rules out the mid-turn eviction window that would make the ordering matter.

## The atom lane chunk family

`arc/lane_chunking.py` (new, 427 lines) is the ONE owner of the chunk-family grammar
both `_events` (the semantic-event log, #771) and `_events/m` (the `message_part` atom
lane, new here) now share. `arc/memory.py` shrinks — its own `_events_writer` /
`_events_writer_lock` / `_events_chunk_for_append` / `_recover_events_writer` are
deleted and replaced by calls into this module; `arc/live.py`'s
`events_chunk_scope`/`events_chunk_index` become one-line delegations to
`lane_chunking.chunk_scope`/`chunk_index`. `arc/segments.py` is untouched.

**Grammar.** Chunk 1 is the bare `base` scope itself (`"_events/m"`) — a migration by
construction, since an unchunked legacy lane already looks like "one chunk" to every
reader. Chunk `N >= 2` is `f"{base}/{N}"`, `N` a canonical decimal with no leading zero
(`_CANONICAL_TAIL`). `is_chunk_of` / `chunk_index` are injective and disjoint from any
sibling partition under the same base — `_events/m/edge` (the `live_edge` sibling
partition) and `_events/m/02` are never swept into the family.

**Dense-prefix invariant.** Chunk `i + 1` exists only once chunk `i` reached its
capacity, and a family is removed only as a whole. This is what lets every reader WALK
ascending chunk indices via `SegmentStore.list_segments` — a cheap per-scope `get`, never
a body-downloading `scan` — and stop at the first absent chunk (`_dense_walk`), instead
of discovering the family through an expensive scope scan.

**Writer cursor.** `chunk_for_append(store, session_id, base, capacity=...)` is THE
writer seam: it reserves a slot in the active chunk and rolls to the next one once the
active chunk holds `capacity` segments, kept in a module-level, per-`(store, session_id,
base)` cache (`_CURSORS`, a `WeakKeyDictionary` keyed on the store instance so it never
outlives it) — so the append hot path never re-derives `(index, count)` from disk on a
warm process. The cursor is trusted only while its active chunk is still present in the
STORE'S OWN `_loaded` cache (`SegmentStore._loaded`); every eraser (`drop_scope` /
`release` / `clear`) already discards that key as a side effect of erasing, which gives
cursor invalidation a zero-extra-RPC signal for free — `chunk_for_append` reads a stale
`_loaded` miss as "the cursor is stale," recovers via `recover_writer` (a fresh dense
walk resuming at the last persisted chunk), and continues, rather than silently writing
past a hole left by a concurrent erase.

**Why readers sort by `logical_time`.** Chunk (discovery) order is NOT append order
under concurrent writers: a thread can reserve the LAST slot of chunk N while a second
thread's append into the just-opened chunk N+1 lands first (its `store.append` actually
executes before the first thread's does), so chunk N ends up holding a later
`logical_time` than chunk N+1 even though the dense walk still discovers N before N+1.
`lane_segments` — the ONE function every reader (`part_atoms.load_message_part_atoms`,
`transcript_projection.assemble_session_messages`) goes through — concatenates the
dense-walk chunks and then re-sorts by `SegmentStore`'s store-wide monotonic
`logical_time` clock (ticked once per real `append()`, never at reservation time), which
recovers the true append order regardless of which chunk each segment physically landed
in. This was proven live: an earlier version of the live leg asserted "the last turn's
puts touch only one record, the highest chunk," which a real codex run disproved (turn 5
straddled a chunk boundary mid-turn, landing puts in both chunk 3 and chunk 4) — not a
backend defect, a wrong assertion, fixed to the real invariant (monotonic non-decreasing
chunk index across the whole run's put sequence).

**`drop_lane`'s look-ahead limit.** `drop_lane` erases every chunk the dense walk finds,
then probes `_HOLE_LOOKAHEAD` (2) indices past the first absent one for a chunk that
survived past a gap — documented, not expected under the dense-prefix invariant, but an
erase must finish cleanly and audibly (`atom_lane_chunk_gap`) if one is found anyway. The
limit is stated plainly, not hidden: a hole of 3+ consecutive absent indices leaves a
surviving chunk beyond it an ORPHAN on disk (measured in
`test_drop_lane_tolerates_a_hole_and_audits_the_gap`: chunks present at 1, 2, 5 erase
only 1 and 2; chunk 5 survives). Not a data-loss risk — the orphan was already
unreachable by every reader (which also stops at the dense-prefix boundary) — only dead
weight in an already-anomalous case the invariant says should not occur.

**The knob.** `arc.message_part_chunk_segments` (env
`CLIO_ARC_MESSAGE_PART_CHUNK_SEGMENTS`, default `512`, resolved fresh on every append via
`part_atoms._chunk_capacity()`, never cached on the `ARCMemory` instance) is a SIBLING
knob to `arc.events_chunk_segments`, not shared with it: atoms carry whole part dumps (a
tool result can be MBs) versus the semantic-event log's lean event dicts, and a
hermetic test that reconfigures the events lane's chunk size must never accidentally
reconfigure this lane too.

**Loop-guard accounting.** `chunk_for_append` and the rest of the cursor machinery are
pure in-memory bookkeeping — no store RPC — so chunking adds zero new loop-thread store
writes; `test_part_atom_lane_chunking.py` proves minting through the real off-loop writer
(`PartAtomMinter`) across a chunk roll never trips the #1334 loop-thread store-write
guard (`arc/loop_guard.py`, `register_server_loop` / `assert_store_write_off_loop`).

## Durability

By owner ruling, the per-session message-store file stays a deliberate first-class
projection — external non-clio tools parse it, so it is not a cache to be collapsed away
under #737's "fewer materializations" direction. Atoms never enter `arc.op`
(`arc/replay.py::ARC_OP_EVENT_TYPE`, the durable trace/replay sink): the atom lane
(`_events/m`, via `arc/lane_chunking.py`) and the per-session file are the transcript's
canonical, durable record; the ARC trace remains the LIVE-PLANE replay contract only
(what a turn's prompt was built from at a point in time), never a second copy of the
transcript. This is precisely what Codex's rejected `codex/arc-trace-compaction` branch
got wrong: it routed atoms through `arc.op` as a fourth copy whose durability depended on
the trace backend and whose content outlived session deletion — rejected for that reason
among the others recorded in the issue's first comment (a `ValueError` on a torn trace
line, mint-before-append that can lose the user message, a `PART_ATOM_SCHEMA_VERSION`
collision with v2, store writes on the loop thread, and the `archived_count`/`arc_status`
wire rename this branch avoided).

## Verification

Unit coverage lives in `tests/test_gact/test_compaction.py` (new, 757 lines),
`tests/test_gact/test_conversation_projection.py` (new), `tests/test_arc/
test_lane_chunking.py` (new, 350 lines), `tests/test_gact/test_part_atom_lane_chunking.py`
(new, 534 lines), `tests/test_arc/test_storage_companion.py` (extended), and the adapted
`tests/test_arc/test_auto_compaction.py` / `tests/test_gact/test_sessions_api.py` /
`tests/test_gact/test_loop_thread_writes.py`. The acceptance criteria from the issue map
onto named tests:

- Repeated compaction retains the transcript and advances the checkpoint:
  `test_repeated_compaction_retains_the_transcript_and_advances_the_checkpoint`.
- The transcript and every checkpoint survive a cold reload byte-equal:
  `test_checkpoint_and_history_survive_a_cold_reload`.
- The auto trigger reaches the same function and stages/flushes correctly against an
  open turn: `test_auto_trigger_stages_and_flushes_after_the_turns_assistant_row`,
  `test_manual_compaction_during_a_running_turn_also_stages`.
- Coverage keeps exactly the right rows: `test_coverage_keeps_the_compacting_turns_own_
  assistant_answer`, `test_rewind_past_the_checkpoint_restores_the_full_model_context`,
  `test_context_frame_marks_pre_checkpoint_rows_excluded`.
- Zero store writes on the server loop: `test_compact_folds_the_arc_working_set_off_the_
  loop` (`test_loop_thread_writes.py`) plus the atom-lane amplification proofs in
  `test_lane_chunking.py` / `test_part_atom_lane_chunking.py`.

The live leg (`scripts/live_verification/leg_compaction.py`) has PASSED against both
configured providers, evidence under `out/live-verification/
compaction-codex-20260911T094554Z` and `out/live-verification/
compaction-claude_code-20260911T094750Z` (`verdict.json` in each, `"pass": true`, zero
`evidence_gaps`). Against a live backend with `CLIO_ARC_MESSAGE_PART_CHUNK_SEGMENTS=8`,
driving five real turns plus two manual compacts and a restart:

| | codex | claude_code |
|---|---|---|
| Loop-thread max latency (`loop_probe.max_latency_overall_s`) | 0.563 s | 0.391 s |
| `store_writes_on_loop` | 0 | 0 |
| Rows after the second checkpoint (`delta_based_expected_rows_after_compact2`) | 12 | 12 |
| Distinct chunk names touched (`lane_chunk_family_evidence`) | 4 (`_events~m`, `~2`, `~3`, `~4`) | 3 (`_events~m`, `~2`, `~3`) |
| Chunk-index put sequence | monotone non-decreasing | monotone non-decreasing |

Both runs confirm `checkpoint_placement=="appended"` for both compacts (neither compact
in this leg lands during an open turn), 9 rows after the first compact against 8 before
it (an append, never a shrink), and `verified_no_sealed_chunk_reamplified: true` — the
end-to-end proof, against a real store, that once a put for chunk N+1 is observed no
later put in the run ever re-targets chunk N or earlier.

## Follow-ups

Recorded, not fixed in this PR (per the issue's own two review comments):

- **The `_events` reader ordering.** The semantic-event log reader
  (`arc/live.py::events_scopes` / its concatenation logic) has the SAME cross-chunk
  read-order exposure `lane_chunking.lane_segments` fixes for the atom lane (events are
  emitted from many threads too); it should adopt `lane_chunking.lane_segments` in a
  follow-up rather than keep its own discovery-order concatenation.
- **`workflow_state/state_merge.py`'s `_events/s` lane** keeps the one-scope-per-session
  shape and has the same Θ(N²) amplification; `lane_chunking` makes chunking it a small,
  mechanical change.
- **The dead `GactDeps.compact_exact_evidence_index` field**
  (`gact/routes/deps.py`, wired at `gact/app.py:2108`). `compact_session_context` now
  imports `delegation._compact_exact_evidence_index` directly rather than going through
  `deps.compact_exact_evidence_index`, so the `GactDeps` field has no remaining caller —
  found while reading the landed code for this doc, not called out in the issue
  comments; worth deleting in a follow-up cleanup pass.

One follow-up named in the brief for this doc — "the title test" — could not be located:
neither issue comment references it, and no test, docstring, or comment touched by any
of the four commits on this branch mentions a session-title test. It is omitted above
rather than invented; flagged for the issue owner to clarify if it refers to something
outside this branch's diff.
