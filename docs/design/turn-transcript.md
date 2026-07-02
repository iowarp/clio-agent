# TurnTranscript — the single-writer part ledger

**Status:** accepted design · PR1 MERGED (`d4159db` — `gact/transcript.py` ledger +
registry, tool-observer/delegation shims) · PR2 implemented on
`feat/767-turn-transcript-pr2` (turn-loop lifecycle + stream tap; see §6 PR2 for the
disclosed adaptations) · **Origin:** synthesis of three competing
designs judged 2026-07-01 (winner: minimal-invasive ledger; grafts: the op-log design's
`FieldStream` exactly-once handle, close-op-recorded `final_text`, and tag-don't-suppress
parent echoes) · **Tracking:** [epic #767](https://github.com/iowarp/clio-agent/issues/767) ·
**Coordinates with:** [gact-tui #232](https://github.com/iowarp/gact-tui/issues/232) (protocol
convergence, single dedup owner) · **Evidence baseline:** every `file:line` below was
verified against `develop@a933728` (`src/clio_agent/gact/turn.py` at 3,618 lines) and has
DRIFTED since — at PR2's base `develop@e58c647` `turn.py` is 3,813 lines (the Phase 0
merges moved the clusters: the stream-tap closure vars sit at :912-926,
`_close_streamed_part` at :948, `_emit_chunk` at :1030, the finalize region at
:2890-3737, `_settle_failed_finalize` at :387); after PR2 the stream-tap state machine is
deleted from `turn.py` entirely (3,629 lines) and lives in `transcript.py`. Treat the
`a933728` refs below as the design's forensic record, not as pointers into the current
tree.

---

## 1. Problem (why streaming keeps regressing)

A turn's parts are produced **twice** and then reconciled:

1. **Live** — the stream tap `_emit_chunk` (turn.py:865) via ~10 closure vars
   (turn.py:740-754: `streamed_assistant_part_id/_msg_id/_buffer`, `streamed_last_agent`,
   `streamed_last_field`, `streamed_live_part_ids`, `streamed_part_text`,
   `closed_streamed_part_ids`, `suppressed_parent_resume_offsets`), the tool observer
   (`tool_observer.py:135` `_append_live_assistant_part`), and the delegation settle path
   (turn.py:1469, :1686, :1761, :1849).
2. **Again at finalize** — turn.py:2694-3454 rebuilds parts from `pred`, `expert_handoffs`
   rows, and the `expert_terminal_answers` side-ledger (writer :1309-1313, reader :2862-2878).

Seven-plus content-matching mechanisms reconcile the two copies (§7 lists them with line
numbers). Every recent regression commit (b1b25d2, 712f5aa, f7339b1, 68c33a9) patched one
reconciliation point and moved the misfire elsewhere. Symptom issues of the same root:
#731 (order changes on reload), #732 (text/tool_call.thought duplication), #733 (non-streamed
terminal answer missing), #736 (parent re-emits the child's answer).

This is a data-flow defect, not a prompt or model defect, so per the superseding principles
(CLAUDE.md ⚑ 1-3) the fix is structural: one writer, zero finalize reconciliation. Content
comparison as a decision procedure is exactly the deterministic prose-heuristic class those
principles ban from core.

## 2. The invariant

> **The live SSE stream and the persisted assistant message are two projections of ONE
> append-only ledger.** Every part is appended exactly once, by its producer, at the moment
> it happens; the append itself publishes the wire event; finalize closes open parts, stamps
> sequence, and persists the ledger **verbatim** — no rewriting, no dedup, no re-publish.

Consequences, by construction rather than by reconciliation:

- `final_text` == concatenated deltas post-clean (the b1b25d2 invariant, now structural):
  the whole-buffer contract-prose clean (`delegation.py:660` `_clean_public_transcript_text`)
  runs **exactly once, at part close**, and the result is recorded into the close event and
  the persisted part — never recomputed, never re-derived at finalize.
- live == reload is a *type-level* property: folding the published
  `message.created/part.added/part.delta/part.completed` events reconstructs the persisted
  `Message.parts` field-for-field. It becomes a property test (§8), not a bug class.
- Duplication stops being a text-matching problem and becomes a typed **exactly-once
  contract per (LM call, agent, field)**, enforced at the producer boundary by the
  `FieldStream` handle (§3.2) — "did this handle receive deltas?" is an identity check,
  not a string comparison.

