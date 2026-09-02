# Goal: Deep Researcher (Web Synthesis) — case14

## Objective

Prove that the marketplace `deep-researcher` pack — already authored upstream
in `external/clio-agent-marketplace/deep-researcher`, not built by this case —
runs end-to-end on clio-agent through the unified MCP v2 client: a flat
coordinator dynamically fans out `researcher` leaves, sends the assembled
evidence through an independent `critic` leaf, and produces a cited Markdown
report, all driven by the clio-kit `web` MCP's `task=required` `web_fetch` /
`web_search` tools. This IS acceptance **leg (iii)** of the
MCP-client-unification campaign
(`docs/design/mcp-client-unification-2026-08.md`, issue
[#1286](https://github.com/iowarp/clio-agent/issues/1286), umbrella #1274).
The question, prompt, and stability rationale already exist in this folder
(`README.md`); this goal is the build-and-prove phase for the harness, not a
re-grounding phase — and per #1286, the actual live proving happens at the
campaign's own **C1-S6 gate**, not during scaffolding.

The case prompt is natural and names no expert, tool, MCP, or schema:
> see `prompt.txt`.

## Non-negotiable method (do not regress on these)

- **No gates, no fake data, no mocks in `src/`.** Acceptance = a real live
  session whose trace, per-leaf evidence, artifact, and provenance are
  inspected (no human in the loop — the agent reads them) and match the
  prompt intent. A passing JSONL counter is not a pass.
- **This case does not author agent reasoning.** Unlike EarthScope/wildfire/
  case13, the `deep-researcher` pack's evidence discipline (FACT vs
  INFERENCE, citation-to-claim mapping, independent critic re-verification)
  is already declared in its own `AGENT.md`/`experts/*.md`. This case's job
  is proving clio's **generic MCP client plumbing** lets that pack actually
  exercise `task=required` fetch/search — never editing the pack's prompts to
  paper over a plumbing defect, and never hardcoding routing/selection logic
  into clio core to make the pack "pass" (⚑ superseding principle #1/#3).
- **Routing is agent-driven spawn, not workflow handoffs.** `main` dynamically
  delegates via `spawn_agent_task`/`spawn_agents_parallel` (C1-S1's
  capability-keyed spawn runtime) — there is no declared workflow, no forced
  worker count, no fixed round count. Matchers must never assume a fixed
  number of `researcher`/`critic` spawns.
- **Difficulty is intrinsic to the plumbing, not the topic.** The research
  topic (HDF5) is deliberately easy/stable — see `README.md` — because the
  hard part this case proves is whether `web_fetch`/`web_search` actually
  reach the server through the generic client, not whether the model can
  reason about a hard scientific question.
- **Work in the session workspace** (the report artifact lands under the
  workspace, never a stray path — same discipline as every other case).

## Testing harness (agent-test)

Use `~/agent-test` as the run + acceptance harness, same division of labor as
every other case: agent-test/matchers monitor the data pathway (tool calls,
artifacts, per-child-session attribution); the agent monitors semantics by
reading the trace. Every failure found by reading a trace becomes a new
matcher; the green grid never substitutes for the trace read.

**Case-specific harness wrinkle:** `main` never calls `web_search`/`web_fetch`
itself — only its `researcher`/`critic` children do, each on its OWN session.
`clio_sut.ClioAgent._to_run` (shared with earthscope/wildfire/case13) only
parses the TOP session's own `tools_called`, so
`tests/test_real_cases/test_deep_researcher_case.py` re-fetches every direct
child session's messages itself (`GET /v1/sessions` filtered by
`parent_session_id`, then `GET /v1/sessions/{child_id}/messages`) and
attributes each child's tool calls to its spawning expert via the child
`Session`'s own `agent.id` field (confirmed in
`src/clio_agent/gact/turn_spawn.py`: `spawn_child_turn` stamps
`agent={"id": spec.child_expert_id, ...}` on the minted child session) — the
same evidence-augmentation-in-the-test-file pattern case13 used for its
door-side/artifact-lineage seams, so `clio_sut.py` stays untouched.

