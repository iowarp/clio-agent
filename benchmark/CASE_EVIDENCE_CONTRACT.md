# Case Evidence Contract

Each `caseXX-*` folder must contain the evidence below before the case can be
marked passed.

Required files:

- `prompt.txt`: exact public prompt used in the live run.
- `run.md`: provider, model, date, CLIO commit, marketplace commit, command,
  environment notes, and any operator interventions.
- `trace.jsonl`: durable semantic trace for the run.
- `report.md`: audited result note with pass/fail decision and links to
  artifacts.
- `artifacts/`: generated files or downloaded/staged evidence needed to verify
  the answer. Use a short manifest if artifacts are too large to commit.

Required audit checks:

- The active agent was a marketplace Agent Blueprint.
- The route graph matched the intended hierarchy.
- Tool calls included arguments, results, durations, and error metadata.
- The final answer cited the same evidence present in the trace.
- Artifacts existed on disk and matched the claimed source/provenance.
- Failures returned to the owning parent expert and did not produce invented
  downstream artifacts.
- Any benchmark-specific shortcut or hardcoded hint caused the case to fail.

Case folders may contain `NOTES.md` while being developed, but notes are not a
substitute for live evidence.
