# case13 — HPC Cluster Operator

**The question family:** can an agent, given only human-level scientific
intents and a set of skills, operate a real HPC cluster — discover what is
installed, set up what is missing, compose and run the right pipeline
(bare application / I/O-intercepted / composed with a visualization stage),
drive the work asynchronously to terminal, and answer with evidence read
from the real output artifacts?

**Why it matters:** this case IS the L3 acceptance of the clio-relay ladder
(clio-relay is the primary use-and-test case of clio-agent's MCP v2 task
support), rebuilt as a durable grind so the same case re-runs at
security-on and on the cut release. The vivid output is scenario S3:
per-frame simulation visualizations produced ON the cluster and returned
through the relay artifact channel with provenance.

**Substrate:** the ares cluster (ares.ares.local) through clio-relay —
self-bootstrapped by the relay itself (#158 pathway), dev-mode
loud-proceed regime during the grind. The door serves 12 relay_* tools
(MCP, protocol 2026-07-28, task-capable); clio-agent's native
JarvisJobs/federation surfaces ride it.

**Scenarios:** see `scenarios.md` (S1 capability / S2 instrumentation /
S3 visualization / S4 honest negative). Prompts name no tools, experts, or
relay concepts.

**Manual solution:** the ares matrix mission's evidence trail is the
by-hand proof the questions are answerable on this substrate — relay
self-bootstrap to ready, a real `spack install` through the relay verified
by `spack find` on the host, jarvis executing under dev mode, per-step
evidence under D:/relay-p5local/evidence/ and on iowarp/clio-relay#158 and
#242. The three-workload matrix (LAMMPS; app+Darshan; composed pipeline
with per-frame images) is the mission's step 4 and maps 1:1 onto S1-S3.

## Semantics To Prove

- A human-level scientific intent (no tool, expert, or relay vocabulary in
  the prompt) is enough for the agent to discover installed software, install
  what is missing through Spack, compose the right JARVIS pipeline, and
  submit it through the relay's MCP v2 task surface.
- Async discipline holds end-to-end: submit → wait/poll to a REAL terminal
  state → retrieve artifacts; the agent reports only states and values it
  read back from the cluster (thermo tables, Darshan logs, per-frame images
  through the artifact channel), never fabricated progress.
- The instrumented (S2) and composed (S3) variants exercise interceptor
  binding and multi-stage pipelines with provenance-carrying artifacts; the
  honest negative (S4) returns "nothing ran" truthfully against a clean
  deployment.

## Current Core Problem

The grind has not yet passed its reliability bar (GOAL.md: ≥0.8 over ≥10
sampled runs per scenario). The by-hand ares mission proved the substrate
answers the questions (see Manual solution above), and the 2026-08-20/21
Darshan mission proved S1/S2 shapes live agent-driven — but the harnessed
scenario family (S1–S4, story-asserted matchers, tamper-proofs) still needs
its sampled live runs on the isolated :17970 instance.

**Status:** not passed. Grind contract in `GOAL.md`; run logs in `runs/`.