CLIO live-run recipe (same as every other case, `clio_sut.ClioAgent.invoke`):
start the gact server → `GET /v1/health` → `GET/POST /v1/workspaces` →
`POST /v1/agent-blueprints/install` `{source: <absolute local path to
external/clio-agent-marketplace/deep-researcher>, scope: workspace,
workspace_id}` → `POST /v1/sessions` →
`POST /v1/sessions/{id}/agent-blueprint {blueprint_id: deep-researcher}` →
post the prompt turn → read session messages + children (recursively re-fetched
per this case's wrinkle above) as the trace → normalize into an agent-test
`Run`.

## Case-specific deviations from the template

- **Provider/guardrail cell: `claude_code` / `sonnet`** (subscription), not
  `argonne_metis`/`gpt-oss-120b`. Per issue #1286's own leg (iii) text and the
  live-tests-use-claude/codex convention (subscription providers for
  live/multi-session runs). Overridable via `CLIO_AGENTTEST_CELLS`.
- **No reliability-sampling bar is claimed by this GOAL.** Unlike the
  domain-discovery cases' `>=0.8 over >=10 sampled runs` Done criterion, issue
  #1286 leg (iii) asks for a single reviewed live pass as the campaign's
  landing proof ("the marketplace deep-research expert works end-to-end on
  clio-agent as the user-visible proof"). A broader reliability grind (this
  file's own Done-criteria table below) is a stretch goal once the campaign
  has landed, not a gate this case's Done criteria invents unbacked-by-#1286.
- **No blueprint authorship step.** The pack is adopted as-is from
  `external/clio-agent-marketplace/deep-researcher`; if the live grind finds
  a genuine pack defect, that fix lands upstream in the marketplace repo, not
  by editing clio-agent core or bolting case-specific prompt patches onto the
  pack from this side.

## Priority order

1. ~~Wire the `web` MCP tool into clio-agent.~~ Not this case's job — that is
   leg (ii) of #1286 (the clio-kit web MCP `task=required` fetch conformance
   work, landing on `feat/mcp-client-unification` C1-S2..S5). This case
   consumes that plumbing, it does not build it.
2. ~~Author the marketplace pack.~~ Not this case's job — `deep-researcher`
   already exists upstream, authored and versioned independently.
3. **Stand up the harness (this slice, `slice/c1-s6-deep-researcher-case`).**
   `benchmark/case14-deep-researcher-web/` (this folder) +
   `tests/test_real_cases/test_deep_researcher_case.py`, gated exactly like
   the sibling real-case tests (`CLIO_RUN_LIVE=1` + `live` + `real_case`
   marks; collects cleanly with zero import errors when the live gate is
   off). SCAFFOLDING ONLY: no live LM turn, no provider boot, during this
   slice.
4. **First live run — at the campaign's own C1-S6 gate**, once C1-S2..S5 have
   landed on `feat/mcp-client-unification` (the generic MCP v2 client, the
   web MCP `task=required` conformance legs). Capture the trace, review by
   hand: did `researcher` actually fetch/search, did `critic` independently
   re-verify, did a real Markdown report land in the registry?
5. **Grind to acceptance (stretch, post-landing).** If the owner elevates
   this case to the same reliability-sampling bar as the domain cases,
   iterate per the standard loop (r1…rN, traces in `runs/`) and update the
   Done-criteria table below with a measured rate.
6. **Finalize the case spec.** Update this folder's `README.md`/GOAL.md once
   a real live run has been read; keep all run logs in `runs/`, never in the
   spec.

## Branching

- clio-agent: this slice on `slice/c1-s6-deep-researcher-case`, off
  `feat/mcp-client-unification` (branched from 954f8b9f).
- marketplace: none — `external/clio-agent-marketplace/deep-researcher` is
  consumed as-is; a pack defect found during the live grind gets its own
  marketplace-repo fix, not a patch from this side.
- Merge `slice/c1-s6-deep-researcher-case` back into
  `feat/mcp-client-unification` per the campaign's own slice-merge discipline;
  do not merge to `develop`/`main` directly — the campaign lands as a whole
  at its own C1-S6 acceptance.

## Done criteria

**Gate bar (what #1286 leg (iii) actually asks for — the campaign's landing
requirement):**

1. The harness collects cleanly (zero import errors) with the live gate off —
   verified during scaffolding, this slice.
2. At the C1-S6 gate, ONE live run through
   `test_deep_researcher_web_synthesis` passes AND is hand-reviewed from its
   trace: a `researcher` leaf performed a real successful `web_search` +
   `web_fetch`, the `critic` leaf independently performed its own successful
   `web_search`/`web_fetch` (its own validity condition, not waived), and a
   real cited Markdown report landed in the artifact registry.
3. **Tamper-proof matchers:** every matcher in the test file reads structured
   evidence (tool call name/args/output/error, the artifact registry, the
   child session's own `agent.id`) and would fail a tampered/hollow run —
   never synthesis prose.
4. **No regressions:** the other cases' suites still pass; this case's files
   are additive and scoped to `benchmark/case14-deep-researcher-web/` +
   `tests/test_real_cases/test_deep_researcher_case.py`.

**Stretch bar (only if the owner elevates this to a full grind post-landing,
matching the other cases' template — NOT claimed as met by this scaffolding
slice):**

5. Reliability: `>=0.8` over `>=10` sampled live runs on the guardrail cell;
   report the Wilson interval, lower bound `>=0.6`.
6. Generalization: the same bar across `>=3` distinct research prompts/topics
   (mutated `prompt.txt` variants), proving no topic-specific dependence.
7. Suite grew from review: every distinct failure found reading a trace is
   encoded as a new matcher.

## Status

- r0 (this slice, `slice/c1-s6-deep-researcher-case`): case scaffolded.
  `benchmark/case14-deep-researcher-web/{GOAL.md,README.md,prompt.txt}` +
  `tests/test_real_cases/test_deep_researcher_case.py` written; collection
  verified clean (zero import errors, tests skip/deselect) with
  `CLIO_RUN_LIVE` unset. **No live turn has been run** — that is deliberately
  deferred to the campaign's own C1-S6 gate, once C1-S2..S5 land on
  `feat/mcp-client-unification`. Blocked-by: C1-S2..S5 (the generic MCP v2
  client + web MCP `task=required` conformance legs (i)/(ii) of #1286)
  landing on the campaign branch.
