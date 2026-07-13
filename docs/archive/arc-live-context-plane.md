# ARC as the Live Context Plane

Design context for making ARC the authoritative, mutable context the DSPy ReAct
loop reads from on every iteration — the substrate for a distributed team of
agents. This is a context document, not a phased schedule. It records what we're
building, why, and the exact points where the design meets the existing code.

---

## 1. The why (north-star)

clio-agent is heading toward a **distributed team of agents at scale**: experts
on different nodes, using local GPUs/resources, **discovering each other and
talking peer-to-peer** (not the hardcoded Tier1→2→3 tree), working
**asynchronously like a team** — one agent produces a work-product and leaves;
hours/days later a *different* agent discovers it and picks it up.

IOWarp / clio-core is the right substrate for this (distributed-by-construction,
multi-node, GPU-aware, now `pip install iowarp-core` cross-platform). ARC becomes
the logical live-context plane on top of it. This document is about the **logical
plane**; the physical KV plane (clio-core `context-transfer-engine/llm-hooks/
kvcache`) is a later, separable backend.

## 2. The core problem ARC solves: it must be the LIVE plane

The agentic loop sends the **full accumulated context** on every step (LLM
inference is stateless): `send A → B`, `send ABC → D`, `send ABCDEF → G`. The KV
cache is only an optimization on that resend.

Today ARC is **context-after-the-fact**: it records the turn at the end; the live
in-turn working context (B,C,D,E,F,G) lives only in dspy's transient `trajectory`
dict and never passes through ARC during the loop. Consequence: compressing ARC
has **zero** effect on the LLM turn — the thing that *should* be true isn't.

Making ARC the live plane = dspy reads the context it sends **from ARC** each
iteration. Then ARC operations (compress/summarize/delete/inject) directly shape
what the model attends over. This is feasible because dspy holds **no hidden
state** — see §6.

## 3. ARC vs Trace (keep these separate)

- **Trace** — immutable, append-only, **full-fidelity** history. Every A..H as it
  happened; never edited or compressed. Jobs: audit, replay, "how was H produced."
  Already exists: `gact/semantic_events.py` `FileSemanticTraceBackend` writes full
  events as JSONL under `<session_store>/semantic_trace/<session_id>.semantic.jsonl`.
- **ARC** — mutable, ordered, **live context plane**: the segment store the LLM
  reads, subject to the ops in §4, scoped per expert/agent (§5).

Relationship: **Trace is source-of-truth; ARC is a derived, mutable working view.**
Every ARC op is logged into the Trace (its evolution is replayable); ARC is
reconstructable from the Trace. This is what makes compressing ARC *safe* — the
Trace never loses anything. Async pickup: read compacted ARC by default, drill
into the Trace for full detail.

## 4. The live-context operations

Surface ops (callable at any scope, §5):

- **append(content)** — add a segment at the end. *Cheap: no prefix break* —
  appended tokens attend over an unchanged, already-cached prefix.
- **injection(position, content)** — insert mid-sequence. Breaks the prefix from
  the insert point.
- **deletion(range)** — remove segment(s).
- **summarize(range)** — replace a range in place with an LLM summary of it.
- **context-compaction** = `summarize(range = whole working context)` → collapse to
  a summary. Trigger: manual on task-change, or auto at ~90% of the context window
  (standard Claude-Code/Codex auto-compact). Per-expert or global (pipeline).

These reduce to **two structural primitives + an LLM transform**:
`insert(position, content)` and `delete(range)` over an ordered segment sequence;
`summarize = delete(range) + insert(LLM_summary(range))`; `append = insert(end)`;
`context-compaction = summarize(all)`.

This insert/delete-over-an-ordered-keyspace **is** why an ordered index (B-tree:
locate/range) and a write-optimized path (LSM: absorb churn, tombstones,
store-compaction) are load-bearing here — not aspirational.

**Naming trap:** *context-compaction* (LLM-summarize the context) ≠
*store-compaction* (LSM merge of sorted runs). Always disambiguate.

## 5. Scope = address

