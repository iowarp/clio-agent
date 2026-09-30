# Campaign: agent-loop rebuild — ClioReAct on DSPy 3.4, clio-core as the context system

**Status:** APPROVED 2026-09-28 (owner). Phase 1 implemented on `feat/codex-sdk-stateful`
(unit-tested; full-suite + live verification pending). Keep this doc updated as work lands.
**Goal file:** this document is also the grounding for a `/goal` run (see the last section).

## Why

clio's agent loop is slow and its context is assembled rather than owned. Measured on a real
multi-turn data-analysis session (one question: 39 min, 79 serial steps, 9.87M input tokens for
93k output), with every cause verified in code:

1. **Full resend every step.** The Codex SDK transport opened an ephemeral thread per LM call and
   re-sent the whole rendered history (v0.7.0 had a Codex stateful delta: 2.95s vs 7.37s TTFT,
   76.7% cached input; the Aug-2026 SDK rework removed it).
2. **Serial tools.** One `asyncio.Lock` per MCP executor is held for the whole call and the
   executor is shared per workspace (parent + children serialize); DSPy's loop runs a step's
   calls one by one; cancel is never checked between iterations.
3. **Context rebuilt per turn, not projected.** Every turn recomposes the system prompt, re-inlines
   attached files, renders earlier turns as prose inside the new user message, wipes the working
   set, and (mid-turn) glues steers and child results onto tool observations — no cross-turn
   prefix reuse, and no record of what the agent actually saw.
4. **clio already runs its own loop, pretending not to.** `instrumented_forward` is a modified copy
   of `ReActV2.forward`, protected by hash pins on upstream code clio never runs.

The campaign rebuilds the loop so clio owns it (approach B), clio-core is the single context
system, providers are stateful where they can be, and tools run concurrently — measured on clio's
own live-verification legs and marketplace agents, with the gact-tui UI verified.

## Owner-settled principles (binding)

- **Fail over fallback.** A typed error is preferred over any fallback. The ONLY sanctioned one:
  the platform cannot run clio-core (not installable/present) → run on DSPy `History`, LOUD
  (typed degraded mode, UI + doctor). clio-core erroring or lost mid-turn → typed turn failure.
  Never a silent or mid-turn switch.
- **clio-core keeps everything** as recorded events with role + actor: turns, summaries,
  injections, edit ops, fixes, hook effects. Two projections over one log: the UI projection
  (everything visible) and the agent-context projection (what the model sees); they may differ.
- **Append-only by default**, maximize prefix reuse. Edits are first-class recorded ops (today
  only compaction; soon algorithm- and human-driven add/remove). Injections (plan reminder, todos,
  replan, task results, memory hits, files) are recorded once at the prefix edge and stay until an
  op removes them; the UI shows them.
- **Deterministic fixes** (arg repair, path grounding, circuit breaker, observation composition):
  each a config switch; each firing recorded, UI-visible, and told to the model next to the result.
- **Provider sessions span turns;** reset only on a recorded op or provider error (typed).
- **Step anatomy:** thinking (provider payload, byte-exact) · text (what `next_thought` really is;
  optional with tool calls, required on the final step = the answer) · tool calls → results.
  **Decided 2026-09-29 (owner):** `next_thought` is optional for EVERY model when the step calls
  tools — clio forces no shape on the model; anything the model outputs (thinking, text) is
  displayed.
- **`dspy extract`** = literally DSPy's extract, config-driven (steps > N, default 3, can
  disable), recorded as an explicit amendment to `react-loop-completion-2026-09.md`.
- **SDK providers are providers only** (Codex SDK, Claude Code SDK): no dynamicTools, no SDK tool
  loop; identical code above the transport as for vLLM. SDK transports keep the text protocol
  (one prompt string, no tools); native tool calls only on direct APIs.
- **Keep DSPy** for signatures, adapters, the LM layer, BestOfN/Refine, optimizers and module
  composition. Public DSPy API only; no hash pins on DSPy internals.

## Design

- **D1 — one log, projections.** Event vocabulary on the existing `_events` family (no fifth
  store): user message · injection (actor = algorithm) · steer · model step (complete provider
  response incl. thinking payload) · tool result (raw + model-facing) · child result · compaction
  checkpoint · edit op (actor = human | algorithm) · fix fired · hook effect. The agent-context
  projection is one function per agent scope, spanning turns; the UI projection is the same log
  with everything visible.
- **D2 — `ClioReAct(dspy.Module)`**, stateless. Per step: boundary (cancel? apply new events:
  steers, child results, injections, ops) → context = project(clio-core) → `dspy.Predict` → record
  the complete step → run tool calls concurrently → record results → end (no tool call = answer ·
  submit · ask_user/plan_exit yield · optional extract). Hooks (BeforeModel/AfterModel/PreToolUse/
  PostToolUse/Stop) apply at fixed step points and their effects are recorded. Differential test
  against stock `dspy.ReActV2`.
- **D3 — transport contract.** Prefix-stable rendering (render(log[0..n]) is a prefix of
  render(log[0..n+1]) unless an op landed). Stateful where the provider is (Codex thread, Claude
  session, Responses `previous_response_id`), prefix-cached where stateless. Thinking stored
  byte-exact, sent back in provider format. Text vs native tool mode chosen from the transport's
  declared capability — typed failure on mismatch.

## Verified DSPy 3.4.0 facts (spike against installed code)

