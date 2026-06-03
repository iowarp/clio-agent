# Case 08: NDP Seismic Waveform To SAC Analysis And PNG

Status: not yet passed in this case folder.

Issue checklist entry: `iowarp/clio-agent#628`.

Historical evidence to migrate or supersede:

- `../MARKETPLACE_SEISMIC_REPORT.md`
- `../NDP_WAVEFORM_RECOVERY_REPORT.md`

## Benchmark Prompt Intent

Ask CLIO to discover a bounded seismic waveform dataset, obtain usable data,
analyze waveform/SAC content, and produce a plot artifact. The prompt must not
name NDP, SAC, or plotting internals.

## Expected Agent Blueprint

Primary pack: `seismic-waveform-review`, or a later researched replacement
pack with a reusable NDP collector subtree.

## Semantics To Prove

- Orchestrator delegates to the owning data/seismic parent, not directly to a
  leaf NDP expert.
- NDP discovery and bounded staging or parent-owned recovery.
- SAC/waveform analysis after data is available.
- Visualization produces a verified PNG artifact.
- Sync delegation returns to the immediate parent at every step.

## Required Folder Evidence

Add the live run evidence required by `../CASE_EVIDENCE_CONTRACT.md` before
checking this case off. Historical reports are not enough until this folder has
the case-local trace and output artifacts.