Ops **and** queries take a scope: `expert` | `agent` (= all its experts) |
`cross-agent`. Hierarchy: **expert = one ReAct loop** ⊂ **agent = team of experts**
⊂ **system = many agents across nodes**.

Maps 1:1 onto CTE tag namespaces:

| Scope        | Tag address       | CTE query (`tag_re`) |
|--------------|-------------------|----------------------|
| One expert   | `agentX/expertY`  | `agentX/expertY`     |
| Whole agent  | `agentX/*`        | `agentX/.*`          |
| Cross-agent  | `*`               | `.*`                 |

- **Agent-level append is the communication primitive**: publish to the shared team
  context; other experts read the agent scope (a blackboard).
- **Cross-agent query** is the async distributed pickup (temporal/semantic over a
  namespace).
- Concurrent writers to a shared scope need ordering/visibility → reads are
  **(scope, as-of-T)** via logical-time + a change-feed (CTE
  `poll_telemetry_log(min_logical_time)`).

Full addressing: **op/query × scope × logical-time** over a tag-namespaced,
ordered, mutable store.

## 6. Exact code splice points (grounded)

**The trajectory → prompt path (the crux).** From the installed dspy source
`dspy/predict/react.py`:
- `trajectory` is a plain dict built in `forward()`, keys `thought_{i}` /
  `tool_name_{i}` / `tool_args_{i}` / `observation_{i}` (react.py:106–111). **These
  keys are the segments.**
- Each LLM call: `module(**input_args, trajectory=self._format_trajectory(trajectory))`
  (react.py:151, inside `_call_with_potential_trajectory_truncation`). dspy
  **re-renders the full trajectory every call** (stateless resend).
- `_format_trajectory(dict)` → string via `adapter.format_user_message_content`,
  passed as the `trajectory` **InputField** value (react.py:76, 91–94).
- **There is no hidden dspy state.** The only "buffer" competing with ARC is this
  local dict — which we own.

**Our override already exists.** `gact/app.py:5407` `_RetainingReAct` overrides
`forward()`, owns the `trajectory` dict, already truncates it, and publishes it to
`_ACTIVE_REACT_TRAJECTORY` before extract (app.py:5440–5441). `react_agent` is
built/called at app.py:5945–5977 inside `with dspy.context(lm=create_lm(...),
adapter=create_chat_adapter(...))`.

**The live-plane move:** make ARC the backing store of that dict for the loop's
scope — write each `thought/tool/observation` segment into ARC as produced, and
make the rendered context come from ARC (override `_format_trajectory` to render
ARC@scope, or make the local dict a view over ARC). The four ops mutate ARC's
segments between renders. Because dspy already re-renders every call, no extra
resend machinery is needed.

**The ARC live seam already half-exists.** `build_app(..., arc=ARCMemory())`
registers `arc.on_semantic_event` as a live consumer (app.py:12330–12339);
`SemanticEventSink.emit()` calls live consumers after durable-trace writes;
`LiveRuntimeContext.fold()` (arc/live.py:99–150) folds events into per-session
`_LiveTurn` state. Gap: this fold is **turn-grained and not read back into the
loop's prompt**. We need **segment-grained** state that the loop *reads from*.

**ARC store surface today** (`arc/memory.py`, `arc/storage.py`): smallest
persistent unit = one `Conversation` (session_id) or `Invocation` (trace_id);
smallest working unit = `_LiveTurn` (in-memory, not persisted). No ordered
"segment" notion yet — that's the new structure. `ARCStore` protocol
(put/get/exists/scan/delete/clear) is the swap seam toward CTE (see
`clio-core-integration` design).

**LM boundary.** No interception today, but dspy `BaseLM.acall` is wrapped with
`@with_callbacks`, and `cache=False` (config.py:1022) so every call is live. A
`dspy.BaseCallback` (`on_lm_start`) is the clean, non-invasive hook for the
recording harness in §8.

**gact does NOT use ARC for context assembly today** — it uses `MessageStore`
(per-session JSON ledgers) + in-memory state; `ContextCompiler` exists but is
unused in the gact path. Making ARC the live plane means the loop's context comes
from ARC, and `ContextCompiler` becomes scope-aware (expert-private segments + a
view of subscribed agent-scope segments, assembled within budget).