- Stock `ReActV2`: history keeps only inputs/`next_thought`/`tool_calls`; tools sequential; no
  tool call → forced `submit` (clio's contract forbids it) → do not use its loop.
- Text mode: after call 0, calls are append-only beneath one byte-identical trailing message
  (tools + "Respond with…"); `stateful_common.classify_delta` handles it.
- `use_native_function_calling=True` silently drops to text when the LM lacks
  `supports_function_calling` (`dspy/adapters/base.py`).
- Native reasoning via `dspy.Reasoning` forces `reasoning_effort="low"` and drops the step's
  visible text; with native tools the signature has zero outputs and the reminder names an empty
  field.
- Custom LMs via `forward/aforward` and `messages=` dicts are deprecated (removed in 3.5) →
  migrate clio's custom transports to the Engine API.

## Phases (stacked branches — each cut from the previous phase's branch; worktree; detailed sub-plan at start)

0. **Environment + baseline.** `uv sync --extra dev`; clio-core daemon working (the full suite
   needs it); Codex SDK signed in; gact-tui web built. Measure the baseline on unmodified
   `develop` for every live case below.
1. **Codex SDK stateful transport** (`feat/codex-sdk-stateful`, implemented): full suite green,
   live-verify against the baseline.
2. **`ClioReAct` + concurrent tools + cancellation at every boundary.** Delete: the ReActV2
   subclass/`instrumented_forward` copy, `reactv2_upstream.py` pins, adapter class-name spoof,
   async-tool ban, dead `trajectory`/`tools_called` reads, workflow-era leftovers
   (`workflow_state` auto-inject, prose-parsed prior state, routing fields, planner LM, EarthScope
   trace), the DSPy-history read fallback, the streamed→sync second run. Drop the per-executor MCP
   call lock; observer/gate state per call.
3. **clio-core as the context system.** Event vocabulary; cross-turn agent-context projection
   (earlier turns as real messages, no prose blob in
   `session_store._compile_session_conversation_history`, no per-turn wipe); injections, steers
   and child results as events in their own role at the step boundary; compaction over what
   clio-core holds (failing-first test for the suspected mid-turn loss); thinking payloads stored;
   UI projection of injections/edits/fixes; the loud platform fallback, typed failure otherwise.
4. **Fixes + hooks as recorded, configurable events** (`conf.resolve` switches documented via
   `scripts/gen_env_reference.py`; defaults from the owner).
5. **DSPy 3.4 upgrade** (Engine API transports) + thinking pass-back per provider + native tool
   calls on direct transports + Codex direct stateful chain (owner logs in when reached).
6. **Config-driven `dspy extract`** (+ contract amendment); relay onto MCP tasks per
   `mcp-client-unification-2026-08.md` campaign 2 (owner decides timing).

## Phase 2 sub-plan (`feat/clio-react`, cut from `feat/codex-sdk-stateful`)

Inventory taken 2026-09-28 against `feat/codex-sdk-stateful` (every item below was located in
code; file:line in the phase report).

**Add** `gact/agents/clio_react.py` — `ClioReAct(dspy.Module)`, constructor
`(signature, tools, max_iters)` (the shape `builders.py` and tests use). One `dspy.Predict` over
the react signature (task inputs + `history: dspy.History` + `tools` → `next_thought: str`,
`tool_calls: dspy.ToolCalls`), built with public DSPy API only. Per step:

1. boundary — cancel checked (typed `_TurnCancelled`); proactive compaction trigger;
2. context — the ARC live plane folded BY STEP (one History event = thought + every tool call
   of that step + their results, call ids preserved; a summary segment is its own event), static
   task inputs folded once into the head. No ARC (no app/scope: unit tests, CLI) → the loop's own
   History. An ARC read failure is a typed turn failure — the `reactv2_arc_history_read_failed`
   fallback to DSPy's internal History is deleted;
3. predict → record the step (ARC `step_open`, `react.step.completed`, spans, step thought);
4. tool calls run CONCURRENTLY (one worker per call, each in a copy of the step's context);
   results kept in call order; a terminal MCP refusal is classified directly from the call's
   exception and re-raised after the step (no contextvar mark/pop), so async tools need no ban;
   cancel checked before dispatch and after results;
5. end: no tool call → `direct_response`; `submit` → its outputs; `ask_user`/`plan_exit` →
   `*_yield`; `max_iters` (0 = unlimited) / `parse_error` / `context_window_exceeded`; a
   `ClioError` closes the expert lifecycle `failed` and re-raises. Same events, Prediction fields
   and termination reasons as today (the external contracts listed in the report).

The turn engine runs the module ONCE in the forward executor (copied context, cancel checker);
live text and thinking already stream through the LM's token hooks (`runtime/lm_activity`).

**Delete** (same branch): `reactv2.py`, `reactv2_events.py`, `reactv2_upstream.py` and their
hash pins; `runtime._retaining_react_cls`; the ARC read seam in the adapters
(`HistoryPreparationMixin`'s override — image/PDF hydration stays); the `LenientChatAdapter`
class-name spoof and the streamify path it exists for (`_try_streamed_forward`, dead stream
listeners, the `llm.request.degraded` → sync second run in `turn_forward`, `stream_fallbacks`
peeks for it); the streamed→blocking second LM call in `IOLoggingLM` (a streaming failure is a
typed error, not a re-issue); the async-tool ban and refusal-marking wrapper; dead
`trajectory`/`tools_called`-from-trajectory reads (`builders`, `turn.py`, `todos.py`,
`evidence.py`, `messaging.py`, `_emit_blueprint_llm_failure`); the never-raised
`_BlueprintTerminalWorkflowState` and its handlers; the prose-parsed `clio_prior_workflow_state`
seed; the planner LM (built on every bind, used by nothing); the EarthScope `trace.hot` checks;
the per-executor MCP `_call_lock` (per-namespace first-connect guard instead; elicitation
correlation keeps its typed decline when concurrent calls on one client are ambiguous) and the
one-call-at-a-time assumptions in the tool observer / artifact identity.

**Tests:** ClioReAct differential vs stock `dspy.ReActV2` (scripted LM: identical message
sequence for single-call steps; documented divergences: no forced submit, concurrent calls);
concurrency (two slow tools in one step overlap; results in call order; one failing call doesn't
stop the other); cancellation at each boundary; terminal-refusal escalation; the by-step ARC fold;
the existing loop tests ported to `ClioReAct` (not kept against the deleted classes).

Phase 3 then replaces the step 2 source with the cross-turn clio-core projection and moves
steers/child results off tool observations.

## Re-review of phases 1–2 against DSPy 3.4 (2026-09-29)

**Why.** Phases 1–2 were built on DSPy 3.3.0b1's adapter path (`dspy.Predict` + `ChatAdapter`
text protocol + `LenientChatAdapter` repairs + LiteLLM `CustomLLM` transports + an `IOLoggingLM`
subclass). DSPy 3.4 — the version this campaign moves to — offers a better-fitting public surface.
Facts below are cited against the 3.4.0 wheel (`dspy/…`, `lm15/` = `dspy/_vendor/lm15/…`).

**3.4 facts that decide the design**

- The adapter path still fights the owner principles: with native tools the model's free text
  is dropped unless it parses into a text field (`adapters/base.py:157-175`); the adapter
  silently falls back to text tool calls when the LM does not declare function calling
  (`adapters/base.py:117-119`); tool inputs are detected only as exact `list[Tool]`
  (`:497-505`); `dspy.Reasoning` still forces `reasoning_effort="low"` and deletes the field
  (`adapters/types/reasoning.py:53-77`) and thinking signatures are lost (`lm15/providers/
  openai_chat.py:383,522-531`); the "Respond with…" reminder moves, so the wire is not
  append-only (`base.py:545` vs `chat_adapter.py:162`); hidden extra calls are on by default
  (JSON fallback `chat_adapter.py:48,86-112`, `num_retries=3` `base_lm.py:94`, cache
  `lm.py:209`).
- The direct path fits: `lm(dspy.lm15.Request)` → `dspy.lm15.Response` with typed parts —
  `TextPart`, `ToolCallPart`, `ToolResultPart` (content may be `ImagePart`/`DocumentPart`),
  `ThinkingPart` with provider `ContinuationState` (Anthropic signatures, OpenAI encrypted
  reasoning items) that round-trips (`lm15/types.py:227-252,570-760`,
  `lm15/providers/anthropic.py:480-485,897-902`, `lm15/providers/openai.py:777-782,1135-1148`).
- Custom transports are engines: `complete(Request)->Response`, `stream(Request)->events`,
  `close()`, async twins, passed as `dspy.LM(model, engine=, async_engine=)`
  (`clients/engines/base.py:23-36`, `clients/lm.py:65-114,217-218`); engines raise lm15
  errors and DSPy owns retries (tutorial "custom_lm_engines"). `BaseLM.forward`/`aforward` and
  `messages=` dicts are deprecated, removed in 3.5 (`clients/_deprecation.py:33-71`).
- OpenAI-compatible endpoints (vLLM, ALCF, LM Studio, llama.cpp) are declared providers:
  `dspy.lm15.register_provider(ProviderDefinition.chat(AccessPolicy(...), compat=
  OpenAIChatCompat(...)))` (`lm15.py:108-185`); `dspy.LM` routes resolvable models to the
  native lm15 engine, else LiteLLM (`clients/backend_selection.py:29-77`).
- Streaming: engine events become chunks with `delta.content` / `reasoning_content` /
  `tool_calls` on `settings.send_stream` (`clients/engines/streaming.py:15-62`,
  `clients/execution.py:593-606`); `on_lm_start`/`on_lm_end` callbacks observe every call
  (`utils/callback.py:104-133`).
- lm15 ships `OpenAICodexLM` (Responses on the ChatGPT Codex backend; accepts a CLIO-held
  callable credential + `account_id`, `lm15/providers/openai_codex.py:37-67`,
  `lm15/providers/base.py:72-79`) — but it is stateless: no WebSocket, no
  `previous_response_id` (`lm15/providers/openai.py:1045-1056,1345`). Its model-string route
  reads `~/.codex/auth.json` (`lm15/router.py:919-920`, `lm15/auth.py:107`), so CLIO must pass a
  constructed engine, never the model string. lm15's `claude_code` is HTTP with the CLI's
  credential file — not usable under the owner's credential rule.

**Measured:** `feat/clio-react` with only `dspy==3.4.0` pinned runs the full suite at 10033
passed / 1 failed (the test pinning the 3.3 prerelease); 3.4 keeps the 3.3 surfaces working but
warns that clio uses the deprecated ones — `messages=` dicts (`lm/io_logging.py`,
`lm/hooked_lm.py`) and custom LMs via `BaseLM.forward` (`io_logging`, `hooked_lm`,
`gact/goal.py`, `agents/builders.py`), all removed in 3.5. So 3.4 is not a breaking upgrade; the
reason for 2b is the design fit below, not breakage.

**Verdicts on phases 1–2**

| Piece | Verdict |
|---|---|
| Loop semantics (termination, concurrent calls in call order, cancel at every boundary, refusal escalation, yields, no post-loop call), `StepRecorder`/highway events, ARC recording, one-run turn engine, `forward_error_info` | **KEEP** |
| Every Phase 2 deletion (ReActV2 subclass + pins, streamify path, trajectory cell, planner LM, MCP call lock, workflow leftovers, dead optimizer decorator, duplicate lm.call) | **KEEP** (DSPy-independent) |
| `ClioReAct` step = `Predict` + react signature + `ChatAdapter` text protocol; `fold_steps` → `dspy.History` events; `submit` args via text | **PORT**: one `lm(Request)` per step with native `tools`; context folded into typed `Message`s; text shown as written, thinking stored with its continuation and sent back as-is; `submit` stays a native tool for structured outputs |
| `LenientChatAdapter` repairs + re-samples, `StrictGuidedJSONAdapter`'s JSON fallback, `runtime/lm_stream.AnswerFieldExtractor`, `claude_code_thinking_split` (all exist to parse the `[[ ## field ## ]]` protocol) | **DELETE** |
| `HistoryAttachmentsMixin` promote step | **DELETE**; descriptor→bytes hydration moves to context building as `ImagePart`/`DocumentPart` |
| `IOLoggingLM(dspy.LM)` | **PORT**: lm.call logging → `on_lm_end` callback reading the lm15 `Response`; token liveness + live lanes → a streaming helper over `send_stream`; its transient retry **DELETED** (DSPy owns retries; engines raise lm15 error types); `_process_completion`/truncation overrides do not run on the native path — replaced by typed engine results |
| `HookedLM` (BeforeModel/AfterModel deny/synthesize/patch) | **PORT** to a wrapping engine (callbacks cannot deny or rewrite) |
| Phase 1 Codex SDK transport (`sdk_transport` CustomLLM, `sdk_stream` LiteLLM chunks, `sdk_stateful` + `stateful_common` delta over rendered message dicts incl. the moving-reminder tolerance) | **PORT** to an engine pair: typed `Request` in, structural prefix compare on messages (the moving-tail tolerance is deleted — no adapter reminder exists), lm15 stream events out; thread-per-conversation, runtime/thread lifecycle, cached-token accounting, auto-compaction off, typed resets **KEEP** |
| Claude Code SDK transport (`claude_code_litellm`/`_bridge`/`_blocking`, CustomLLM) | **PORT** to an engine pair; pool/reaper/bounds/cancel/audit **KEEP** |
| Codex direct (`litellm_adapter`, `responses`, `stream_events`, `transport_sse`, `sessions`, retry half of `errors`) | **DELETE** in favour of a constructed `OpenAICodexLM(api_key=<CLIO credential>, account_id=…)` engine — **owner decision**: that drops WebSocket delta/`previous_response_id` (full resend each call, `prompt_cache_key` only) unless a custom WebSocket engine is kept |
| `_cli_provider` registry, `custom_transports`, factory prefix/CustomLLM wiring, `lazy_tiktoken`/`tiktoken_vendored` (once no route uses LiteLLM) | **DELETE** |
| `lm/factory`, `request_builder`, `dialect_wire`, catalog `litellm_prefix`, capabilities `endpoint.py` | **PORT** to engines / declared providers / `clients/capabilities.py` |
| Everything else in `providers/` (OAuth/login UX, catalogs, handshakes, discovery, SDK process management) | **KEEP** (DSPy-independent) |

Phase 2's tests that pin the adapter wire (`test_clio_react_wire_byte_equality`,
`test_clio_react_fold`, the text-protocol parts of `test_clio_react*`) are re-derived on the typed
`Request`; the ReActV2 differential becomes a semantic differential against 3.4 `ReActV2` with
native function calling (same tool sequence and outputs for a scripted engine).

**Owner decisions (2026-09-29):** 2b approved — the DSPy 3.4 upgrade is step 0 of the rebuild
("that was meant to be step 0"). Live validation runs on the **Codex SDK only**; Claude Code is
tested once development is done. Codex direct: **benchmark stateless (`OpenAICodexLM`) vs
stateful (WebSocket continuation)** on latency and reported cache hits before choosing (needs
the owner's Codex direct login). Guided output: **configurable, default off**. The web-fetch leg
runs against a local `clio-web-search` container.

**Revised phase order (approved)**

- **2b (new, next): DSPy 3.4 + engines + the direct-Request loop.** Pin `dspy==3.4.0`; Codex SDK
  and Claude Code SDK engines (stateful, structural deltas, a minimal text tool-call block because
  those transports take one prompt string — declared by the engine, a malformed block is a visible
  observation); declared providers for OpenAI-compatible endpoints; Codex direct per the owner's
  decision; `ClioReAct` on `lm(Request)` with native tools; streaming and lm.call via the
  send-stream bridge + callbacks; `HookedLM` as a wrapping engine; delete the rows marked DELETE.
- **3** unchanged in goal, built on typed messages: the projection maps clio-core events to
  `dspy.lm15` `Message`s; thinking continuation stored byte-exact.
- **4** shrinks: the adapter repairs are deleted in 2b, not turned into switches; remaining fixes
  (arg repair, path grounding, circuit breaker, observation composition) as recorded switches.
- **5** shrinks to: Codex direct stateful chain if kept custom, per-provider thinking checks.
- **6** unchanged.

## Phase 2b sub-plan (`feat/dspy34-engines`, cut from `feat/clio-react`)

- **A. Pin `dspy==3.4.0`** (the version-pin test follows); suite green.
- **B. Engines** (`src/clio_agent/lm/engines/`), each an `Engine`/`AsyncEngine` pair
  (`complete(Request)->Response`, `stream(Request)->events`, `close`), raising `dspy.lm15` errors
  so DSPy owns retries:
  - **B1 Codex SDK** — from `sdk_transport`/`sdk_stream`/`sdk_stateful`: one prompt rendered from
    the typed `Request` (system + messages + tools); tools described in the prompt and called
    through ONE fenced tool-call block the engine parses into `ToolCallPart`s (declared by the
    engine; a malformed block becomes a visible error observation); SDK reasoning → thinking
    deltas; usage with cached input; one thread per conversation, only the new messages sent
    (structural prefix compare on `Request.messages`), typed resets.
  - **B2 Claude Code SDK** — same shape from `claude_code_litellm`/`_bridge`/`_blocking`; pool,
    reaper, bounds, cancel, audit kept.
  - **B3 Codex direct** — an engine over the existing WebSocket transport (stateful); the stock
    `OpenAICodexLM` (stateless) wired as a second engine for the owner's benchmark.
  - **B4 LM construction** — `create_lm` builds `dspy.LM(model, engine=…, async_engine=…,
    num_retries=<CLIO's transient-retry setting>, cache=False)`; OpenAI-compatible endpoints
    (LM Studio, vLLM, llama.cpp, Ollama, ALCF) as declared providers; hooks
    (BeforeModel/AfterModel) as a wrapping engine; lm.call logging as an `on_lm_end` callback.
- **C. `ClioReAct` on `lm(Request)`** — native `FunctionTool`s from the step's `dspy.Tool`s
  (`submit` is one of them); each step's `Response.message` recorded in CLIO's own codec (text,
  tool calls, thinking with continuation); the context rebuilt as typed `Message`s from the ARC
  plane (tool results as `ToolResultPart`, images/PDFs as `ImagePart`/`DocumentPart` hydrated
  from descriptors); text and thinking streamed live through `send_stream` into the UI lanes.
- **D. Delete** the rows marked DELETE in the re-review (lenient repairs/re-samples,
  `IOLoggingLM`, the field-parsing stream extractor, the thinking-split parser, the promote step,
  CustomLLM shims and registries, LiteLLM prefix wiring, nested retry layers; `lazy_tiktoken`/
  `tiktoken_vendored` once no route uses LiteLLM). Guided output stays as a configurable
  switch, default off.
- **E. Tests** — engine contract tests per engine; the ClioReAct contract re-derived on the
  typed request (append-only messages across steps); the differential against 3.4 `ReActV2`
  with native function calling (same tool sequence and outputs for a scripted engine).
- **F. Live** — Codex SDK only (owner), against the develop baseline.

### B3 result: Codex direct, stateless vs stateful (2026-09-29, owner decision)

Benchmark `opal-work/live/bench/codex_b3.py` (results `D:/t/bench/b3`): three `dspy.LM`
candidates on `gpt-5.5`, reasoning effort `low`, driven with ClioReAct's request shape
(append-only across steps and turns) over a deterministic lab toolset with realistic tool
output sizes; 3 reps, candidates interleaved; a single multi-step turn, a four-turn
conversation, and a return turn after a 12-minute idle gap. Every candidate answered every
turn correctly.

| four-turn conversation (medians) | Codex SDK engine | clio direct (WebSocket) | lm15 `OpenAICodexLM` (stateless HTTP) |
|---|---|---|---|
| TTFT (median / p90) | 1.92 / 4.13 s | **1.18 / 1.61 s** | 1.49 / 2.52 s |
| call wall | 4.47 s | **4.21 s** | 5.15 s |
| turn wall | **6.1 s** | 6.7 s | 8.7 s |
| model calls (3 conversations) | **21** | 24 | 24 |
| input tokens / call | 24,469 | 10,667 | 10,656 |
| cache hit (cached / input) | 81% | 71% | 70% |
| delta sends | 18/21 | 21/24 | 0 |

- clio's stateful WebSocket beats lm15's stateless HTTP at equal cache hit rate and
  correctness: TTFT -21% median / -36% p90, turns ~23% shorter. lm15 stateless is not adopted.
- The SDK carries Codex's own ~14k-token base prompt on every call (2.3x the input); its
  model took fewer steps here, so its turns finished first despite the slowest TTFT.
- After the 12-minute gap: SDK continued its thread (98% cached), clio direct reconnected
  typed (`session_evicted`) at 93% cached, lm15 89% cached.
- Measured side facts: lm15's async transport pools its connection on the first event loop,
  which the loop's per-step `asyncio.run` closes (a B4 item: one persistent LM loop);
  `prompt_cache_key` alone did not make small stateless prompts cache.

**Owner decision:** keep both transports. clio's direct WebSocket engine
(`providers/codex/direct_engine.py`: lm15's payload and parser over a kept WebSocket with
`previous_response_id`) replaces the old LiteLLM direct adapter; CLIO may read the local
Codex CLI login (`~/.codex/auth.json`) when there is no CLIO sign-in; `CLIO_CODEX_TRANSPORT=sse`
is an explicit stateless-HTTP mode, never a fallback. SDK vs direct is re-measured in the
live legs before any removal.

## Phase 3 sub-plan (`feat/context-projection`, cut from `feat/dspy34-engines`)

### What the code does today (read 2026-09-29 at `9b0c3281`)

- **Each turn starts from a blank working set.** `ClioReAct` calls `record.reset_working_set`
  on every forward. That tombstones every live segment of `(session, scope)`, compaction
  summaries included.
- **Earlier turns reach the model only as prose.**
  `session_store._compile_session_conversation_history` wraps the enriched question as
  `Earlier turns … === Current request ===`. It keeps only text, thinking, error and
  compaction parts, so earlier tool calls and results never cross a turn boundary. Hook
  defer/resume then enriches that blob again, which nests the prose inside itself.
- **Injections are concatenated into the question.** `turn_start_offloop` joins files,
  resources, context refs, memory hits, task notifications, plan reminder, todos, replan and
  the prose history. None of them is a recorded event, and the UI only sees a length difference.
- **Steers and child results are glued onto the next MCP observation string** in
  `tools/execution.py`. They are never drained on native tools, on steps with no tool call,
  on failing calls or on `return_raw` calls.
- **Mid-turn auto-compaction loses the turn's own work.** `compact_session_context`
  summarizes the session *ledger*, which does not hold the in-flight assistant message yet.
  It then tombstones every live segment, including this turn's steps. The model loses its own
  tool results and sees earlier turns twice: once in the prose head, once as the
  `[earlier context]` summary.
- **Provider conversations cannot continue across turns.** The head message differs every
  turn, so every turn opens with `prefix_mismatch` and a full resend.
- **Smaller defects.** `user` and `system` segments written by `/context/ops` never reach the
  model. `ContextCompiler` and `ContextRetriever` have no caller. The `/context/compact`
  docstring is stale.
- **ARC is present in every app turn.** When clio-core cannot start, `make_arc_store`
  already degrades loudly and typed to `LocalFSStore`, running the same code paths. `arc is
  None` only for a bare call with no app or react scope (the CLI, unit tests); then the
  loop's own step list is its context.

### What changes (in order; each step lands with its failing-first test, then a commit and push)

1. **Failing-first tests** in `tests/test_gact/test_context_projection.py`:
   - (a) A second turn on the same scope sees turn 1's real steps: messages, tool calls and
     results.
   - (b) The question carries no prose history.
   - (c) A mid-turn auto-compaction keeps this turn's observations available to the model.
   - (d) A steer at a native-tool step reaches the model as a user message at the next step
     boundary, and the observation is left untouched.
   - (e) Prefix stability: across steps and turns, render(n) is a prefix of render(n+1)
     unless an op landed.
   - (f) UI vs agent: every agent-visible event appears in the UI projection, and the UI
     marks injections, compactions, steers and fixes by kind.
2. **One working set across turns.** Delete `reset_working_set` and its call. The forward
   records its user message as a `user` segment (actor `user`): the question text plus the
   user's own attachments (files, resources, context refs) as typed parts. `fold_steps`
   folds `user` segments, and `system` ops, in order. `_head` goes away because the head is
   the first projected message. `arc is None` (the bare call) keeps the in-memory step list.
3. **Delete the prose blob.** Remove `_compile_session_conversation_history` and its caller.
   Defer/resume stores the user's text only.
4. **Injections as recorded events.** A new kind `injection` holds
   `{source, text, actor: "algorithm"}` for memory hits, task notifications, the plan
   reminder, todos and replan. Each is recorded once, at the prefix edge before the turn's
   user message, and again only when its content changes (todos are re-recited only after
   they change). The fold renders an injection as a user-role message headed
   `[clio: <source>]`. `turn_start_offloop` stops concatenating.
5. **Steers and child results at the step boundary.** At the start of every step,
   `ClioReAct` drains the loop inbox and records a `steer` (user role, actor `user`) or a
   `child_result` (actor = the child agent) before projecting. Delete the drain from
   `tools/execution.py`. An item still undrained when the loop goes idle becomes a new turn,
   as today.
6. **Compaction over what clio-core holds.** `compact_session_context` summarizes the
   scope's own projection, meaning every live segment it will replace (this turn's steps
   included), not the ledger. It replaces them with one `summary` through
   `summarize_segments`, which is already a recorded op with `derived_from`. It still
   writes the ledger checkpoint row for the UI, and the one-per-turn limit stays. Manual
   compaction targets the session's primary agent scope explicitly, so it no longer ends in
   `no_active_scope`. Fix the route docstring.
7. **Provider conversations across turns.** The conversation key becomes
   `(session, scope, model)` and a forward no longer releases it. It is released on session
   release, on compaction and on undo/rewind/delete (all typed `ops_reset`), or on a provider
   error. Turn 2 is then a delta send on Codex SDK, Codex direct and Claude Code; a test
   covers this.
8. **UI projection.** Injections, steers, child results and compaction become message parts
   on `_events/m`, each carrying its actor and kind (per the gact-tui SPEC part types; add
   `context_injection` if missing), and gact-tui web renders each as what it is.
   Undo/rewind/delete in the ledger also record ops on the working set, so the two
   projections agree.
9. **Deletions.** Remove each of the following and prove it with a zero-reference grep:
   - `reset_working_set`
   - the prose blob and its helpers
   - the observation-glued drains
   - the dead `ContextCompiler` and `ContextRetriever`, with their tests
   - `segments_to_keys` (the context route reads the projection instead)
   - the stale docstrings
   - the double enrichment of `enriched_text` on resume
10. **Verify.**
    - Full suite, lint and guards.
    - Live multi-turn on Codex direct and SDK: turn 2 is a delta, and the cache share across
      turns is reported.
    - The web UI browser check comes with Phase 4's fix events (DoD 4).

**Platform fallback (recorded decision, to confirm with the owner):** the sanctioned loud
fallback is the existing typed `LocalFSStore` degrade in `make_arc_store`. It runs the same
projection code with clio-core search off, and is visible in the UI and in doctor. No
separate DSPy `History` path is built, since that would be a second context system. Losing
ARC mid-turn is a typed turn failure (`ContextReadError`).

### Phase 3 progress (2026-09-29)

**Landed on `feat/context-projection`** (`d375955d`, `51999a73`, `75dc84af`; full suite 9956 passed, lint/mypy/guards green): steps 1–7 of the sub-plan, the context-frame part of step 8, and undo/rewind rolling back the agent context.

- **The working set spans turns.**
  - The per-forward wipe is deleted.
  - Each forward records its user message as a `user` segment, with media byte-exact.
  - `fold_steps` projects `user` segments, so turn 2 sees turn 1's real steps and answer.
  - A lost plane write is a typed turn failure (`ContextWriteError`), no longer a logged warning.
- **The prose blob is deleted:** `_compile_session_conversation_history` is gone.
- **One loop for every agent kind.**
  - The prompt-only agent and the `predict` / `chain_of_thought` blueprints now run `ClioReAct` with no tools; their reasoning is the model's own thinking.
  - This was needed because those modules never read clio-core, and without the prose blob they would have lost earlier turns.
  - `ClioReAct` now keeps a DSPy module LM (`get_lm` / `set_lm`), and the variant wrapper forwards it. Before this fix, `dspy.BestOfN` / `Refine` over `ClioReAct` raised "Multiple LMs", so variant blueprints could not run.
- **Injections are recorded additions.**
  - `memory_search`, `task_results`, `plan_mode`, `todos` and `replan` return blocks. They are no longer concatenated into the question.
  - The loop records each block once as a user-role message headed `[clio: <source>]` with `actor: algorithm`, ahead of the turn's user message. It records a block again only when its text changes.
  - The context frame lists each injection as `kind: injection`.
- **Steers and child results arrive at the step boundary.**
  - `ClioReAct` drains the loop inbox before every model call. A steer becomes a user message; a child result becomes a `[clio: task_results]` addition.
  - The executor no longer appends anything to tool results.
- **Compaction runs over what clio-core holds.**
  - It summarizes the scope's own projection, the running turn's steps included, and folds exactly those segments.
  - A manual compaction compacts every live scope of the session.
  - If nothing but a lone summary is live, compaction is a typed skip (`nothing_new_since_last_compaction`).
- **Provider conversations continue across turns.**
  - With the projection append-only across turns, turn 2 is a delta send on the kept Codex thread.
  - Test: `test_a_later_turn_of_the_agent_continues_the_thread_with_only_the_new_message`.

**Open:**
- Step 8, the UI rendering of injections in gact-tui web; this lands with Phase 4's browser pass.
- BestOfN run scopes (`agent#runN`) now persist across turns, but each run only continues its own line. Proposed: fork each run from the base scope, and record the winner's answer on the base scope.
- **Undo / rewind follow the ledger:** each scope's working set is rebuilt as it stood before the first rolled-back turn (recorded ops; a rolled-back compaction's originals come back; a kept question keeps its user message).
- `render_keys` (the old trajectory projection) is still on the context route and in the gact-tui SPEC; it goes with the UI-projection step.