## 3. The API

New module `src/clio_agent/gact/transcript.py` (accretion rule: all new logic lives here;
`turn.py` only shrinks). Registry on `app.state.turn_transcripts` keyed by session id.

### 3.1 `TurnTranscript`

```python
class TranscriptPublisher(Protocol):
    """Injected event sink. The default impl wraps EventBus and emits BOTH vocabularies
    (message.part.* legacy + turn.text/trace.delta normalized) from ONE transition, so
    gact-tui #232's 'decide the normalized channel' choice is a publisher config flag."""
    def publish(self, event_type: str, payload: Mapping[str, Any]) -> None: ...

class TurnTranscript:
    """Single-writer part ledger for one assistant turn.

    OWNERSHIP
    - Sole minter of the assistant message id and every part id for the turn.
    - Sole publisher of message.created / message.part.added / .delta / .completed.
      (tool.call.*, semantic events, delegation lifecycle telemetry stay with their
      producers — telemetry is not transcript.)
    - Append-only; arrival order IS persisted order; a monotonic seq is stamped on
      append and becomes Part.sequence.
    - Text mutates exactly once: clean_text runs on the WHOLE buffer at close
      (text parts only; provider thinking is verbatim); the cleaned result is
      recorded into the close event AND the part. finalize() never rewrites text.
    - Thread-safe: one lock guards the ledger; events publish while holding it, so
      ledger order == bus order. Requires bus.publish to be non-blocking (it is:
      queue.put_nowait; asserted by test). Lock ordering: transcript -> bus, never
      the reverse.
    - Vendor fields preserved on the wire: final_text, stream_source, turn_id (#232).
    - Appends after close are rejected and audited (stream_audit 'late_op'), never
      silently absorbed into the next turn.
    """
    def __init__(self, *, session_id: str, turn_id: str,
                 publisher: TranscriptPublisher,
                 clean_text: Callable[[str], str]) -> None: ...

    # -- identity ------------------------------------------------------------
    message_id: str
    def ensure_message(self) -> str:
        """Mint the assistant message id + publish message.created ONCE, whichever
        producer arrives first (replaces tool_observer._ensure_live_assistant_message
        :104-132 AND _emit_chunk's lazy mint turn.py:956-981 — today two mint sites
        coordinate through app.state.live_assistant_message_ids)."""

    # -- the ONE producer API (stream tap, tool observer, delegation, finalize) --
    def append_part(self, part: Part, *, stream_source: str = "live") -> Part:
        """Atomic non-text part (tool_call/tool_result/expert_handoff/routing_decision/
        file_diff). Closes any open text part first — the runtime boundary, replacing
        the live_stream_text_boundary_hooks cross-module callback (turn.py:859-863,
        tool_observer.py:138-142) — assigns id if empty, stamps seq, publishes
        message.part.added. One locked burst."""
    def append_part_once(self, key: str, part: Part, **kw) -> Part | None:
        """append_part gated on a turn-scoped idempotency key (replaces
        app.state.live_assistant_part_keys; used for 'route:{agent}' banners)."""
    def append_text_delta(self, agent_id: str, field: str, chunk: str) -> None:
        """Streamed text/thinking delta. Opens a new part (type='thinking' for
        provider_thinking:* fields, else 'text') when (agent_id, field) changes,
        closing the prior part; publishes message.part.added on open and
        message.part.delta per chunk (+ the normalized twin, §4)."""
    def close_open_text(self) -> None:
        """Close the open streamed part: clean the full buffer once (text parts only),
        DROP the part if empty after clean (emits nothing — and reload cannot disagree,
        because the persisted list is the same ledger), else set part.text = final_text
        and publish message.part.completed{final_text, stream_source:'live'}.
        Idempotent. Absorbs _close_streamed_part (turn.py:783-841) and
        _close_streamed_part_at_runtime_boundary (:843-857)."""
    def annotate(self, part_id: str, **metadata: Any) -> None:
        """Post-hoc facts only (tool_result final preview, stream_fallback,
        restates_part_id): metadata merge, NO text change; persisted, republished as
        a metadata patch. Replaces the finalize enrichment loop (turn.py:2760-2781)
        that today mutates parts AFTER their added event went out — a live!=reload
        hole this closes."""

    # -- state queries (identity checks replace content comparison) -----------
    def has_closed_text(self, agent_id: str, field: str = "answer") -> bool:
        """Did (agent, field) already produce a non-empty closed part this turn?
        Finalize uses this INSTEAD of answered_agents / answer_already_present /
        suppressed_thinking_part substring matching (turn.py:2859-2914)."""
    def streamed_text(self, agent_id: str, field: str) -> str:
        """Concatenated deltas for (agent, field) THIS turn — subsumes the
        app.state.live_streamed_field_text buffer (turn.py:763-781) including
        fix/757's turn-scoping (the ledger is turn-scoped by construction)."""
    def open_text_part(self) -> Part | None: ...
    def snapshot(self) -> list[Part]:
        """Read-only arrival-order view; also exposed as
        app.state.live_assistant_parts[sid] during migration (PR1/PR2) so untouched
        finalize reads keep working."""

    # -- the reader (finalize; loop-only) --------------------------------------
    def finalize(self) -> list[Part]:
        """close_open_text(); stamp 1-based sequence; freeze; return parts VERBATIM
        for the persisted Message. No text rewriting. No dedup. No re-publish."""
```