## 7. The backend seam (the handoff contract to the KV work)

Implement the ops **now** as a working v1 behind a stable interface — not stubs.

- **Now-backend (correct, slow):** logical ops on ARC's ordered segment store +
  the **naive KV path**: after an edit, the next iteration re-renders and re-sends
  the (possibly shorter/edited) context; the inference engine recomputes from the
  edit point. Breaks prefix caching on mid-edits; **append stays free**;
  context-compaction is rare so its recompute is amortized.
- **Later-backend (KV-optimized, a student's work):** surgical KV splice via
  clio-core `context-transfer-engine/llm-hooks/kvcache`, **behind the same
  interface** — a drop-in backend swap, not a loop change.

The deliverable that makes the handoff clean is the **interface boundary**:
`apply(op, scope, ...)` with semantics fixed by the ops in §4. The default impl
recomputes; the student's impl splices. Same signatures, same semantics.

## 8. Acceptance-test contract (write these FIRST)

Claim to prove: *the message list sent to the LM on iteration k is a pure function
of ARC@k, with zero contribution from a dspy-internal buffer.* Sole observation
point: the messages handed to `dspy.LM` (everything downstream — the KV cache — is
faithful to what's sent).

1. **Make it true by construction.** Remove the dual copy: `_RetainingReAct` reads
   ARC to build the prompt input; the local `trajectory` dict stops being an
   independent source. One store ⇒ nothing to diverge. The tests below are then
   regression guards.
2. **Recording-LM harness.** A `dspy.BaseCallback` (`on_lm_start`) — or a recording
   `dspy.LM` returning scripted replies — that captures the exact outgoing
   `messages`. (No such harness exists yet; build it.)
3. **Mutation propagation (decisive).** Out-of-band mutate ARC mid-loop, assert the
   *next* captured prompt reflects it:
   - `append(X)` → prompt contains X.
   - **`delete(C)` → prompt does NOT contain C** (a shadow buffer would still have
     it — the killer test).
   - **`summarize(E→E')` → prompt contains E', not E.**
   - `insert(mid, Y)` → Y present at the right position.
4. **Byte-equality.** Independently render ARC@scope and assert it byte-equals the
   captured trajectory content (modulo static signature/instructions). Seed each
   segment with a UUID marker; assert a bijection marker↔segment.
5. **Prefix cross-check (ties to KV).** Append-only step: prompt@k is a literal
   prefix of prompt@k+1. Edit op: the prefix breaks at the expected segment
   boundary and nowhere earlier. This doubles as the spec the KV-splice validates
   against.
6. **Runtime invariant (dev).** An `on_lm_start` callback that recomputes the
   expected prompt from ARC and asserts equality on every real call (debug-flag
   gated) — catches drift in long/distributed runs.
7. **Trace audit (async/distributed).** Replay the Trace, reconstruct ARC@each LM
   call, assert == captured prompt. Correctness as a property over recorded runs.

Existing tests to build alongside: `tests/test_gact/test_trajectory_retention.py`,
`tests/test_arc/test_live.py`, `tests/test_arc/test_context_compiler.py`.

## 8b. The auto-compaction trigger (resolved)

**dspy's own truncation is a backstop, not our feature.** `react.py:146`
`_call_with_potential_trajectory_truncation` + `react.py:170` `truncate_trajectory`
are **reactive** (only fire after the provider raises `ContextWindowExceededError`),
**lossy** (pop the oldest tool call's 4 keys — delete, no summary), and **blunt**
(3 retries then raise). The docstring invites override — that's our hook. Our
auto-compaction is proactive + lossless (LLM-summarize) + configurable + scope-aware
and runs *first*, leaving dspy's truncation as a never-fired safety net.

**Measuring "are we at threshold."**
- Max: `config.context_window` (config.py:258/348), per-model from `model_limits.json`
  `context` (also `effective_context_window` / `chosen_context`).
- Current usage, two sources: `litellm.token_counter(model, messages)` (pre-send
  estimate; approximate for local LM-Studio/Argonne tokenizers) and
  `response.usage.prompt_tokens` (post-call **exact**, already wired via
  `dspy.settings.usage_tracker`, cache disabled at config.py:1017–1019).
- **Drive the trigger off exact `prompt_tokens`** from the last call:
  `ratio = prompt_tokens / context_window`; compact before the next send when
  `ratio >= threshold`. Use `token_counter` only as a pre-send guard for single-shot
  overflow (one huge observation).
- **Threshold is configurable** (mirror Claude-Code `*_AUTOCOMPACT_PCT_OVERRIDE`).
  Claude-Code default ~83–95%, but their team recommends compacting *earlier*
  (50–60%) for better summaries (full uncompressed info available); reserve buffer
  for the summarization call itself. Default configurable, likely < 90%.
- **The check is per-expert** (each ReAct loop vs *its* model window) — co-located
  with `_call_with_potential_trajectory_truncation`. Agent/global compaction
  (task-change, aggregate) is a separate higher-level trigger.

**Tokenizer accuracy via the model DB (enrichment + self-calibration).**
litellm's `token_counter` falls back to tiktoken for clio's local fleet
(gemma/qwen/nemotron), making per-segment counts approximate. Fix: record the
correct **tokenizer per model in the model DB** (`model_limits.json` + handshake
profile), next to `context`. The seam already exists — `token_counter(model,
custom_tokenizer=...)` accepts a `{type, tokenizer}` from
`create_pretrained_tokenizer(hf_repo)` / `create_tokenizer(json)` (utils.py:1784).
Accuracy ladder: (1) server `/tokenize` endpoint when available (exact, no local
deps, one round-trip); (2) DB-specified HF tokenizer via `custom_tokenizer` (exact,
one-time download); (3) tiktoken fallback (approximate — flag entry for
enrichment). SELF-CALIBRATION: reconcile local per-segment estimate vs the provider's
exact `prompt_tokens` after each call → learn a per-model correction factor or flag a
wrong tokenizer mapping. The DB starts approximate and converges to exact from
traffic (fits clio's self-improving identity). Window check still uses the exact
provider total regardless.

## 9. Explicitly deferred (future-work, hooks present)

- Physical KV surgery (delete/insert/summarize without recompute) — behind the §7
  interface; clio-core `llm-hooks/kvcache` backend; a student's KVCache work.
- Vector/embedding semantic search — neither ARC nor CTE has it (CTE BM25 is
  lexical, recomputed per query, no inverted index). Likely a separate MCP.
- CTE-backed `ARCStore` (cross-node shared plane) — see `clio-core-integration`;
  the local-store v1 proves the logical plane first.

---

### Open questions to resolve while building

- Segment model: exact schema of an ARC segment (id, scope, logical-time, kind,
  content, derived-from, **token_count**) and how it maps to dspy's `*_{idx}`
  trajectory keys. `kind ∈ {system, tool_def, thought, tool_call, observation,
  summary}` enables token attribution by category and scope (group-by over
  segments). Provider `usage` gives only ONE total (full prompt each call, not a
  delta; window-fullness = that total), with no semantic breakdown — so
  categorization is client-side per-segment counting, reconciled against the exact
  provider total. Caveats: tokenizer mismatch (tiktoken vs local models), BPE
  non-additivity at segment boundaries, un-attributable chat-template framing — so
  the breakdown is an attribution ESTIMATE, not exact. It drives compaction policy
  (summarize the heaviest low-value segments first; `tool_def` is a fixed large
  per-call cost). Providers may also split the total into cached vs fresh
  (`prompt_tokens_details.cached_tokens` / Anthropic `cache_read_input_tokens`) —
  useful later to validate KV/prefix reuse, not for the window check.
- Where the ops are invoked from: the manual task-change trigger surface (the
  auto-compaction trigger is resolved below).
- Scope-aware `ContextCompiler`: how expert-private + subscribed agent-scope
  segments are merged within budget.
- as-of-T read semantics for concurrent shared-scope writers (logical clock source).