## Phase 4 sub-plan (`feat/recorded-fixes`, cut from `feat/context-projection`)

### Owner decisions (2026-09-29)

**Principle.** The harness works with the agent. It gives the agent freedom and a better environment, informs it, and never silently reinterprets what the agent meant. Every piece of data the harness hands the agent is an **injection**: recorded, visible in the UI, and told to the model.

| Fix | Today | Decided |
|---|---|---|
| Oversize tool result | Head + tail JSON envelope; `limits.model_tool_result_chars` = 12000 | The full result goes to a file in the session workspace. The agent is told: "the result is too big; here are the first N chars (N configurable); the rest is in `<file>` for you to explore". |
| Circuit breaker | Blocks after 2 transient failures (constant) | Configurable limit (default 3). When the limit is reached, the agent is told: "you have failed N times; consider an alternative route; this call will be blocked to prevent looping". The block itself is also told. |
| Relative output paths | Resolved against the workspace root | Kept. This is semantics (CWD is the reference), not a repair, and is not an injection. |
| Path-argument repair | Silently substitutes a unique basename match | **Removed.** The call fails with "`<arg>`: `<path>` not found. Did you mean `<match>`?", and the agent decides. |
| Artifact identity / elicitation notes, tool-media relocation | Appended notes, not recorded | Recorded as injections. |
| Hook effects (BeforeModel patch/route, AfterModel rewrite, PreToolUse modify/synthesize, PostToolUse rewrite/deny) | Only `hook.invoked` (trace-only) | Recorded as injections, with what changed. |

