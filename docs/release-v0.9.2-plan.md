# CLIO v0.9.2 release plan

Status: release preparation in progress. Do not tag clio-agent until every gate below is complete.

## Integrated pre-release tips

| Component | Candidate branch | Required release | Integrated tip |
|---|---|---|---|
| clio-schemas | `main` | v0.2.3 | `f99450067e498de25c28764e15ecad1a88b7d595` |
| Marketplace | `main` via PRs 65-68 | v0.6.3 | `fac72f0670607a08871a369040acd867e642ebec` |
| GACT UI | `develop` through PR 400 | v0.11.0 | `1629ee736da35adf0c66260f0435922ce879b083` |
| clio-agent | `codex/v0.9.2-final-qualification` | v0.9.2 | final pointer and evidence commit in progress |

These dependency commits are the frozen pre-release inputs. The backend pins these exact commits
before live testing; after the gates pass, the dependency tags must be created on these same
commits so publication does not change the tested graph.

## Stacked release order

1. **Complete:** merge clio-schemas v0.2.3 to `main` at `f9945006`; after the live gates
   pass, tag and publish it before testing the registry-backed clio-agent install.
2. **Complete:** merge the marketplace v0.6.3 candidate to `main` at `fac72f06`, including
   Daisy Quach's Factorio Flat materials-science taxonomy; do not tag or publish yet.
3. **Complete:** merge the GACT v0.11.0 candidate to `develop` at `1629ee73`; do not tag,
   publish, or advance `main` yet.
4. Pin the exact dependency commits in clio-agent, bump every version pointer to 0.9.2,
   roll the changelog, and advance clio-agent `develop` to that coherent candidate.
5. Run the release skill's static, build, metadata, memory-budget, and install-path gates,
   then run the full live matrix against those exact three commits.
6. If and only if every gate passes, publish clio-schemas v0.2.3, then tag marketplace v0.6.3 at `fac72f06`; fast-forward GACT
   `main` to the already-tested `1629ee73` and tag v0.11.0 there; publish and verify its assets.
7. Confirm the backend gitlinks still match those tags, fast-forward clio-agent `main` to the
   already-tested `develop` commit, create v0.9.2, publish curated notes, and verify PyPI,
   all release assets, and all three GHCR images.

## Feature-presence audit

The scoped 2026-08-30 through 2026-09-13 reconciliation is recorded in
`docs/release-v0.9.2-feature-reconciliation.md`. Its feature-convergence gate passes; live,
version, packaging, and publication gates remain separate.

- The clio-agent stack PRs 1314, 1329, 1330, 1335, 1331, 1338, and 1340 are all ancestors of
  def21cfb. PR 1332 was carried by PR 1335 and is also present.
- The GACT stack PRs 389, 390, 391, 392, and 393 are all present beneath the integrated
  v0.11.0 candidate at `20edc765`.
- The older GACT external-provenance branch targeted the removed apps/web tree. Its behavior was
  deliberately re-landed as backend-owned presentation blocks in clio-agent PR 1338 and rendered
  by the current generic GACT presentation path.
- The current goal implementation retains the explicit deletion of the deterministic
  model-authored completion predicate. The bounded LLM judge remains the sole semantic completion
  decision; hard loop bounds remain enforcement. This must be rechecked by the focused goal tests
  on the release candidate.
- feat/case13-live-verification-harvest predates the scoped two-week audit and is not a v0.9.2
  release input. It is neither evidence for nor a blocker to this release gate.

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
- Before live testing, the GACT and marketplace gitlinks must resolve to the frozen integrated
  commits above. Before publication, the release tags must resolve to those same commits.
- The GACT v0.11.0 tag must have a matching CHANGELOG.md heading.
- The clio-agent release completeness job must confirm every installer, TUI binary, desktop
  bundle, and web archive; PyPI and GHCR must be verified independently.

## Known non-release follow-ups

The following are real residuals but are tracked separately from the release mechanics: the
semantic-event sibling reader's cross-chunk concurrent ordering exposure, richer live-probe
exception/timestamp evidence, and the Windows CTE teardown crash 0xC0000409. Their release-blocking
status must be decided explicitly before the final tag.
