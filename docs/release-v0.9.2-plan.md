# CLIO v0.9.2 release plan

Status: release preparation in progress. Do not tag clio-agent until every gate below is complete.

## Candidate source tips

| Component | Candidate branch | Required release | Source tip |
|---|---|---|---|
| Marketplace | codex/release-0.6.3 | v0.6.3 | 0fa12654e2236c52a61f22e8c92c699afcda72c7 |
| GACT UI | codex/release-0.11.0 | v0.11.0 | 91c6e232a3a693dfccaca740b7879630dd783a51 |
| clio-agent | codex/release-0.9.2 | v0.9.2 | def21cfbacd3e80bd0e8b6a3aaae5a859ed0b6a7 |

The two dependency releases must exist before the clio-agent release commit pins their tags.

## Stacked release order

1. Prepare marketplace v0.6.3 notes, run its policy suite, merge its release branch to main,
   tag the exact merge commit, and publish the GitHub release.
2. Prepare the GACT v0.11.0 changelog, run CI and the live browser acceptance matrix, merge
   its release branch to main, tag the exact merge commit, and verify the binary release assets.
3. Update both clio-agent submodule gitlinks to those released tag commits.
4. Bump every clio-agent version pointer to 0.9.2 and roll CHANGELOG.md.
5. Run the release skill's local build, metadata, memory-budget, and install-path gates.
6. Run the final live matrix against the exact clio-agent and GACT release candidates.
7. Merge the clio-agent release branch through develop to main, create v0.9.2, publish curated
   notes, and verify PyPI, all release assets, and all three GHCR images.

## Feature-presence audit

- The clio-agent stack PRs 1314, 1329, 1330, 1335, 1331, 1338, and 1340 are all ancestors of
  def21cfb. PR 1332 was carried by PR 1335 and is also present.
- The GACT stack PRs 389, 390, 391, 392, and 393 are all present at 91c6e232.
- The older GACT external-provenance branch targeted the removed apps/web tree. Its behavior was
  deliberately re-landed as backend-owned presentation blocks in clio-agent PR 1338 and rendered
  by the current generic GACT presentation path.
- The current goal implementation retains the explicit deletion of the deterministic
  model-authored completion predicate. The bounded LLM judge remains the sole semantic completion
  decision; hard loop bounds remain enforcement. This must be rechecked by the focused goal tests
  on the release candidate.
- feat/case13-live-verification-harvest is not in develop. It contains an unreviewed preservation
  commit with both benchmark material and production changes. It is not silently part of v0.9.2;
  it requires an independent decision and validation before inclusion.

## Required live gates

- Re-run the canonical tool-presentation scenarios in scripts/live_verification/tool_presentation
  against the exact release candidates. Preserve session IDs and outcomes.
- Exercise both supported providers through repeated compaction, restart, and cold transcript
  reconstruction. Verify the complete human transcript remains available after compaction while
  model materialization starts at the latest checkpoint.
- Exercise A2UI interaction and action submission live.
- Exercise an MCP App resource live. The September tool-presentation campaign did not do this;
  automated tests alone do not satisfy this gate.
- Verify session reload progressively reveals the shell and messages rather than blocking the
  entire UI behind one loading state. Record cold and warm observations.
- Run the release memory-budget command with three sessions and the required 180-second settle.
- Verify all six documented installation experiences after publishing.

## Required static and packaging gates

- All required CI checks must pass at the exact marketplace, GACT, and clio-agent candidate heads.
- No required check may be skipped, cancelled, pending, or inferred from another commit.
- uv version, pyproject.toml, clio_agent.__version__, uv.lock, install documentation, and the
  release policy test must all report 0.9.2.
- Both wheel and sdist must be below 100 MB and wheel metadata must contain no git dependency.
- The GACT and marketplace gitlinks must resolve to their released tags, not merely branch heads.
- The GACT v0.11.0 tag must have a matching CHANGELOG.md heading.
- The clio-agent release completeness job must confirm every installer, TUI binary, desktop
  bundle, and web archive; PyPI and GHCR must be verified independently.

## Known non-release follow-ups

The following are real residuals but are tracked separately from the release mechanics: the
semantic-event sibling reader's cross-chunk concurrent ordering exposure, richer live-probe
exception/timestamp evidence, and the Windows CTE teardown crash 0xC0000409. Their release-blocking
status must be decided explicitly before the final tag.