**UI.** One special **`injection` message part**, used for every harness addition: turn additions (plan reminder, todos, replan, memory hits, task results), fix notices, hook effects and cleanup. gact-tui web and desktop render it with a vaccine/syringe icon, expandable to the exact text the agent got. `PARTS.md` and `SPEC.md` gain the kind.

### Steps (each with its failing-first test, then commit and push)

1. **The `injection` part.**
   - Minted on `_events/m` by the turn minter with `{source, text, actor: "algorithm", call_id?}`.
   - The loop mints one whenever it records an addition on the plane: turn injections, steers from CLIO, `task_results`.
   - Test: every agent-visible `[clio: …]` message has an `injection` part in the UI projection (the UI-vs-agent test from phase 3 step 1f).
2. **Fix notices as injections.** A tool-result note the harness adds is recorded as an `injection` tied to the call id, in the same text the model got. Test (fix-recorded-and-told): for each fix, the observation carries the note AND the UI has the injection.
3. **Oversize results spill to a file.**
   - The file goes under the session workspace's `.clio/tool-results/`.
   - The agent is told the head N (`limits.model_tool_result_chars`) and the path.
   - Delete the head/tail envelope.
4. **Configurable circuit breaker** (`tools.circuit_breaker.failure_limit`, default 3), with the warning at the limit.
5. **Path repair becomes a suggestion.** Delete the substitution and return the "did you mean" error.
6. **Hook effects recorded as injections.**
7. **gact-tui web.**
   - Render `injection` (syringe icon, expandable).
   - Remove the old `render_keys` context view in favour of the projection.
   - Verify in the browser with the Claude in Chrome tools against a `CLIO_WEB_DIR` instance (DoD 4).

