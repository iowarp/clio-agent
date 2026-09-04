# Campaign: Land the references / child-interaction integration wave

**Status:** LANDING (fix train AF1 → AF-FOLD → AF-IMG → AF-FINAL complete on
`review/campaign-backend`, PR [#1298](https://github.com/iowarp/clio-agent/pull/1298)
→ `develop`).
**Executor:** Codex (three repos). **Reviewer / re-lander:** Claude (this repo's review session).
**Keep this doc updated as work lands** (house rule; cf. `system-cleanup-2026-07.md`).

This is the record for the **integration wave**, the successor to the composer/workspace-resource
wave recorded in [`composer-pipeline-landing-2026-09.md`](composer-pipeline-landing-2026-09.md).
That document stands as written; this one does not amend it. Where the two overlap — the resource
custody lane, the message-intent store, the `resource_ref` part — this wave builds on what that
landing shipped rather than re-litigating it.

---

## (a) The wave as delivered

Three repositories, three PRs:

| Repo | Branch / PR | Reviewed head | Landing state |
| --- | --- | --- | --- |
| clio-agent | `codex/campaign-integration-backend` — [#1298](https://github.com/iowarp/clio-agent/pull/1298) *"feat: integrate references and child interaction contracts"* | `58547561` | Review branch `review/campaign-backend`; **44 commits** of review fixes on top of the reviewed head, ending with this record |
| gact-tui | `codex/campaign-ui-integration` — [#387](https://github.com/iowarp/gact-tui/pull/387) *"feat: integrate references and child attention UX"* | `da01c63c` | Review branch `review/campaign-ui` @ `04f654ef`; open |
| clio-agent-marketplace | `codex/factorio-flat` — [#59](https://github.com/iowarp/clio-agent-marketplace/pull/59) *"feat: add Factorio Flat research pack"* | `34cec6c6` | **MERGED** to `main` @ `22a0cea8` (2026-09-03) |

The clio-agent delta against `develop` is **173 files / +16,863 / −1,257** (src alone: 125 files,
30 of them new, +9,538 / −1,155). The visible feature is attention and reference UX; the substance
is backend: structured context references with their own search/admission/delivery pipeline, a unified
runtime-interaction surface (`/v1/sessions/{sid}/interactions` + one `/respond` door over questions,
permissions and A2UI), a durable ask-user question ledger with restart recovery, child-work
projection into provenance, native multimodal inputs on both the Claude Code and Codex provider
paths, and a session-MCP inventory surface.

### The post-review parallel pushes

Review of `58547561` was already under way when Codex pushed two further commits, one per lane,
**without disclosing them in the PRs**:

- clio-agent `ad7ecf7b` *"fix(gact): preserve campaign integration contracts"* — this commit
  contains, among new work, **cherry-picks of three commits from this review branch's own fix
  train** (`62b0003d`, `eefd12fe`, `d397f370` are patch-id identical to our `a51a8986`,
  `ae487a6b`, `299a8a05`), plus a **partial revert** of the review branch's off-loop
  `routes/messages` fix. The chronology is unambiguous: our three land at 06:16, 06:31 and
  06:56 (−05:00); the three cherry-picks are stamped 06:57:24, 06:57:24 and 06:57:25 — one batch,
  seconds later — and `ad7ecf7b` itself at 09:48. The genuinely new surface in it is the
  session-MCP inventory, the Claude multimodal provider path, the child-projection
  `include_children` gate, and the `descendant_session_ids` substrate.
- gact-tui `7fc94e2a` *"test(web): refresh linux transcript baseline"*.

Both were folded rather than rejected wholesale (see (c)); the cherry-picks are recorded here as
fact, not as a complaint, because they are why every conflict in the reference-resolution files
resolved to ours and why the merge commit needed an explicit resolution table.

---

## (b) Review verdicts per lane, at the reviewed heads

Reviewed per the `codex-review` method, one adversarial pass per lane against `58547561` /
`da01c63c` / `34cec6c6`. The designs are sound and the store layers are well built; every lane
nonetheless carried at least one blocker, and the blockers cluster into five classes.

### Blocker classes

1. **Work on the event loop.** The async `POST /messages` handler ran the whole reference
   pipeline inline — digesting every referenced file with no size cap, then reading them again and
   folding every message part of every session in the workspace. Five of `search_workspace_references`'
   six producers were likewise on the loop, as was the session-MCP inventory snapshot, which takes a
   `threading` lock a live turn holds. Every one of these stalls every other session's SSE stream.
2. **Unbounded or unreleased in-process state.** An armed ask-user deadline created an unreferenced
   daemon `threading.Timer` that was never cancelled when the question settled — one live closure
   over the app per answered question, for up to a day, plus one more per question on every restart.
   Reference-discovery degradations accumulated in a flat, never-reset, process-lifetime list.
3. **Fabricated attribution and fabricated capability.** `by_session.get(sid, by_session[root])`
   handed a session fork the ROOT's `task_id`/`task_path` — a fabrication indistinguishable from a
   real attribution, which then minted a `contains` edge onto a node the graph never had.
   `models_source` was hardcoded to `"live"` whenever any model existed, so zero-network handshakes
   claimed a probe they never ran. The Codex modality read took `Model.input_modalities` straight
   off the SDK object, whose schema default is `["text", "image"]`, manufacturing an image
   capability for every wire row that omitted the field. The `claude_code` modality probe attached a
   1×1 transparent pixel and an empty PDF, asked for `"ok"`, and stamped `["text","image","pdf"]` on
   any non-error reply — a CLI that stripped both attachments proved both modalities, and three
   tests asserted that constant against itself.
4. **Degradations that reach only the log.** Typed reasons were computed and dropped: the A2UI
   store's quarantine reasons, the reference-discovery failures, the `elicitation_correlation`
   blind `except`, six distinct fleet-state facts collapsed into one bare `{}` reported as
   `available`, and five `None`-returning conditions on the native-attachment path that could make a
   user's attachment simply not arrive with no reason recorded anywhere.
5. **Scope and vocabulary holes.** The A2UI `approval.respond` handler resolved a permission by bare
   id with no session check, so a surface in session A could grant a tool call parked in unrelated
   session B; the interaction responder reached `dispatch_action` with no `x-a2ui-version`
   negotiation while the canonical route 406s without it; `provenance/normalization` classified four
   prefixes nothing emits; a day-one `/response` alias duplicated `/respond`.

### Verdicts

- **clio-agent @ `58547561` — NOT MERGEABLE AS-IS → fixed forward on `review/campaign-backend`.**
  All five classes present. Unlike the composer wave, the branch was *not* a stale fork, so it was
  fixed forward on `review/campaign-backend` rather than rebuilt; PR #1298's head ref remains
  `codex/campaign-integration-backend`, and the review branch is what lands there.
- **gact-tui @ `da01c63c` — NOT MERGEABLE AS-IS → fixed forward on `review/campaign-ui`.**
  Classes 3, 4 and 5 in client form: raw ids leaking into observability text, a foreign owner
  attributed by invented role rather than by the viewed session, a task row named for its token
  instead of what ran, a relay degradation shown without the relay's own reason, and interaction
  kinds asserted by `data-*` attributes rather than visible content.
- **clio-agent-marketplace @ `34cec6c6` — MERGEABLE after a fix round → MERGED @ `22a0cea8`.**
  The pack's grader was class 3 in miniature: outcomes graded on *tool names* rather than real
  runtime signals, a trace allowed to contradict itself, and a completed child that returned nothing
  still able to ground an answer.

---

## (c) The fix train

Four rounds on `review/campaign-backend`, 42 commits, each finding proven failing-first.

### AF1 — the interaction and reference lanes (twelve findings plus three follow-ons)

Twelve numbered findings landed as `62b0003d` → `95efb87a`:

| Finding | Fix |
| --- | --- |
| F1 | The reference pipeline moves off the loop behind a `PreparedReferences` seam computed in a worker *before* the synchronous acceptance path (which must stay on the loop — it hands turns to `TurnRunner.spawn`). `accept_message` then skips the authorization it was handed, so the promote door authorizes exactly once instead of twice. All six discovery producers run in one worker. `gact.context_references.max_hashable_bytes` refuses a file too large to digest with a typed `context_ref_too_large` naming limit and actual size — never a silently skipped digest behind a pinned revision. |
| F2 | The ask-user expiry timer is retained in an app-scoped `ask_user_deadlines` registry and released from `claim_question_transition`, the one serialization point every answer/cancel/expiry already goes through. Re-arming an id cancels its predecessor. The window's default and clamp become `gact.ask_user.ttl_s` / `gact.ask_user.max_ttl_s`. |
| F3 | `approval.respond` refuses a permission id outside the dispatching surface's session scope (itself plus its spawned descendants) as *not found*, so existence does not leak. Proving it end-to-end exposed the action being unroutable at all: `_validate_value` treated every key named `action` as a nested envelope, so both doors 422'd on their own required field. An action/event `context` subtree is now free-form — the safety rules still apply, only the structural envelope rule is lifted. |
| F4 | `x-a2ui-version` negotiation is hoisted *into* `dispatch_action`, so both doors refuse an un-negotiated action identically; the comment claiming negotiation had already happened was false and is corrected. |
| F5 | A `context_ref{ref_kind: resource}` is MAPPED onto the `resource_ref` part the composer already delivers, revision-checked against the custody record — one attachment mechanism, not two. Every search row states its `part_type`. |
| F6 | The recorded-snapshot lookup comes first for summary kinds, making the stale-retry resilience path reachable: a `context_frame` or diff goes stale inside its own turn, so an ADMITTED message used to become permanently un-retryable. Identity and ownership are still verified; only currency is not, and the re-delivered block carries a typed stale marker. |
| F7/F8 | Parked inputs are released and a frozen queue head reports itself. |
| F9 | The interaction projection is bounded by `gact.interactions.projection_limit` *before* the ledgers are walked (it used to trim finished rows after every walk), and reads this app's task store rather than the process-global one. |
| F10/F11 | Dead vocabulary deleted (four never-emitted prefixes, `.answered`/`.responded` suffixes, the `/response` alias, `_question_from_interaction_id`'s bare-id fallback); `elicitation_correlation`'s blind `except` logs the house-style structured reason, and the downgrade it causes carries a typed marker naming declared vs projected kind. |
| A4 F19 | The interaction responder's permission branch forwards the P2.6 intercept payload, so approve-with-modified-args is no longer silently downgraded to a plain allow. |

Three follow-ons its own tests exposed: reference-discovery degradations served to the client
(`549fe862`) and then **scoped per workspace and reset per search** (`0498bcbf`) — the first fix
stored them in one flat process-lifetime list, so a client searching a healthy workspace saw another
workspace's failure and a recovered repository reported its old failure forever; the
reference-admission gate refusing a still-uploading resource on *both* doors (`4801e4ac`); and the
promote door's exactly-once authorization test (`95efb87a`), verified by reverting the skip guard
and watching it fail `2 == 1`.

### The fold — resolving `ad7ecf7b`

`923b4dc4` folds the undisclosed push. Resolution rulings, each recorded in the merge body:

- **`routes/messages` → OURS (his revert rejected).** His hunk re-gates `accept_message_async` on
  `context_ref` presence, which puts enrichment *and* his own 250 MB resource `read_bytes` back on
  the event loop. His stated hazard — a `TestClient` lifetime problem — **is refuted by test**:
  the streaming suite's own fixture note records that constructing `TestClient` without entering it
  gives each request a *transient* portal, so a turn that correctly outlives `POST /messages` races
  that portal's teardown. The fix is one app-lifetime portal in the fixture, which the suite now
  holds. A harness/task-ordering problem is not a reason to revert a production off-loop fix.
- **`elicitation_bridge` → OURS.** His `_prune_terminal_questions` (and its
  `gact.ask_user.max_terminal_history` knob) is superseded by the ledger funnel; `LoopInbox.snapshot()`
  is kept because it is clean and the init-failure path needs it.
- **`provenance/normalization` → OURS.** His hunk re-adds `question.` / `interaction.` prefixes that
  nothing in this tree emits.
- **`agent_initialization` / `composer_runtime` → OURS, insights ported.** His two genuine insights
  land in follow-up commits, pinned by his own two tests (kept, failing, until they did).
- **Config → both sides' keys minus the one belonging to the dropped pruner.**

Two rulings from that fold are worth stating as principles rather than as file decisions:

- **Refuse-and-retain over drain.** Our init-failure path drained every parked inbox item to
  "release" it, then published `recoverable: False` alongside `recovery_actions` — internally
  inconsistent and wrong on the substance, because construction failing is not terminal for the
  process: `PUT /v1/providers/lm` builds a fresh agent and drains exactly those inboxes. Draining
  them here threw away an answer the user had already given, on the one path that could still
  deliver it. The typed refusal and the session's `error` status stay; the item stays queued and the
  durable steer intent stays `pending`, with `recoverable: True` / `retained: True` naming the
  rebind door. A second test proves the retention **on the real recovery path** rather than
  asserting it.
- **The invalid-status store boundary.** His `status="failed"` is out of `Session.status`'s
  vocabulary — and that whole class of bug was invisible: `SessionStore.update` guards
  mode/edit_mode/routing_mode/approval_mode against their vocabularies and then assigns `status`
  unchecked onto a plain dataclass, so an invented status persists silently and detonates at the
  wire boundary, where one bad row 500s `GET /v1/sessions` for **every other session**. The write is
  now refused at the store, and a store that already holds a bad row resolves it to `error` at
  `to_wire` rather than taking the listing down.

### AF-IMG — the provider modality paths

**Evidence, never fabrication** — the single principle behind this round, applied identically to
both providers:

- **Codex** (`7f9e3c25`): capabilities come only from `model_fields_set`. The pinned `openai_codex`
  SDK declares `Model.input_modalities` with a schema default of `["text","image"]`, so reading the
  attribute manufactured an image capability for every wire row that omitted the field, and the
  typed negative could never fire in production. An omitted field records *no* modality plus a typed
  `modality_unreported` reason that rides the overlay row into the passive handshake. The tests
  built rows from `SimpleNamespace`, which cannot reproduce the omitted-field case at all; they now
  construct real `openai_codex` `Model` rows.
- **claude_code** (`4584b300`): each probe mints a fresh four-digit code, renders one *into* the
  image and prints the other *into* the PDF, and requires the reply to quote both back. Codes are
  per-probe (unmemorisable) and always distinct (one guess cannot satisfy both), and each modality
  is judged independently, so a CLI that forwards the image but strips the PDF is recorded as
  exactly that. Anything unquoted is typed-unreported. A multimodal probe that cannot answer no
  longer rejects the model or sinks discovery: it retries once as text-only and, if that validates,
  the alias is accepted with text-only capabilities plus a typed `modality_probe_unavailable` reason
  carrying the native failure. A definitive 404 is never retried. The probe assets are built from
  the standard library and tested by **re-deriving each code from the rendered pixels and the PDF
  content stream**, so a broken renderer fails there instead of making every model look
  modality-incapable.
- **Catalog provenance** (`44531cdc`): `models_source` is reported by the handshake that ran — `live`,
  `overlay` or `static`. Evidence timestamps are the evidence's own (`generated_at` threaded through
  `cli_catalog` onto `ModelProfile`), with the read's clock reported separately as `read_at`. The
  overlay is consulted at READ time against `providers.model_catalog_ttl_s`; a served-but-old entry
  still serves its prior list per the documented contract but carries a typed staleness marker. A
  provider bind invalidates `app.state.provider_catalog` — leaving the previous provider's snapshot
  in place decided what bytes reached a model it never described. The vision gate's literal
  `{"openai","anthropic"}` name allowlist is gone.
- **The attachment boundary** (`69834f01`): conf-resolved per-image / per-document / per-request
  ceilings, checked on SOURCE bytes (a recorded size, or a base64 length computed arithmetically) so
  nothing is expanded to discover it was too big. Parts are classified by declared type instead of
  key presence. Remote image URLs are refused unless the host is in a conf allowlist, and permitting
  one records a typed egress line. Every decline reason reaches the delivery ledger, and the turn
  settles each native-planned row after the attach pass with `delivery_confirmed`.
- **The native-input gate** (`e283bd1e`): `_try_streamed_forward` injected `images=[]`/`files=[]`
  into every streamed forward on all three rungs of the compat ladder, so an ordinary *imageless*
  turn on a pre-multimodal module failed as a streaming error. The injection is gated on the same
  `_agent_accepts_images` predicate the turn path uses, so gate and dispatch cannot disagree.

### AF-FOLD — owner modules and the substrates the campaign grew

`e76a0f16` extracted rather than baselined away: `gact/session_descendants.py` (the two-substrate
descendant walk, its attribution vocabulary, the shared `MAX_SPAWN_DEPTH`, the delete-time task
purge), `tools/workspace_root.py` (`canonical_workspace_root` — the one key rule the fleet registry,
the leases, the reaper and the inventory reader must agree on), `tools/mcp_redaction.py` (credential
redaction, which stopped being a footnote once argv and url joined headers/auth/env as carriers —
`b10aa3b5` proved a declaration like `ndp-server --token ${NDP_TOKEN}` reached the wire, because
`expand_env` runs at construction). `4f719f26` then audited the walks the slice itself authored:
`descendant_sessions` scanned the whole session store once per parent per depth (the very cost it
exists to bound) and is now indexed once per walk; truncation is reported from the walk already done
and only when something below the cap is genuinely unvisited.

### AF-FINAL — the hardcoded-tunables sweep

Owner standing order, run over the **whole** branch delta (`git diff origin/develop...HEAD -- src/`),
wave plus every fix slice. 209 numeric literals in operational positions across 37 files; the
disposition table is in [§ AF-FINAL sweep](#af-final-sweep) below. Seven escaped tunables migrated;
zero raw `os.environ` reads were added anywhere in the delta.

---

## (d) Deviations, with rationale

**1. A second degradation ledger beside the single `stream_fallback` slot.** The existing slot
answers exactly one question — how was this turn's text delivered? — is single-valued by design, and
a live-streamed turn discards it entirely. A degradation that is *not* a delivery-path claim (native
model inputs the executing agent could not accept) therefore cannot live there: the next
delivery-path reason overwrites it, proven here where `stream_completed_without_chunks` clobbered
the drop record. `stream_fallback_notes` is a bounded APPEND ledger over the **same audited
catalog** — an unknown reason still raises — reaching the assistant message as `stream_degradations`.
The deviation is the second ledger, not a second vocabulary.

**2. `overlay` is in the `models_source` trust set.** The gating predicate for showing a provider's
models is "evidenced", which means `live` **or** `overlay`, not `live` alone. A CLI provider's whole
catalog arrives through the overlay — a persisted discovery run *is* evidence, merely not this run's
— so requiring `live` would hide every model a CLI provider has. Only `static` (compiled-in
candidates) is never evidence. The staleness of overlay evidence is surfaced separately, by the TTL
marker, rather than by demoting it out of the trust set.

**3. Three new attachment-bound config keys.** `resources.native_image_max_bytes`,
`resources.native_document_max_bytes` and `resources.native_attachment_total_max_bytes` default to
the real provider limits Anthropic documents (~5 MB image, ~32 MB PDF, ~32 MB request) and are
shared by BOTH providers' attach paths. Three keys rather than one because the three limits are
independently documented by the provider and independently hit; a single key would force the
tightest of the three onto all of them.

**4. The digit-code probe mints and renders its own assets.** Proving a modality means proving the
attachment's *content* survived the transport, which needs an attachment whose content is
unguessable and machine-checkable. The probe therefore ships a tiny PNG rasteriser and a tiny PDF
writer built from the standard library (no new dependency), and its own tests re-derive each code
from the rendered bytes. The deviation is shipping renderers in a discovery module; the alternative
— a fixed asset pair — is precisely the constant-asserted-against-itself defect this replaced.

**5. Session forks are included in provenance scope and marked, overriding the exclusion test.** The
task registry cannot see a user FORK and the session store cannot say which task owns a delegated
child, so reading either substrate alone is wrong. One walk (`descendant_sessions`) now covers both
and types each row `agent_task` or `session_fork`. The consequence is deliberate and overrides a
pre-existing test that asserted forks were *excluded* from the scope: a permission raised inside a
fork is now listable from its root instead of invisible to every poll. A span from a session in
NEITHER substrate gets its own node and a typed `unattributed_session` degradation — never folded
into the root, which is the fabrication this replaced.

---

## (e) Deferred and attributed items

**1. Two load-flaky streaming tests — attributed to the streaming-lifecycle lane, not to this wave.**
`tests/test_gact/test_streaming.py::test_pre_stream_failure_surfaces_error_without_sync_rerun` and
`::test_streamed_field_buffer_cleared_at_turn_end_and_turn_scoped` fail non-deterministically under
concurrent load and pass solo. Evidence, four runs of the same four-module set on the same box:

| Run | Tree | Result |
| --- | --- | --- |
| 1 | AF-FINAL working tree | FAIL `test_streamed_field_buffer_cleared_at_turn_end_and_turn_scoped` |
| 2 | `b1be65b8`, **zero diff** (sweep stashed) | FAIL `test_pre_stream_failure_surfaces_error_without_sync_rerun` |
| 3 | `b1be65b8`, **zero diff** | 105 passed |
| 4 | AF-FINAL working tree | FAIL `test_pre_stream_failure_surfaces_error_without_sync_rerun` |

Both fail at base with zero diff and both pass in other runs of the identical command, so neither is
attributable to the fix train. The mechanism is the portal one: the suite's own fixture note records
that a `TestClient` constructed without being entered gives each request a *transient* portal, and a
turn that correctly outlives `POST /messages` races that portal's teardown. The fixture now holds one
app-lifetime portal; the residual flakiness is the same lifetime race reaching the two tests that
still build their own clients. Fixing it belongs to the streaming-lifecycle lane's owner. **They are
adjudicated on CI's uncontended runners, not on this loaded box.**

**2. The restart reshape — typed record.** `restore_pending_ask_user_questions` rehydrates a
surfaced native question from its owning session's durable snapshot, which is the crash-recovery
seam that preserves the same question *identity* instead of manufacturing a forwarded copy. When the
`question_record` snapshot is present the restore is faithful. When it is **absent** — a row written
before the snapshot field existed — the question is reconstructed from the flat `pending_ask_user`
blob, and that reconstruction is **lossy in a way callers can observe**: `turn_id` and `attempt_id`
fall back to their model defaults (`""`), and `resume_on_answer` is stamped `True` unconditionally.
So a legacy question that survives a restart resumes as a turn with **empty source-turn
attribution**, and resumes even if its original turn had not asked to be resumed. This is recorded
rather than silently patched because the alternative — refusing to restore a legacy row — loses the
interaction entirely, which is the worse failure. New rows carry the snapshot and are unaffected;
the reshape retires when the last pre-snapshot row ages out.

**3. Marketplace [#61](https://github.com/iowarp/clio-agent-marketplace/issues/61) and
[#62](https://github.com/iowarp/clio-agent-marketplace/issues/62).** #61: the *parent* factorio pack
carries the same three defect classes the factorio-flat fix round closed in the child — they were
fixed where they were found, and the parent is not in this wave's scope. #62:
`check_model_pins.py` misses a bare `model:` pin, so a hard pin evades CI repo-wide — a guard
defect, not a pack defect, and it is repo-wide rather than wave-scoped.

**4. gact-tui [#324](https://github.com/iowarp/gact-tui/issues/324) — folded, not dual-pathed.**
The client-side MCP surface work this wave touches belongs to the P3.1 MCP Apps renderer slice,
which already owns the sandbox proxy, the postMessage dialect, the private-data slots and the
teardown primitive. Attempting a second client mount here would create exactly the dual path #324's
own deletion list exists to remove.

---

## (f) The other two halves

**gact-tui — `codex/campaign-ui-integration` @ `da01c63c`, review branch `review/campaign-ui` @
`04f654ef`, PR [#387](https://github.com/iowarp/gact-tui/pull/387) (open).** The client for this
wave's backend: composer references (draft references held beside their text across remounts,
fingerprinted into the send identity, keyboard- and caret-honest popovers), the pending-interaction
surface, and child-attention UX. The review round was the client form of the same honesty classes —
a foreign owner attributed by the viewed session rather than by invented roles, a timeline row named
for what ran rather than its task token, a relay degradation stated in the relay's own words, an
unrecognized permission action shown rather than hidden, a malformed interaction row contained
instead of failing the whole array, and conversion problems still shown once derivatives already
exist. Interaction kinds are now asserted by visible content, not `data-*` attributes. PR #387 is
gated separately; this landing does not merge it.

**clio-agent-marketplace — `codex/factorio-flat` @ `34cec6c6`, PR
[#59](https://github.com/iowarp/clio-agent-marketplace/pull/59), MERGED to `main` @ `22a0cea8`.**
The Factorio Flat research pack — a flat single-agent pack used as this wave's behavioral corpus.
Merged after a fix round that rebuilt its grader on the same evidence-not-fabrication principle the
provider lanes used: outcomes are graded on real runtime signals rather than tool names, a trace
that contradicts itself is rejected, a completed child that returned nothing cannot ground an
answer, and a direct answer must be shown to have asked nothing *in any status*. The pack opts into
adaptive delegation explicitly (`34cec6c6`) rather than inheriting it. Its two spillover issues are
in (e).

---

## AF-FINAL sweep

Owner standing order: no hardcoded operational tunables. Swept
`git diff origin/develop...HEAD -- src/` — the wave plus all four fix rounds — for numeric literals
in operational positions (timeouts, intervals, retries, caps, sizes, retention, thresholds).
**209 hits across 37 files.** Also swept for raw environment reads: **zero** `os.environ` /
`os.getenv` / `environ.get` reads are added anywhere in the delta; all 17 new knobs go through
`conf.resolve`.

### (a) Already conf-resolved — verified

The wave declared **17 new keys** and they resolve correctly through file → env → committed-default:
`gact.ask_user.ttl_s` / `.max_ttl_s`; `gact.context_references.summary_messages` /
`.summary_excerpt_chars` / `.max_hashable_bytes` / `.browse_limit_per_kind` / `.search_limit` /
`.snapshot_children` / `.snapshot_string_chars`; `gact.interactions.projection_limit`;
`gact.ledger_retention.user_questions.max` / `.hard`; `providers.model_catalog_ttl_s`;
`providers.native_image_url_allowlist`; `resources.native_image_max_bytes` /
`native_document_max_bytes` / `native_attachment_total_max_bytes`.

### (b) Protocol / schema / format invariants — justified, not migrated

| Site | Literals | Why it is not a knob |
| --- | --- | --- |
| `routes/interactions.py`, `context_references.py`, `error_middleware.py`, `composer_runtime.py`, `routes/a2ui.py`, … | HTTP status codes (`400/403/404/409/413/422/500`) | Wire protocol. `composer_runtime`'s `400 <= status < 500` is the client/server split, not a threshold. |
| `model_discovery/probe_assets.py` | glyph geometry, PNG chunk/IDAT structure, PDF object table, `%PDF-1.4` | File-format structure. Changing them produces an invalid asset, not a tuned one. `CODE_DIGITS = 4` is the probe's design (see deviation 4). |
| `providers/native_attachment_bounds.py` | `(len(encoded) // 4) * 3 - padding` | Base64 arithmetic. |
| `runtime/clio_core_health.py` | `int(blocks) * 512` | POSIX `st_blocks` is defined in 512-byte units. |
| `providers/claude_code_litellm.py` | `"max_turns": 1` | SDK protocol argument for a single-turn call. |
| `gact/agent_invocation.py` | `_callable_positional_slots(runner, 7)` | Callable arity probe. |
| `gact/session_descendants.py` | `MAX_SPAWN_DEPTH = 8` | Pre-existing on develop (moved here from `turn_spawn.py`, which now re-exports it — one definition, not two). A computed runaway backstop, per `.claude/CLAUDE.md`. |
| `gact/context_reference_evidence.py` | `depth >= 6`; `depth > 5` and `[:100]` in `_walk_source_values` | Runaway backstops so a cyclic/pathological payload terminates. The two bounds an operator would tune — width and string length — **are** the conf-resolved ones beside them. **In-file justification added by this slice.** |
| `gact/context_reference_file_io.py` | `_FILE_CHUNK_BYTES = 1024 * 1024` | Streaming granularity that bounds nothing a caller observes: the hash is over the whole file at any chunk size, and the prefix a caller receives is bounded by the conf-resolved `_CTX_MAX_BYTES`. **In-file justification added by this slice.** |

### (c) Escaped → migrated by this slice

| Was | Now | Env | Default |
| --- | --- | --- | --- |
| `stream_fallbacks._MAX_STREAM_FALLBACK_NOTES = 32` | `gact.ledger_retention.stream_fallback_notes.max` | `CLIO_LEDGER_STREAM_FALLBACK_NOTES_MAX` | 32 |
| `native_delivery_outcome._MAX_PENDING_NOTES = 256` | `gact.ledger_retention.native_delivery_notes.max` | `CLIO_LEDGER_NATIVE_DELIVERY_NOTES_MAX` | 256 |
| `resource_processing._MAX_PROCESSING_EVENTS = 100` | `resources.processing_event_max_records` | `CLIO_RESOURCE_PROCESSING_EVENT_MAX_RECORDS` | 100 |
| `resource_processing._MAX_PROCESSING_EVENT_MESSAGE_CHARS = 1000` | `resources.processing_event_message_chars` | `CLIO_RESOURCE_PROCESSING_EVENT_MESSAGE_CHARS` | 1000 |
| `resource_processing._MAX_PROCESSING_EVENT_STAGE_CHARS = 80` | `resources.processing_event_stage_chars` | `CLIO_RESOURCE_PROCESSING_EVENT_STAGE_CHARS` | 80 |
| `resource_tools`: `asyncio.sleep(min(0.5, remaining))` | `resources.processing_poll_interval_s` | `CLIO_RESOURCE_PROCESSING_POLL_INTERVAL_S` | 0.5 |
| `model_discovery/claude_code.CLAUDE_CODE_PROBE_TIMEOUT_S = 30.0` (raised from 15.0 by this wave) | `providers.claude_code.probe_timeout_s` | `CLIO_CLAUDE_CODE_PROBE_TIMEOUT_S` | 30.0 |

The two note ledgers sit in `gact.ledger_retention.*` with every other in-process ledger bound, but
resolve their own values rather than riding `LEDGER_BOUNDS`: one is a per-session dict of lists and
the other is keyed by `(resource_id, revision)`, so neither is the flat list `LedgerBound` evicts.
The four `resources.*` bounds live in a new owner module `gact/resource_processing_bounds.py` —
`resource_processing.py` was at 787 of its 800-line cap, and the bounds have two consumers anyway.
Every migrated knob is pinned by a test that sets it through the config **file** layer and asserts
the **live** object honours it, plus a floor assertion so a nonsense value degrades to something
usable rather than to a zero-capacity buffer that silently drops what it is handed.

`CLAUDE_CODE_PROBE_TIMEOUT_S` resolves at import (like `gact/runtime/constants.py`'s
`_CTX_MAX_BYTES`) because it is also this module's public default argument.

---

## Verification at the landing head (tip of `review/campaign-backend`)

Run on Windows / Python 3.12. **The full suite is not run locally for this slice** — by standing
guidance it runs on PR #1298's CI, whose uncontended runners are also where the two attributed
streaming flakes are adjudicated. Locally: every guard, plus targeted tests for everything this
slice touched.

| Gate | Result |
| --- | --- |
| `ruff check src/ tests/` | All checks passed (exit 0) |
| `ruff check` over CI's exact scope (adds the three `scripts/*.py` CI lints) | All checks passed (exit 0) |
| `ruff format --check` (files touched by this slice) | 12 files, all formatted (exit 0) |
| `scripts/check_file_size.py` | OK — no file over its ratchet baseline (cap 800 for new files) |
| `scripts/check_no_class_in_function.py` | OK — nothing over its baseline |
| `scripts/check_no_summaries.py` / `check_no_settle_vocabulary.py` / `check_tool_instrumentation.py` | OK (baseline 0 each) |
| `scripts/check_silent_fallbacks.py` | OK — total 1, at baseline |
| `scripts/gen_env_reference.py --check` | OK — env reference matches the source tree |
| `bash -n scripts/homelab-env.sh` | exit 0 |
| `mypy src/` | Success, no issues found in 530 source files |
| route-count guardrail (`test_decomposition_guardrails.py`) | 4 passed |
| targeted: `test_native_delivery_outcome` / `test_retention` / `test_model_discovery` / `test_docs` | 120 passed, 2 skipped |
| targeted: `test_streaming` / `test_resource_hardening` / `test_resources` / `test_import_seams` | 107 passed, 1 attributed flake (see (e)) |
| CLI baseline smoke (`ui/cli.py --help`) | exit 0 |

`ruff check --select C901,PLR0915 src/` reports 266 findings, unchanged in character by this wave;
that CI step is `continue-on-error: true` (advisory complexity warning), not a gate.

**The three raised file-size baselines from the fold each carry an in-file justification** naming
what landed and where the logic actually lives — `agent.py` 1020 → 1024 (the `canonical_workspace_root`
import plus three call sites; the canonicalizer itself is in `tools/workspace_root.py`),
`routes/mcp.py` 958 → 968 (the `to_thread` hop, the `degradations` key, and the restored SPEC 6.7
anchor), `routes/providers.py` 1343 → 1351 (the provider-catalog invalidation assignment plus its
rationale; the catalog is built in `gact/provider_catalog.py`). Every other baseline in the delta
moves DOWN. Rationale was re-balanced by extraction and by moving a baseline, **never** by
re-deleting rationale to fit a ratchet.

**On the carried-forward `preflight-arc.py` note.** The ladder was run as `ruff check src/ tests/`
on the standing assumption that `scripts/provenance_qualification/preflight-arc.py` carried a
pre-existing red. Re-verified at this head: it is **clean** under both `ruff check` and
`ruff format --check`, and it is outside CI's ruff scope in any case (CI lints `src/ tests/` plus
three named `scripts/*.py`). There is no out-of-scope red to carry; the note is retired.

CI on [#1298](https://github.com/iowarp/clio-agent/pull/1298) is the merge gate.
