# CLIO v0.9.2 live qualification

This directory is the reproducible recipe for the release-blocking v0.9.2 live
campaign. Historical qualification sessions, including Case 13, are examples
only and are not evidence for this release.

## Frozen inputs

- clio-agent base: `dc9268576d1d46b5fb2761d7f014642debeb6a75`
- gact-tui: `20edc76538d382c866acb3c0cfb29cd8889926ea`
- clio-agent-marketplace: `067631a44ce40fe1fa90764427a3b8e4e0229852`
- provider/model: `codex` / `gpt-5.6-luna`
- target versions: clio-agent 0.9.2, gact-tui 0.11.0, marketplace 0.6.3

The exact clio-agent commit under test is written at runtime to
`out/live-verification/release_v0_9_2/manifest.json`; it includes this campaign
package commit. Evidence is kept out of the tracked tree so collecting it does
not change the candidate.

## Decision rule

A visible scenario passes only when its backend evidence and Browser screenshots
agree. Any functional failure blocks publication. After a fix, update the
candidate manifest and rerun only the affected live gates. GitHub owns the full
unit fleet; local verification is limited to focused tests and release packaging.

Pre-release scope is Codex-only. EarthScope Skills and Factorio Flat are the
blocking scientific cases. Claude-provider parity and SPOTTER are post-release.

See [runbook.md](runbook.md), [prompts.md](prompts.md), and
[coverage.md](coverage.md). Run `scripts/preflight.py` before starting any live
session and `scripts/package_evidence.py` only after the verdict is complete.