### Phase 4 progress (2026-09-29)

**Landed on `feat/recorded-fixes`**, in `f171629f`, `fc561d5f`, `81125901`, `7eb2139a`, `eab64725` and `47f9a3a5`:

- **Path repair became a suggestion.** The call runs exactly as the agent asked. If it fails, the result (or the raised error, as a note) carries `[clio: path_hint]` listing the same-named files that do exist. The silent substitution is deleted. It could redirect an output file the agent meant to create.
- **Circuit breaker.**
  - Configured by `tools.circuit_breaker.failure_limit` / `CLIO_TOOL_FAILURE_LIMIT` (default 3; 0 turns it off).
  - The failure that reaches the limit tells the agent to take an alternative route.
  - A blocked call says what failed and why it was not run.
- **Oversize results.**
  - The full result goes to the session's tool-output folder.
  - The agent gets the head (`limits.model_tool_result_chars`) plus the file path to explore.
  - The head/tail envelope is deleted.
  - Bounding runs on the calling thread, so the file lands in the session workspace.
- **The `injection` part.**
  - Every CLIO addition, and every note the harness adds to a tool call, is minted as `injection {source, text, call_id?}` with the exact text the agent got. This covers turn additions, the path hint, the circuit breaker, spilled results and hook effects.
  - It serializes to v3 as `type: injection`.
  - Tests:
    - UI vs agent: every `[clio: …]` message the agent sees has an injection part.
    - Fix recorded and told: every executor note is collected with its call id.
