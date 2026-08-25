# case13 scenario family

Each scenario is a natural scientific intent. None names an expert, a tool,
a relay concept, or a contract. The SAME pack must pass all of them.

## S1 — capability (prompt.txt)

Run a short LAMMPS melt on the cluster; report the final thermodynamic state
(temperature, energy, pressure). Setup-if-missing is part of the ask.

Story acceptance: the job reached a terminal SUCCESS on the cluster through
the v2 task flow; the reported numbers appear in the actual output artifact;
if software was missing, the transcript shows the agent deciding to install
it (not the harness).

## S2 — instrumentation

> "That simulation you can run — I care about its I/O behavior on the
> cluster's filesystem. Profile the I/O it does and summarize what you find:
> how much is read vs written, and where the hotspots are."

Story acceptance: a run executed WITH an I/O interceptor (Darshan) through
the pipeline; the summary's numbers trace to the interceptor's log artifact;
no profiling claim without the artifact.

## S3 — visualization (the vivid one)

> "Show me the simulation evolving over time — I want to actually see it,
> frame by frame, not just numbers."

Story acceptance: a composed pipeline ran (simulation stage then an
image-producing stage); multiple per-frame images arrived back as workspace
artifacts with lineage tracing to the cluster run; the final answer
references/presents them.

## S4 — honest negative

> "Before we run anything new: did any of my earlier simulation runs leave
> results on the cluster? If there's anything there, summarize it; if not,
> just say so."

Against a deployment with no prior case13 runs, the correct answer is
"nothing found" — verifiable against the cluster's job/artifact listing. No
forced artifact, no invented history. (When prior runs DO exist from earlier
scenarios, the correct answer flips to a real summary — the matcher checks
agreement with the listing, not a hardcoded "no".)

## Difficulty note (intrinsic)

The hard part is decision-making over live cluster state: discovering what
is installed, choosing to install what is missing, composing the right
pipeline shape per intent (bare / interceptor / composed+render), driving
async v2 tasks to terminal without inventing results, and reading real
output artifacts to answer. None of that is parsing trickery, and every
scenario's "boring" branch (already installed / nothing found) is a correct,
reachable answer.
