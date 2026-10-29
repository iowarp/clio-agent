# Goal: CLIO ready for the Polaris OPAL + attention run (set 2026-10-26)

Owner: Jaime Cernuda. Work branch: `claude/epic-hopper-6s2vhn` in iowarp/clio-agent,
iowarp/gact-tui, iowarp/clio-schemas and iowarp/clio-agent-marketplace (submodules pinned to
each other). No merge, tag or release; nothing pushed to spotter-ai-genesis repos.

## Outcome

A Polaris agent takes this branch plus `HANDOFF.md` (this directory) and, without asking
anyone, on a PBS compute node:

1. lets **CLIO deploy** Flowcept natively (Apptainer only as fallback; Polaris has no Docker),
   then vLLM with the attention connector, then serves CLIO + the web UI;
2. creates an OPAL workspace/session with the **SPOTTER AI** agent;
3. runs OPAL: Bogdan's chain(s) from `opal_chains.csv` round by round through the session,
   then the broad SPOTTER investigation over the staged OPAL export;
4. opens the attention view, which follows **Bogdan's semantics**.

Model, in two stages (decided 2026-10-26): **stage 1, IBM Granite 4.2-8B on one A100** (no tensor
parallelism; same chat template and tokenizer as the 30B); **stage 2, Granite 4.2-30B on two A100s**
only after stage 1 passes. The connector has never run under tensor parallelism on CUDA: one attention
turn is the first check of each stage.

History is the model's own answers (no teacher forcing). Teacher forcing does not change the
attention formula.

## Done means

- **A. Bogdan's attention semantics** (spotter-ai-genesis/experiments
  `bench/opal/analytics/attn_analytics.py` @ `ebe17be` is the spec):
  - default profile = his formula `score[j] = Σ_i 0.5^i · val_all_max[step_i][j]`
    (unnormalized weights, item's first step weighted highest, unretained = 0);
  - one vector per answer item (separate views, not a union);
  - item columns over the whole rendered prompt, per line (max over the line's tokens),
    each column scaled to its own max, 10-level ramp;
  - connector `NO_ENTRY` (-1) slots dropped instead of refusing the capture;
  - export with his CSV columns;
  - parity test: CLIO's per-item vector == his `aggregate()` on a real capture file.
- **B. OPAL driver**: chain rounds from `opal_chains.csv` and the broad prompts, resumable
  receipts.
- **C. Polaris kit**: PBS launcher, OPAL run script, connector build fix, model download,
  ALCF proxy, project-storage paths with inode check, teardown. Syntax-checked and dry-run
  here as far as possible without a GPU.
- **D. Live check in the cloud session** (no GPU, so no capture): CLIO on `claude_code`,
  Flowcept deployed by CLIO natively and with Docker, SPOTTER AI installed and armed, SPOTTER
  on two or three plain non-OPAL requests with provenance visible in Flowcept, UI screenshots;
  attention UI checked against the real capture fixture.
- **E. Morning report** `REPORT.md`: verified here vs. Polaris-only, open risks.