- **Hooks.**
  - PreToolUse changing or answering a call, and PostToolUse replacing or objecting to a result, are told to the agent in the result and recorded.
  - BeforeModel route, patch or answer, and an AfterModel rewrite, are recorded for the user.
- **gact-tui web** (`feat/injection-parts`, `91df1449`).
  - It renders the `injection` block with a syringe icon, named in plain words and expandable to the exact text.
  - The contract is documented in `contract/PARTS.md`.
  - The block is a client-local schema (as `compaction` is). Adding it to `clio-schemas` needs a schema release, which is the owner's call.

**Found live and fixed:**

- **A Codex/Claude Code bind right after launch** answered 401 "models are being checked". The bind now waits, bounded, for the startup check. A Codex bind now checks the models itself when nothing has, as the Claude Code bind does.
- **A regenerated plot failed a later turn.** Cross-turn history re-read an old `view_image` file whose hash had changed. `view_image` and `view_pdf` now snapshot the viewed bytes, and history reads the snapshot. Media that history can no longer show becomes a `[clio: media_unavailable]` note, never a failed turn.
- **A Codex WebSocket 1012 (service restart) failed a turn** with a raw error. Before any output, the call reconnects and resends. Mid-reply, it is a clear `ServerError`.

**Live, phase 3 tree, Codex direct (medians of one run; develop baseline in brackets):**

| scenario | wall | model calls | cache share | full sends |
|---|---|---|---|---|
| earthscope | 216 s [397] | 18 [18] | 91% [62%] | 1 of 18 (the only turn-boundary resend is gone) |
| deep-researcher | 1362 s [2538] | 164 | 88% [63%] | 10 of 164 |
| data-semantics | 239 s [430] | 28 [34] | 76% [70%] | 4 of 28 |
| opal | 1085 s | 50 | 93% | 3 of 50 |

- **Factorio evals:** 21 failure lines [25].
- **SDK transport on the same tree:** deep-researcher made one model call, never called a tool, and claimed "the delegation tool failed". This is the SDK text-protocol tool-use weakness seen earlier.

**Open:**

- The Go TUI rendering of `injection`, last per the owner.
- ~~Removing `render_keys` from the context route and the SPEC.~~ Done (see below).
- ~~The web UI browser verification (DoD 4).~~ Done (below).
- Whether `injection` goes into a `clio-schemas` release.

### Web UI verification (DoD 4), 2026-09-29

**Setup.**
- gact-tui web at `feat/injection-parts` (`91df1449`), served same-origin (`CLIO_WEB_DIR`) by `feat/recorded-fixes`.
- Isolated instance (`live/serve_ui4.sh`, port 17995), driven in Chrome with the Claude in Chrome tools.
- Model: Codex direct `gpt-6-sol`. Realistic prompts on the OPAL APPL-CORE export.
- The automation window is not on screen, and the web client pauses its live stream for a hidden tab (`use-session-live-stream`, by design). The check therefore marks the page visible and dispatches `visibilitychange`, the event the client listens for. The same test against `develop` behaves identically.

**Verified live, and again after reload:**