### 3.2 `FieldStream` — the exactly-once text channel (grafted from the op-log design)

The non-streaming provider is today a finalize special case (`expert_terminal_answers` +
`answer_already_present`). Instead, every LM-call site takes a typed handle **before** the
call and finishes it after:

```python
class FieldStream:
    """Exactly-once channel for one (agent_id, field) within one LM-call scope."""
    part_id: str | None                      # None until the first delta opens the part
    def append(self, chunk: str) -> None     # tap calls this; opens lazily on first chunk
    def finish(self, *, fallback_text: str = "") -> str | None:
        # deltas streamed          -> close the part (clean once); fallback audited+ignored
        # no deltas + fallback     -> ONE open+delta+close burst authored to (agent, field)
        # neither                  -> None
```

`stream.finish(fallback_text=pred.answer)` replaces the #733 fallback and the
`answer_already_present` comparison: whether the batch text lands is decided by
**op identity** (did this handle stream?), never by comparing strings. The
`expert_terminal_answers` side-ledger disappears; the raw answers still ride
`expert_handoffs` metadata for the trace.

### 3.3 `TurnTranscriptRegistry`

```python
class TurnTranscriptRegistry:
    """app.state.turn_transcripts. Lifecycle owned by the turn loop."""
    def open_turn(self, sid, turn_id, publisher, clean_text) -> TurnTranscript: ...
    def get(self, sid) -> TurnTranscript | None      # producers resolve here
    def close(self, sid) -> None
        # Called in the success path AND inside fix/756's finalize error envelope
        # AND on the ask_user early return (turn.py:2372-2463 exits the turn before
        # the finalize region) — a leaked ledger must never poison the next turn
        # (the failure class fix/757 fixed for the field-text buffer).
```

## 4. Event-emission mapping (one transition = one emission site)

Finalize re-publishes **nothing** that is already in the ledger; the finalize re-publish
loop (turn.py:3296-3376) is deleted whole.

