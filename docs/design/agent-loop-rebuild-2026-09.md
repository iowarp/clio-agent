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
4. gact-tui web UI verified in a real browser (screenshots/GIF): thinking streams live; tool calls
   and results render (concurrent too); injections, compaction checkpoints, edits and fixes
   visible as such; a mid-turn steer lands as a user message; cancel stops a running turn
   promptly; reload == live; a multi-turn follow-up reuses prior context without re-sending.
5. Docs describe what IS: this doc updated per phase; stale docs fixed
   (`docs/providers/claude_code.md`, `docs/tui/08-semantics-and-lifecycle.md`).

## Rules

- **Full replacement, one transition.** This is not an alternative loop behind a flag: no dual
  paths, no old-vs-new switch, no compatibility shims, no "legacy" fallback kept alive. Each phase
  deletes, in the same branch, the code it replaces; expect deletions to match or exceed additions
  across the campaign. Phases STACK (each phase branch is cut from the previous phase's branch);
  there is ONE merge, when the owner says, after which the old loop never exists again.
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
/goal Complete the clio agent-loop rebuild campaign exactly as specified in docs/design/agent-loop-rebuild-2026-09.md on branch docs/agent-loop-rebuild of iowarp/clio-agent (read it in full first, plus the docs it cites). Done means every item of its Definition of done holds with evidence: suite, lint and guards green on every phase branch; the ClioReAct differential, projection-stability, UI-vs-agent-projection and fix-recorded tests passing; live verification legs and marketplace agents (earthscope-single-agent, factorio-flat evals, deep-researcher, data-semantics) clearly faster than the develop baseline with no quality regression; the gact-tui web UI verified in a real browser for thinking streaming, concurrent tool calls, injections, compaction, fixes, steer, cancel and reload==live; docs updated. Commit and push every branch after each step, never merge, no Claude attribution. Pause and ask the owner for Codex direct login, fix defaults, the next_thought decision and merge/PR timing.
```