| Item | Result |
|---|---|
| Thinking streams | The reasoning summary shows as a Thinking block (after fix `f190b47a`). |
| Concurrent tool calls | One step's calls are grouped ("Bash +4"). |
| Approvals | The approval card appears live; "Allow for session" continues the turn. |
| Injections | "… gave the agent: Large result saved to a file" (syringe icon) shows live; expanded, it is the exact text the agent got. |
| Steer | A steer queued mid-turn shows as a user message and changes the answer: "keep it short, and say how many plants" → "360 distinct plants". |
| Final answer | Renders as the message body live, not as an activity row (after fix `de7d1a6f`). |
| Multi-turn | A follow-up ("which of those measurements…") uses the earlier turn without re-asking. |
| Cancel | Stop interrupts within about 3 s; the tool row shows "Interrupted". |
| Compaction | The "Context summarized · Requested" checkpoint shows live; the next question is answered correctly from the summary with no tool call. |
| Reload == live | 177 page-text lines each. The only difference is the steer's timestamp: live shows when it was sent, reload shows when it was consumed. |

**Fixed during the check** (each with a failing-first test):
- `99aa6dc6`: an agent new to the conversation starts from its earlier turns.
- `de7d1a6f`: a part that changes after it streams reaches live clients whole. The promotion and annotations published a patch, which v3 turned into an empty block.
- `f190b47a`: Codex direct asks for its reasoning summary.

**Open findings:**
- **gact-tui:**
  - The composer label shows "Codex · SDK / Sol" after Direct / Sol is chosen, although the server binds `variant: direct`. The label resolves by model id.
  - Activity titles show raw markdown (`**…**`).
- **clio-core:** a hard-killed daemon leaves a partial `storage.bin`. The next start's capacity preflight refuses it and ARC degrades (loudly) to LocalFS. On Windows every forced stop does this.

### After the UI check (2026-09-29)

- **gact-tui findings fixed** (`feat/injection-parts`):
  - `323a79b5`: the composer names the session's own transport. The server side is `b3cf96c3`: v3 sessions carry `model_transport`.
  - `44a2afa5`: activity titles drop inline markdown.
- **`render_keys` removed.**
  - `eb9f50fe` (clio-agent) and `403b93fb` (gact-tui SPEC and clients): the context state carries `messages`, the scope folded exactly as the loop folds it (`context_view.context_messages`).
  - `segments_to_keys`, `SegmentStore.render_keys` and `ARCMemory.render_segments_keys` are deleted, along with the tests of the dict's shape.
  - A fidelity fuzz over `fold_steps` replaced them. It found a tool call with no open step losing its name and arguments; that is fixed.
- **Folded history marks retired atoms** (`9c58dc7c`). `list_segments(include_tombstoned=True)` on the folding store listed compacted or deleted atoms as `live`. It was found by the live compaction probe.
- **Live ARC probes** (`f03f49a9`).
  - The `CLIO_RUN_LIVE` tests still called deleted ReActV2 methods. Being skipped, they had gone unnoticed since phase 2.
  - They now drive `ClioReAct` with one real call over the folded plane. A live-marked test runs the operator's configured model, where it was pinned to the unit-test model.
  - 62 of 62 pass on Codex direct: needle recall while present, gone after delete, as-of time travel, and a real auto-compaction.
- **Docs** (`8a68f79d`): `docs/tui/08-semantics-and-lifecycle.md` describes the cross-turn context, injections and fix notes; `docs/providers/claude_code.md` notes the session carries over across turns.
- **Operator notes** (`fcf95cce`) for `tools.circuit_breaker.failure_limit` and the spill limit.
- **Full suite:**
  - At `fcf95cce`: 9980 passed, 1 failed (the missing operator note, fixed in that commit), 100 skipped. The same ~100 skips have been there since phase 1: optional dependencies and platform- or live-gated tests.
  - At `8a68f79d` (rerun with `-n 2` after the host stopped the first attempt for low memory): **9989 passed, 0 failed**, 100 skipped.
    - The skips are all gated: 58 live (the 55 live ARC ones pass live on Codex direct, see above); 21 platform (Linux-only deploy / Landlock / setpriv, Windows symlink / POSIX bits); 9 relay configuration; 1 `flowcept` not installed.
  - Guards (size ratchet, silent fallbacks, env reference) and ruff are green.

### Deletion inventory (DoD 6), at `5913d866`

**Lines, merge-base `c7a87d73` (develop) to each stacked branch:**

