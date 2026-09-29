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

**Revised phase order (owner to approve)**

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
