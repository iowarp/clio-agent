# case07 — HPC Cluster Operator

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

**Status:** grind contract in `GOAL.md`; run logs in `runs/`.