| branch | files | + | − |
|---|---|---|---|
| `feat/codex-sdk-stateful` | 16 | 1,464 | 410 |
| `feat/clio-react` | 149 | 5,778 | 7,674 |
| `feat/dspy34-engines` | 262 | 11,941 | 21,126 |
| `feat/context-projection` | 299 | 13,094 | 21,834 |
| `feat/recorded-fixes` (the chain's tip) | 344 | 15,305 | 23,091 |

At the tip: `src` +6,144 / −11,226; `tests` +8,891 / −11,402; the rest +270 / −463.

**Zero-reference greps over `src tests scripts`:** `instrumented_forward`, `_RetainingReActV2`, `_RetainingReAct`, `reactv2_upstream`, `reset_working_set`, `_compile_session_conversation_history`, `segments_to_keys`, `render_keys`, `_format_trajectory`, `REPEATED_TRANSIENT_FAILURE_LIMIT`, `_repair_missing_file_arguments`, the ChatAdapter name spoof, the per-executor `_call_lock`.

- `loop_inbox_drain` remains, as the new step-boundary drain: arrivals become their own user messages.
- No `CLIO_*` variable or config key selects an old loop, trajectory or history path (`docs/ENVIRONMENT.md` checked).

## Phase 7: session bring-up off the first message (`feat/session-bringup`, cut from `feat/recorded-fixes`)

**Owner (2026-09-30):**
- 248 s against 397 s is not enough; iowarp/clio-coder is much faster.
- The start of a session is a big waste: CLIO waits for the first message to initialise the session and to start its MCP servers.
- Start them early, and block only when a call needs a server that is still starting.
- This supersedes the #1237 ruling ("activation mounts nothing eagerly").

**Measured (earthscope, Phase 4 tree):** 78 s of the 206 s first turn passed before the first model call (`blueprint.resolve` 74 s):
- The four declared servers mounted in a serial loop (`builders.py`), after the first message.
- Each was spawned twice, once for a listing client and once for the persistent connection, at ~4 s per spawn through uv.
- About 40 s went to discover-probe timeouts on a cold pandas server.
- The launcher-cache file lock serializes cold spawns. It guards a real uv cache race (astral-sh/uv#11694) and stays.
- Field report (Utah CHPC, via the patch-release session): "Setting up session" after every answered question. This is the same cost, because the idle reaper (120 s) closes the servers while the user answers.

**What clio-coder does** (read at `4f03d2c`):
- one long-lived agent per conversation;
- the system prompt and tool list frozen per session;
- MCP servers spawned on first use, from a 24 h disk listing cache;
- about 8 tools, the rest behind a gateway tool;
- reasoning `low` by default.

**Landed:**
- `7cd7e324`: a turn waits for tool listings, not connections. A server connects when a call needs it, and the executor's per-namespace connect joins concurrent callers.
- `c13de240`: creating a session or activating a blueprint starts its servers (the blueprint's, plus always-load services) concurrently in the background, controlled by `tools.mcp.session_warmup` (default on).
- `c8d07ffa`: every turn start kicks the warm-up, once per session at a time. This covers a resume after the reaper closed the servers.
- `55cea154`: spawned FastMCP servers skip their banner and its pypi.org update check.

**Result (earthscope, cold listing cache as the bench always has):**
- `blueprint.resolve`: 74 s → 24 s.
- First turn: 208 s → 166 s.
- Run: 248 s → 222 s.
- With a warm listing cache (normal use, 24 h), the first model call waits on no server at all.

**Profiled on our own tree (owner, 2026-09-30: no more develop baselines; only semantics-preserving wins):**

Earthscope's first turn, after start-up: 176 s = model 140 s (17 serial calls, ~2 s TTFT each) + tools 26 s + harness ~11 s.
- Five steps were single `load_skill` calls. Five were one A2UI component schema each, followed by a 26–64 s decode of the surface JSON. The owner's `feat/a2ui-data-everywhere` branch targets both: multi-file `load_skill`, `$defs` inlining, `dataUri`.
- One mid-chain call got 0 cached tokens despite a correct `prompt_cache_key` and `previous_response_id` delta. This is the server's cache; we count it per run.
- Per-turn prologue: py-spy across all threads puts the YAML frontmatter parse of every blueprint, expert and pack at ~0.5 s per turn (now memoized on the text). The rest of the prologue is ~0.5–1 s: skill scans, blueprint metadata.
- Model speed varies ~2x between identical runs (110 s vs 218 s), so single runs judge only model-independent phases.

**Landed since:**
- `c3cc7e93`: a session waiting on its user keeps its servers (`tools.mcp.hold_while_waiting_s`).
- `7339ece0`: frontmatter memoized.
- `f34dde7a`: an agent with tools is told once (an injection) that a step may call several. On its own this did not change the serial skill loads, because they were dependent fetches.
- `4325474f`: clio-kit servers skip the shared uv-cache lock. clio-kit isolates its own cache and environments, so the lock only serialized their starts.
- `28463f10`: one concurrent mount path for a turn and the warm-up.

**Result:** turn start to first model call on a cold listing cache went from 28 s to 6.3 s (two runs: 6.39 and 6.33 s). Follow-up turns take 1–4 s.

**Researched (progressive tool disclosure):**
- Any mid-conversation tool-list change resets every stateful transport: Codex direct `prefix_mismatch`, and the Claude Code and Codex SDK sessions restart.
- A disclosure design must therefore keep the tool list byte-identical: a category index plus stable `tool_info`/`call_tool` tools. That is the MCP client guidance too.
- It removes the listing wait from the first call. It saves little TTFT while the cache hit rate is ~90%.
- It waits on the owner's go-ahead.

**Next:**
- One spawn per server: list over the persistent connection. The listing currently also records the server's task capability, which the connect route reads (#1281), so the two must be reordered together.
- The reaper keeps a session's fleet while the session waits on a question.
- Owner decisions: reasoning effort for tool-routing steps; a smaller tool set behind a gateway tool.

## Definition of done

1. Full suite green on every phase branch (`pytest tests -m "not integration"`): zero failures,
   zero errors; skips only the documented platform/live-gated ones. ruff, ruff format, mypy,
   `scripts/check_file_size.py`, `scripts/check_silent_fallbacks.py`,
   `scripts/gen_env_reference.py --check`. Failing-first tests for every bug fixed.
2. New tests: ClioReAct differential vs stock ReActV2; projection prefix-stability; UI-vs-agent
   projection; fix-recorded-and-told; Codex stateful (exists).
3. Live, against the phase-0 baseline (isolated instance with `CLIO_USER_DIR`, Codex SDK,
   realistic short human prompts): `scripts/live_verification` legs (synthetic session,
   compaction, goal judge, web fetch, stress, deep researcher — per `RUNBOOK.md`),
   clio-agent-marketplace `earthscope-single-agent` multi-turn session, `factorio-flat` behavioral
   evals via its evaluator, `deep-researcher`, `data-semantics`. Report per turn: wall time,
   steps, input/cached/output tokens, full vs delta sends, one provider thread per conversation.
   Clearly faster, high cached share, no quality regression (eval scores / leg verdicts equal or
   better).
4. gact-tui web UI verified in a real browser (screenshots/GIF) — driven with the Claude in Chrome
   tools against the isolated live instance started with `CLIO_WEB_DIR` pointing at a built
   gact-tui `web/dist` (served same-origin by `gact/app.py`): thinking streams live; tool calls
   and results render (concurrent too); injections, compaction checkpoints, edits and fixes
   visible as such; a mid-turn steer lands as a user message; cancel stops a running turn
   promptly; reload == live; a multi-turn follow-up reuses prior context without re-sending.
5. Docs describe what IS: this doc updated per phase; stale docs fixed
   (`docs/providers/claude_code.md`, `docs/tui/08-semantics-and-lifecycle.md`).
6. **The old loop is gone.** A deletion inventory per phase (what was removed, with the grep
   proving zero remaining references: `instrumented_forward`, `_RetainingReActV2`,
   `reactv2_upstream`, the adapter name spoof, the prose history blob, the per-turn working-set
   wipe, observation-glued steers/child results, the DSPy-history read fallback, the per-executor
   call lock, workflow-era leftovers). No flag, env var or config key selects an old path.
   `git diff --stat develop...` over the final branch shows deletions matching or exceeding
   additions (tests included); if not, the report explains line by line why.

## Rules

- **Full replacement, one transition.** This is not an alternative loop behind a flag: no dual
  paths, no old-vs-new switch, no compatibility shims, no "legacy" fallback kept alive. Each phase
  deletes, in the same branch, the code it replaces; expect deletions to match or exceed additions
  across the campaign. Phases STACK (each phase branch is cut from the previous phase's branch);
  there is ONE merge, when the owner says, after which the old loop never exists again.
- **The design is the source of truth.** Where existing code, docs or a client expect the
  old behavior, the client/docs change to the design — never the reverse. Client priority:
  the gact-tui web/desktop app first (verified in the browser), the terminal TUI last.
  The campaign owns the whole stack: clio-agent AND gact-tui (web, desktop, TUI) change
  together, in stacked branches, for the users that matter — non-CS scientists on the
  web/desktop app.
- Stacked phase branches in worktrees; **commit and push after every coherent step**; never
  merge to develop/main or open PRs until the owner says (one merge at the end). Conventional commits; no
  Claude/Co-Authored-By attribution line.
- Read before changing (the relevant `docs/design/*` and the code path); keep DOES vs SHOULD
  separate. No silent fallbacks, no deterministic decisions on model prose, no accretion into god
  files, no ratchet raises.
- Ask the owner for: Codex direct login, fix defaults, whether `next_thought` is required only for
  non-thinking models, merge/PR timing, anything that contradicts this doc.

## `/goal`

```
/goal Complete the clio agent-loop rebuild campaign exactly as specified in docs/design/agent-loop-rebuild-2026-09.md on branch docs/agent-loop-rebuild of iowarp/clio-agent (read it in full first, plus the docs it cites). Done means every item of its Definition of done holds with evidence: suite, lint and guards green on every phase branch; the ClioReAct differential, projection-stability, UI-vs-agent-projection and fix-recorded tests passing; live verification legs and marketplace agents (earthscope-single-agent, factorio-flat evals, deep-researcher, data-semantics) clearly faster than the develop baseline with no quality regression; the gact-tui web UI verified in a real browser for thinking streaming, concurrent tool calls, injections, compaction, fixes, steer, cancel and reload==live; docs updated; the old loop fully deleted (no flags, shims or dual paths; deletions >= additions; one stacked branch chain, one merge when the owner says). Commit and push every branch after each step, never merge, no Claude attribution. Pause and ask the owner for Codex direct login, fix defaults, the next_thought decision and merge/PR timing.
```
