# Goal: HPC Cluster Operator (case13)

## Objective

Turn the **grounded** question family "can an agent operate a real HPC
cluster from human-level scientific intents" into a working CLIO Agent
Blueprint case that runs through the normal orchestrator against the LIVE
ares cluster via clio-relay (MCP v2 task semantics), produces the vivid
artifact (per-frame simulation visualizations returned through the relay
artifact channel) plus grounded numeric answers, and is accepted only after
trace + artifact + provenance review. This case IS the L3 acceptance of the
relay ladder, rebuilt as a durable grind: the same case re-runs at
security-on and on the cut release as the final acceptance.

The case prompts are natural and name no expert, tool, relay concept, or
schema: see `prompt.txt` (S1) and `scenarios.md` (S1-S4).

Grounding that already exists (this is the build-and-prove phase):
- The infrastructure legs are proven live by the ares matrix mission
  (relay self-bootstrap; spack install through the relay verified via
  `spack find`; jarvis executing under dev-mode loud-proceed; evidence on
  iowarp/clio-relay#158/#242 and D:/relay-p5local/evidence/). That evidence
  is this case's `manual-solution/` equivalent — the by-hand proof the
  question is answerable.
- The client v2 stack is proven (L3-local: task envelope, tasks/get to
  terminal, durable record, zero suppression).

## Case-specific deviations from the template (deliberate, owner-aligned)

- **Provider/guardrail cell: `claude_code` / `sonnet`** (not argonne_metis).
  Live multi-session tests ride subscription providers on this box (memory
  rule), sonnet is the owner's cost-policy default, and this case tests the
  productized deployment path.
- **The live substrate is the ares cluster through clio-relay**, not the NDP
  catalog. Freshness/liveness gate = the relay session is `ready` and the
  worker serves (checked per run, typed).
- **Dev-mode loud-proceed is the grind regime**: deferred-enforcement
  records are expected and informational. The security-on re-run of THIS
  case (unchanged prompts/matchers) is a later phase's acceptance, then the
  release re-test.
- **The domain tools are NOT a new MCP**: the relay door IS the MCP surface
  (12 relay_* tools; clio-agent's native JarvisJobs/federation surfaces ride
  it generically). Building a per-case server would violate non-negotiable
  #8; nothing new belongs in core.

## Non-negotiable method (do not regress on these)

- **No gates, no fake data, no mocks in `src/`.** Acceptance = a real live
  session whose trace, evidence, artifacts, and provenance are inspected and
  match the prompt intent. A passing counter is not a pass.
- **Decisions live in the agent's reasoning; code only runs tools and
  records their data.** The agent decides to install missing software,
  chooses the pipeline shape (bare / interceptor / composed+render), and
  reads artifacts to answer. No runtime heuristic, no env var force-feeding
  the cluster/app/answer, no harness pre-registration of ad-hoc agents.
- **Routing is DSPy-typed, not string-matching.** Handoffs ride typed
  workflow state over tool-produced data; must generalize across S1-S4 with
  zero scenario-specific string contracts.
- **Difficulty is intrinsic** (live-state decisions + async task discipline
  + artifact-grounded answers). The boring outcomes (already installed;
  nothing found) are correct, reachable answers.
- **Work in the session workspace** — frames and outputs land as workspace
  artifacts with lineage, never stray paths.
- **v2 or fail**: a run whose answer arrived through v1-synchronous shapes
  fails regardless of correctness (`mcp_tasks_declaration_suppressed` must
  be absent; the task envelope + terminal via tasks/get must be present).
- **No invention**: any terminal tool failure the agent met must be
  reflected honestly in the answer (matcher-enforced), and recovery via the
  actionable-error vocabulary is the expected behavior, not a bonus.

## Testing harness (agent-test)

`~/agent-test` is the run + acceptance harness; division of labor per the
skill (agent-test = data-pathway monitor; the grinder reads every accepted
trace as the semantic monitor; every trace-read failure becomes a matcher).

Matcher plan (structured evidence only, each proven to FAIL a tampered run):
- door-side: the submitted job(s) reached terminal `succeeded` (relay job
  status re-queried by the harness), a v2 task envelope existed.
- serve-side: zero `mcp_tasks_declaration_suppressed`; the durable task
  record exists (or the #1223 typed degradation is present and the run is
  marked accordingly — never silent).
- artifacts: expected artifact class exists in the workspace with lineage
  (S3: >= N frame images); matcher re-reads the artifact.
- answer grounding: the final message's reported numbers appear in the
  actual output artifact (re-extracted by the matcher, not prose-matched).
- honesty: terminal failures acknowledged; S4 agrees with the real listing.
- anti-force-feeding: the run's inputs are exactly {task, blueprint_id};
  matcher rejects `{{`-template literals and None-shaped state.

## Priority order

1. **Pack**: adopt the marketplace `cluster-operator` pack (in flight from
   its builder) as the case blueprint; refine per grind findings. Install
   workspace-scoped in the isolated instance. Topology: domain-grouped,
   re-entrant (main re-decides on typed state after cluster/pipeline
   returns) — not a linear chain.
2. **Harness**: `tests/test_real_cases/test_case13_cluster_operator.py`
   mirroring the earthscope case; SUT drives the isolated serve.
3. **Wiring validation fast, then ONE live S1 run** on the guardrail cell;
   trace to `runs/`; hand review.
4. **Grind r1..rN** per the loop: one unit, live run, trace read, fix the
   real thing, encode the failure as a matcher, commit with the measured
   rate, update Status.
5. **Finalize**: README as the accepted contract; runs/ holds the logs.

## Isolation (parallel-clio)

- Worktree: `D:/clio-cluster-case` branch `feat/cluster-operator-case`.
- Port 17970; `CLIO_DATA_DIR`, `CLIO_ALLOWED_ROOTS`, artifacts root all
  under the worktree. The serve on :17900 (ares mission) is NOT touched.
- The ares cluster is shared live infra (like ALCF): concurrent use is
  fine; mind capacity (small runs, minutes not hours).
- Relay env for the isolated serve: door :18795 + the p5local token + a
  fresh owned-session identity (documented in runs/ENV.md when minted;
  config-over-env where the config surface allows).

## Done criteria (hard — ALL must hold)

Guardrail cell `claude_code`/`sonnet`. Every accepted run's trace read by
the grinder with a one-line verdict recorded.

1. **Reliability**: S1 passes live at >=0.8 over >=10 sampled runs; Wilson
   lower bound >=0.6.
2. **Generalization**: the same bar for S2 and S3 (>=3 scenarios total).
3. **Honest negative**: S4 at >=0.8 over >=10 with the dedicated matcher.
4. **Tamper-proof matchers**: every matcher proven offline to FAIL a
   tampered run.
5. **Suite grew from review**: every trace-read failure is encoded.
6. **No regressions**: existing case suites still pass; changes additive.
7. **(case13-specific) v2 discipline held in every accepted run** and every
   degradation observed was typed and loud (inventory kept for the
   security-phase re-run).

## Status

- r0: case authored (prompts, scenarios, GOAL). S2 is BLOCKED-BY
  clio-kit#376: the jarvis MCP lost the interceptor target-binding
  special case (add_step cannot express what Darshan wraps); fix =
  clio-kit handler + contract v3.7.1 (first patch-level contract).
  Grind S1/S3/S4 first; S2 enters rotation when #376 lands. Prerequisites in flight:
  the marketplace cluster-operator pack (builder agent) and the ares matrix
  legs (steps 1-5 of the mission). Harness skeleton next; first live run
  after the pack lands.