| # | Ledger transition | Wire events |
|---|---|---|
| 1 | `ensure_message` (first append of the turn) | `message.created` (empty `parts`, `turn_id`) — exactly once regardless of which producer arrives first. Today two mint sites (turn.py:956-981; tool_observer.py:104-132) coordinate through a shared dict. |
| 2 | `append_text_delta` with `(agent, field)` change | `close_open_text` on the prior part, then `message.part.added` {part.to_wire(), stream_source: "live", signature_field_name} for the fresh part (today: turn.py:985-1029). |
| 3 | `append_text_delta` chunk | `message.part.delta` {part_id, delta.text_append, signature_field_name, stream_source: "live"} **plus** the normalized twin from the same call site: `turn.trace.delta` for `provider_thinking:*` fields, `turn.text.delta` otherwise (today double-published from turn.py:1032-1096; the #232 single-vocabulary decision becomes a publisher flag, not a code hunt). |
| 4 | `close_open_text` | `message.part.completed` {part_id, final_text: cleaned-whole-buffer, stream_source: "live"}. Empty-after-clean parts are removed from the ledger and emit nothing (today's `dropped_empty_streamed_part`, turn.py:808-825 — but reload can no longer disagree). |
| 5 | `append_part` (tool_call / tool_result) | `message.part.added`. The tool observer still separately emits `tool.call.started/completed` + `turn.action.added` — telemetry channel, unchanged. A later `annotate(part_id, ...)` patches persisted metadata (replaces the finalize enrichment mutation, turn.py:2760-2781). |
| 6 | `append_part` (expert_handoff / routing_decision) | `message.part.added` exactly once. There is no finalize rebuild-from-rows, so `live_handoff_sigs` / `live_routing_agents` have nothing to suppress; `expert_handoffs` rows remain message **metadata** only. |
| 7 | `FieldStream.finish` on the batch path | `message.part.added` {stream_source: "batch", stream_fallback} + `message.part.completed` {final_text, stream_source: "batch"} — the same shape today's finalize loop emits at turn.py:3331-3363, now via the one API. |
| 8 | `finalize()` | The loop publishes `message.completed` {stop_reason, tokens, cost_usd, error_info?, metadata} + `turn.completed` + the semantic `turn.completed/failed` with `final_message` == the persisted ledger (unchanged from turn.py:3381-3428), then `registry.close(sid)`. |

**Fold invariant** (enforced by the property test, §8): folding the published
`message.created + part.added + part.delta + part.completed` stream reconstructs exactly the
`Message.parts` that `_append_session_message` persists (turn.py:3433) — field-for-field:
id, sequence, type, agent_id, text/final_text, stream_source.

## 5. Parent echoes: tag, don't suppress (grafted from the op-log design)

Today the #736 duplicate is fought twice with content heuristics: live chunk suppression
against the resume payload with a word-boundary guess (`suppressed_parent_resume_offsets`,
turn.py:916-953) and a finalize byte-identical cross-agent drop (`_dedup_cross_agent_text`,
turn.py:93-119, applied :3098) — plus `dedupeRepeatedText` in the client. Three
approximations of one fact.

The ledger keeps the echo as **truth** and tags it: at `close_open_text`, if the closed
answer part's text exactly equals the typed parent-resume payload the runtime itself
injected (one exact comparison against data *we* produced — a reality check per ⚑ principle
2, not a prose heuristic), `annotate(part_id, restates_part_id=<child part id>)`. The TUI
collapses tagged parts; live and reload agree because the tag is persisted with the part.
Both suppression mechanisms and the client-side `dedupeRepeatedText`/`dedupToolThought` are
then deleted (#232's single-dedup-owner rule — server owns it, and "owning" means labeling,
not scrubbing). If echo *volume* is the real complaint, the root fix is the parent-resume
prompt, not core scrubbing.

## 6. Producer migration — PR-by-PR

Five PRs, each green, each shrinking `turn.py` (accretion rule: `system-cleanup-2026-07.md`
§1.7). Ordering is designed around the in-flight Phase 0 branches (§10).

- **PR1 — `feat(gact): TurnTranscript ledger + registry; migrate producers 1+2 (tool
  observer, delegation).`** MERGED as `d4159db`. New `transcript.py` + unit tests. `tool_observer.py`'s
  `_append_live_assistant_part` (:135), `_append_live_assistant_part_once` (:166), and
  `_ensure_live_assistant_message` (:104) become one-line shims into the registry (falling
  back to the legacy dicts when no turn is open), preserving every test seam — tests
  monkeypatch these through the `app.py` re-exports (app.py:1190-1193), and the transcript
  call sits *inside* the shim body. `app.state.live_assistant_parts[sid]` becomes an alias
  of `transcript.snapshot()` so `turn.py`'s finalize reads work unchanged. `turn.py`
  untouched → lands before/parallel to fix/756's large turn.py diff with zero conflict.
- **PR2 — `refactor(gact): migrate producer 3 (stream tap).`** Implemented on
  `feat/767-turn-transcript-pr2` (cut after fix/756/757/761 merged). `_emit_chunk`'s
  open/delta/close state machine, per-part buffers, boundary hook,
  and lazy message mint move into `append_text_delta`/`close_open_text`/`ensure_message`;
  `_emit_chunk` shrinks to an adapter (semantic `lm.token.delta` + `stream_audit` +
  transcript call; the parent-resume gate temporarily retained until PR4). Deletes the
  closure vars and `live_stream_text_boundary_hooks`. Landed at -184 `turn.py` lines
  (3,813 → 3,629). **Disclosed adaptations against this entry as written:**
  (a) the turn-loop LIFECYCLE (open at turn start; settle in the success path, inside
  fix/756's `_settle_failed_finalize`, and at the ask_user early return) ships in PR2,
  pulled forward from the PR3 bullet — settling uses `abandon()` (freeze WITHOUT
  closing/publishing) because the legacy finalize region still owns the terminal wire
  events during the PR2 window; PR3 swaps it for `finalize()` + verbatim persist.
  (b) the error partials read `transcript.raw_streamed_text()` — a whole-turn,
  cross-(agent, field) arrival-order aggregate added to the API — because the per-field
  `streamed_text` cannot reproduce the legacy `streamed_assistant_buffer` concat
  byte-identically (§9 error/cancel partials).
  (c) finalize's remaining reads of the deleted closure vars are re-derived from two
  legacy-equivalent queries added to the API: `current_stream_part_id` (mirrors
  `streamed_assistant_part_id`: set on open, cleared only at the atomic-append runtime
  boundary, NOT by close) and `was_closed_live(part_id)` (mirrors
  `closed_streamed_part_ids`, empty-drops included).
  (d) `ensure_message` on a frozen, never-minted ledger now raises the structured
  `TranscriptFrozenError` (audited late_op) instead of silently returning `""`.
  (e) the ask_user pause CARRIES the in-flight assistant message across the question
  today (same message id, pre-question live parts persist into the resumed turn's
  message); PR2 encodes that explicitly — the early return settles the ledger, the
  legacy dicts keep the carried state, and the resume turn's open adopts it via
  `adopt_carried_state` (fresh-ledger-only, publishes nothing) so the wire stays
  byte-identical while a leaked ledger still cannot poison a later turn (the registry
  evicts real leaks loudly).
  Wire equivalence is enforced by the develop-captured goldens
  (`tests/test_gact/goldens/turn_transcript_pr1/` + the PR2
  `error_envelope_turn` golden).
- **PR3 — `refactor(gact): finalize becomes a reader; delete the reconciliation block.`**
  Finalize appends routing_decision via `append_part_once("route:{agent}")` (same key the
  live observer uses), thinking gated by `has_closed_text(responder, "reasoning")`, the
  canonical answer via `FieldStream.finish(fallback_text=answer_text)` — replacing the
  `reuse_streamed_part_id` text-swap (:2832-2852, :3079-3097, :3311-3330): the streamed
  part's close already carries the cleaned buffer as `final_text`, so there is nothing to
  swap — and file_diffs; then persists `transcript.finalize()` verbatim inside fix/756's
  error envelope, with `registry.close(sid)` in both the success path and
  `_settle_failed_finalize`. Deletes mechanisms 1-5 (§7), `expert_terminal_answers`, and
  the re-publish loop (:3296-3376). ~-400 lines. The live==reload property test (§8) turns
  on for the whole gact suite here.
- **PR4 — `refactor(gact): single dedup owner — tag parent echoes, delete suppression.`**
  Implements §5: `restates_part_id` at close; deletes `suppressed_parent_resume_offsets`
  (:916-953) and `_dedup_cross_agent_text` (:93-119, :3098); retires
  `app.state.live_streamed_field_text` entirely (turn.py:763-781 + the fix/757 helpers in
  `streaming.py`/`lm_activity.py`) in favor of `transcript.streamed_text`, and
  `live_assistant_part_keys`. gact-tui removes `dedupeRepeatedText`/`dedupToolThought` only
  after a release of parity data — never delete both layers in one release.
- **PR5 — `chore(gact): vocabulary + shim cleanup.`** Normalized
  `turn.text.delta`/`turn.trace.delta` emission moves fully into the `TranscriptPublisher`
  (single site) gated per the #232 decision; delete dead `app.state` keys, the `app.py`
  re-exports for removed helpers, stale docstrings; SPEC.md note for the `message.part.*`
  contract. Pure deletion + docs. Client-visible only at the #232-gated vocabulary switch.

Estimated net: ~+1,000 / -1,200 lines; `turn.py` 3,618 → roughly 2,900 before fix/756's own
decomposition of `_run_turn_in_background`, which compounds.

## 7. Deletions unlocked

| # | Mechanism (evidence) | Replaced by |
|---|---|---|
| 1 | `live_routing_agents` set (turn.py:2739-2743) + not-in-live guard (:2784) | `append_part_once("route:{agent}")` — appended once, live or at finalize |
| 2 | `live_handoff_sigs` (:2751-2755) + finalize skip (:3305-3309) + `[] if live_has_expert_handoff else expert_handoffs` rebuild (:2807) | delegation appends handoff parts once at emit time; rows stay metadata |
| 3 | `closed_streamed_part_ids` (:753, populated :817/:826, consulted :3301) | closing is a ledger state transition; finalize publishes nothing |
| 4 | `answered_agents` scan (:2859-2861) + `expert_terminal_answers` side-ledger (writer :1309-1313, reader :2862-2878, cleanup :3438) | `FieldStream.finish(fallback_text=...)` — exactly-once by op identity |
| 5 | `answer_already_present` text comparison (:2912-2914) + `reuse_streamed_part_id` swap machinery (:2832-2852, :3079-3097, :3311-3330) | the answer part is the closed streamed part OR one batch burst; never both, never swapped |
| 6 | `_dedup_cross_agent_text` (:93-119, applied :3098) | `restates_part_id` tag (§5); client collapses |
| 7 | `suppressed_parent_resume_offsets` chunk suppression (:754, :916-953) | same tag; no live scrubbing |
| — | `suppressed_thinking_part` gates (:2879-2894, metadata :3044-3045) | `has_closed_text(responder, "reasoning")` |
| — | finalize re-publish loop (:3296-3376) | events go out at append time |
| — | `live_stream_text_boundary_hooks` (:859-863; tool_observer.py:138-142) | `append_part` closes open text under the ledger lock |
| — | `live_assistant_message_ids`, `live_assistant_part_keys`, `streamed_assistant_buffer` + per-part buffers, `app.state.live_streamed_field_text` (:763-781) incl. fix/757's helpers | transcript identity, `append_part_once`, `streamed_text` |
| — | finalize tool_result enrichment mutation (:2760-2781) | `annotate()` |
| — | downstream (gact-tui): `dedupToolThought`, `dedupeRepeatedText` | after PR4 + one release of parity data (#232) |

## 8. Test strategy

1. **Unit tests for the module** (`tests/test_gact/test_turn_transcript.py`):
   open/delta/close lifecycle; `(agent, field)` change splits parts; empty-after-clean part
   is dropped and emits nothing; atomic append closes open text first; `append_part_once`
   idempotency; 1-based arrival-order sequence; `clean_text` called exactly once per text
   part on the whole buffer; `message.created` emitted once whichever producer arrives
   first; `FieldStream.finish` truth table (streamed / fallback / neither); late-append
   audit; a thread-interleaving test (N executor threads appending tool parts while the
   loop streams deltas → ledger order == event order, no lost/duplicated parts) plus an
   assertion that `bus.publish` is non-blocking (the emit-under-lock precondition).
2. **The live==reload property test**, two forms: (a) transcript-level randomized generator —
   any interleaving of `append_part` / `append_text_delta` / `close_open_text` /
   `FieldStream.finish`; fold the published events into a reconstructed parts list and
   assert it equals `finalize()` output field-for-field (id, sequence, type, agent_id,
   text/final_text, stream_source); (b) end-to-end parity — an autouse-within-`test_gact`
   conftest bus subscriber that folds SSE events during any TestClient turn and, at
   `message.completed`, asserts fold == `GET /v1/sessions/{sid}/messages` persisted parts,
   so every existing scenario (streaming, SSE, thinking blocks, tool-thought dedup,
   delegation, cancellation, fix/756's finalize-error envelope) doubles as a regression for
   exactly the class this epic ends.
3. **Deleted-mechanism replacements:** keep each original symptom fixture (#731 order on
   reload, #732 tool_call.thought duplication, #733 non-streamed terminal answer, #736
   parent echo, the b1b25d2 reasoning→answer swap) but flip the assertion from
   "reconciliation removed the duplicate" to "the producer appended exactly once" (count
   parts by `(agent_id, type, field)` in both the folded stream and the persisted message;
   for #736, assert the echo part carries `restates_part_id`).
4. **Targeted regressions:** cancelled mid-stream turn (live parts persist,
   stop_reason=cancelled, fold==reload holds); ask_user early return closes/carries the
   ledger (next resume turn does not adopt a stale message id).

Gate per PR: full `pytest tests/` green, `ruff check src/` clean, baseline CLI smoke
(CLAUDE.md Rule 2).

## 9. Risks

- **fix/756 rebase collision.** fix/756 rewrites `turn.py` heavily (finalize try-wrap +
  `_settle_failed_finalize`). Mitigation: PR1 touches only `transcript.py` +
  `tool_observer.py`; PR2/PR3 are cut after 756/757/761 merge, and PR3's `finalize()` sits
  inside 756's envelope with `registry.close(sid)` on both paths — a leaked ledger would
  poison the next turn, the exact failure class fix/757 closed for the field buffer.
- **Cross-thread ordering.** Tool observer + delegation append from executor threads while
  the tap appends on the loop; today ordering is an unlocked `list.append` plus a
  cross-thread boundary-hook call (already racy). The transcript lock serializes it, but
  publishing under the lock couples us to `bus.publish` being non-blocking — assert it in a
  test; document lock ordering (transcript → bus, never reverse).
- **Alias-view aliasing (PR1/PR2 window).** `_close_streamed_part` today mutates
  `Part.text` in place on the shared live-parts object (turn.py:827-828); the transcript
  must preserve mutate-in-place close semantics until PR3 removes the external readers,
  else finalize sees stale empty-text parts and re-drops them.
- **Test-seam coupling.** Tests monkeypatch the helpers through `app.py` re-exports
  (app.py:1190-1193). Shims delegate, never move; monkeypatching the shim still intercepts.
- **Error/cancel partials.** `_TurnTimedOut`/`StreamingOutputError` paths derive
  `answer_text` and `partial_output`/`stream_source` from `streamed_assistant_buffer`
  (turn.py:2625-2653, a whole-turn concat across agents/fields); the
  `transcript.streamed_text` replacement must keep those error details byte-identical —
  covered by the targeted regression in §8.4.
- **Parent-echo tag misses.** If the parent paraphrases instead of restating verbatim, the
  exact-match tag won't fire and the echo renders twice — which is *honest* (it is two
  different texts) but may read as regression against today's fuzzy suppression. Fallback
  is fixing the parent-resume prompt (the root), never re-adding content heuristics;
  `stream_audit` counters on tag hits/misses give the parity data #232 wants before the
  client dedupe is deleted.
- **Event-volume back-compat.** PR1-PR4 change nothing on the wire (both vocabularies
  preserved, same event shapes). PR5's single-vocabulary switch is client-visible and ships
  only behind the #232 conformance suite and a gact-tui release gate.

## 10. Alignment notes

**gact-tui #232 (protocol convergence).** This design implements the server side of two
#232 checklist items: (a) *one dedup owner* — after PR4 the server labels
(`restates_part_id`) instead of scrubbing, and the client deletes
`dedupToolThought`/`dedupeRepeatedText`; (b) *decide the normalized `turn.*` channel* — the
`TranscriptPublisher` makes both vocabularies come from one emission site, so
finishing-the-consumer vs stop-double-publishing is a config flag flipped in PR5 under the
conformance suite. Vendor fields clients already depend on (`final_text`, `stream_source`,
`turn_id` — per #232's reverse-engineering note) are preserved on every event.

**Phase 0 semantics (fix/756, fix/757, fix/761).** At this doc's baseline
(`develop@a933728`) these are complete on their branches, pending merge:
`fix/756-finalize-error-envelope` (12a9863 — finalize wrapped in an error envelope with
`_settle_failed_finalize`), `fix/757-clear-live-stream-buffer` (19768f4 —
`live_streamed_field_text` cleared at turn end, suppression scoped per turn),
`fix/761-heartbeat-replay-watchdog` (be1aba5 — heartbeats out of SSE replay, per-session
watchdog). The design *builds on* their semantics rather than re-implementing them: PR3's
persist runs inside 756's envelope and `registry.close(sid)` joins
`_settle_failed_finalize`; 757's turn-scoping is **subsumed** (the ledger is turn-scoped by
construction and `registry.close` is its cleanup — its `streaming.py`/`lm_activity.py`
helpers retire in PR4); 761's non-blocking bus delivery is the precondition for
publish-under-lock (§3.1) and is asserted by test. Sequencing: PR1 is independent and can
land first; PR2+ rebase on the merged Phase 0 branches.

**#767 epic checklist.** This doc covers the first four boxes (TurnTranscript + producers;
delete the seven mechanisms + side-ledger; unify the double vocabulary; end the
text/tool_call.thought duplication). The remaining boxes (`_run_turn_in_background`
decomposition, EarthScope vocabulary extraction #646/#648, app↔turn shim dissolution #714)
are follow-on work this design deliberately enables — PR5 leaves the shims one deletion
away — but does not include.
