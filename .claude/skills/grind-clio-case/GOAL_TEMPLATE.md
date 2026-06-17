# Goal: <Case Title>

## Objective

Turn the **grounded** <one-line question> into a working CLIO Agent Blueprint
case that runs through the normal orchestrator against the live NDP catalog and a
live provider (ALCF), produces <the vivid artifact> via <the MCP tool>, and is
accepted only after trace + artifact + provenance review. The question, datasets,
and a hand-built solution already exist in this folder (`README.md`,
`manual-solution/`, `manual-solution/DATASETS.md`) — this goal is the
build-and-prove phase, not a re-grounding phase.

The case prompt is natural and names no expert, tool, or schema:
> see `prompt.txt`.

## Non-negotiable method (do not regress on these)

- **No gates, no fake data, no mocks in `src/`.** Acceptance = a real live
  session whose trace, per-branch evidence, artifact, and provenance are
  inspected by hand (no human in the loop — the agent reads them) and match the
  prompt intent. A passing JSONL counter is not a pass.
- **Decisions live in the agent's reasoning; code only runs tools and records
  their data.** No runtime selection heuristic, no runtime outcome decision, no
  env var that force-feeds the answer. The runtime may persist data and compute
  geometry via tools, never *choose*.
- **Routing is DSPy-typed, not string-matching.** Handoffs come from typed
  workflow state, never `when_request_contains` or forced mandatory branches.
  Must generalize across scenarios without case-specific string contracts.
- **Difficulty is intrinsic.** Selection is by meaning, not by size/parsing. The
  "boring" outcome (nothing found / no impact) is a correct, reachable answer.
- **Work in the session workspace** (artifacts under the workspace, not stray
  `.local/` paths).

## Testing harness (agent-test)

Use `~/agent-test` as the run + acceptance harness. Division of labor: **the
agent monitors semantics — reads traces, discovers failures, makes design
decisions; agent-test monitors data pathways — assertions on tools, trajectory,
artifacts, routing; the agent iterates to improve both.** Every failure found by
reading a trace becomes a new matcher; the green grid never substitutes for the
trace read.

First iterate the case to *productive* on a single cell with trace review. DEFER
the model-matrix and prompt-search features until the case works — those are the
payoff phase, not the setup phase.

CLIO live-run recipe (the SUT `invoke` reuses `scripts/run_demo_benchmark.py`):
start the gact server → `GET /v1/health` → `GET/POST /v1/workspaces` →
`POST /v1/agent-blueprints/install` {source, scope:workspace, workspace_id} →
`POST /v1/sessions` → `POST /v1/sessions/{id}/agent-blueprint` {blueprint_id} →
post the prompt turn → read session messages + children as the trace → normalize
into an agent-test `Run`.

## Priority order

1. **Wire any new tool** into clio-agent (register the MCP in the gateway,
   focused registration + render smoke tests). Skip if the tools already exist.
2. **Author the marketplace pack** `<case>-review` using the **domain-grouped,
   re-entrant topology** (NOT a linear chain). `main` re-decides after each
   domain on typed workflow state. Each expert: real 500+ word domain prompt,
   typed signature, 5–7 curated tools. The selection logic lives in an expert's
   reasoning over typed state, never as a routing string. Install workspace-scoped.
3. **Stand up the harness + first live run.** Write the SUT adapter usage and the
   acceptance test (canonical matchers + `extra` matchers for the intrinsic
   difficulty and no-forced-routing). Validate wiring fast, then ONE live run on
   the guardrail cell. Capture to `runs/`, review by hand. Single cell only.
4. **Grind to acceptance.** Iterate (r1…rN, traces in `runs/`) until reviewed
   runs are clean across mutated scenarios and the negative path. Encode each
   regression found during review as a matcher / unit test (state-space, not one
   happy path). Only once productive: turn on matrix + prompt search.
5. **Finalize the case spec.** Update `README.md` to the accepted contract; keep
   all run logs in `runs/`, never in the spec.

## Branching

- clio-agent: `feat/<case>` (use a git worktree to isolate from other grinds).
- marketplace: `feat/<case>-pack`.
- Rebase `main` into open branches after each merge.

## Done criteria (hard — ALL must hold)

Provider for the whole grind: `argonne_metis` / `gpt-oss-120b` (guardrail cell).
Each run writes a trace to `runs/`; the agent reads every accepted run's trace
and records a one-line verdict.

1. **Reliability:** the case test passes live at **≥0.8 over ≥10 sampled runs**;
   report the Wilson interval (lower bound ≥0.6). One green run is NOT done.
2. **Generalization:** the same bar (≥0.8 over ≥10) holds for **≥3 distinct
   scenarios** via mutated prompts. Traces for all in `runs/`.
3. **Honest negative path:** at least one scenario whose correct answer is the
   "boring" one returns it correctly (no forced artifact), asserted by a
   dedicated test, ≥0.8 over ≥10.
4. **Tamper-proof matchers:** every matcher reads structured evidence and is
   proven offline to FAIL a tampered run.
5. **Suite grew from review:** every distinct failure found by trace read is
   encoded as a matcher / unit test.
6. **No regressions:** the other cases' suites still pass; changes additive + scoped.

## Status

<!-- Updated every iteration. Current r, measured rates per scenario, what's
     next, what's blocked. Keep run logs in runs/, the rolling verdict here. -->

- r0: not started.
