# CLIO v0.9.2 feature reconciliation

Audit date: 2026-09-13  
Scope: product work committed or modified from 2026-08-30 through 2026-09-13  
Gate: **PASS — feature convergence only**

This is a read-only reconciliation of the release candidates against recent branches,
worktrees, merged pull requests, and explicit feature removals. It does not replace the
live-runtime, packaging, version, or publication gates in
`docs/release-v0.9.2-plan.md`.

## Candidate lineage

| Component | Audited source | Candidate branch | Result |
|---|---|---|---|
| clio-agent | `def21cfbacd3e80bd0e8b6a3aaae5a859ed0b6a7` | `codex/release-0.9.2` | Recent stack PRs 1314, 1329, 1330, 1331, 1335, 1338, and 1340 are present; PR 1332 is carried by 1335. |
| GACT UI | `20edc76538d382c866acb3c0cfb29cd8889926ea` | `develop` after PR 394 | Recent UI PRs 389 through 393 are present, including the restacked PR 391 work, plus the v0.11.0 release record. |
| Marketplace | `067631a44ce40fe1fa90764427a3b8e4e0229852` | `main` after PR 65 | Recent base-agent and pack changes are present, including Daisy Quach's Factorio Flat materials-science taxonomy at `1defadf`. |

The GACT release branch adds release documentation only. The marketplace release branch also
integrates Daisy Quach's six-specialist materials-science expansion while preserving her original
commit in `main` ancestry.

## Recent branch reconciliation

Not every local branch name is an ancestor of the candidate: several changes were rebased,
restacked, or deliberately dropped. The relevant question is whether an intended product
change survives in the candidate.

| Branch or worktree | Disposition |
|---|---|
| `contained/night-backend` | Its sole artifact-lineage commit `0b2ab007` is patch-equivalent to `develop` commit `5f8d228e`. |
| `codex/bring-codex-image-back` | All three commits are patch-equivalent to commits on `develop`; image, interrupt, and diagnostic behavior was re-landed on the SDK-native path. |
| `codex/campaign-composer-integration` | Product slices were ported through PRs 1278 and 1298. The stale Codex app-server transport, duplicate capability contract, answer framing, and direct-response plumbing were intentionally not restored. |
| `backup/provider-runtime-campaign-*` | Provider catalog, generic reasoning/images, request hardening, HPC provenance, stabilization, and process census were restacked through PR 1329. Duplicate pytest-runtime ownership and repairer-specific routing were intentionally dropped. |
| GACT `codex/provider-runtime-ui` | Restacked equivalents are present in merged PR 392. |
| GACT `codex/provider-runtime-provenance` | The deleted `apps/web` implementation was superseded by backend-owned generic presentation blocks in PR 1338 and the current generic renderer. |
| Marketplace `codex/base-agent-direct-response` | Re-landed on `main` as `11311fc`; the base agent still declares answer-only behavior with `workflow_state: false`. |
| Old tool-presentation worktrees | Their product work begins at `4fc5d22c` and is followed by the merged artifact, workflow, resource, memory, A2UI, Gantt, and progressive-hydration refinements. No unique product behavior remains only in the dirty copies. |

Branches last changed before 2026-08-30, including the Case 13 harvest, are outside this
audit. They were not used as evidence for passing this gate.

## Dirty-worktree reconciliation

- The preserved local documentation was committed under `docs/review`; no product source change
  from the scoped period remains only in the primary checkout.
- The old MCP v2 UI-contract worktree's four working blobs equal the candidate blobs.
- The current GACT development checkout contains stale local dev scripts, generated build
  state, completed research copies, and one proposed/unapproved A2UI campaign document.
  Applying the stale scripts would remove newer safety and ownership behavior; they are not
  missing release features.
- The detached tool-presentation validation tree is composed of candidate or candidate-history
  blobs. Its one non-identical test was expanded, not removed, in the candidate.

No product source change from the scoped period was found exclusively in an uncommitted
worktree.

## Explicit-removal audit

| Removed behavior | Candidate evidence | Result |
|---|---|---|
| Deterministic model-authored goal predicate | `goal.py` retains the bounded LLM judge as the sole semantic completion authority. Goal, skill-effects, and composed-governance tests reject `predicate` / `predicate_backed` vocabulary. | Still removed |
| Hidden direct-ReAct repair and forced submit | `reactv2.py` performs no post-loop model call. `test_reactv2_repair.py` asserts the former forced-submit and bounded-repair methods do not exist. | Still removed |
| Old Codex app-server transport | The old transport modules and symbols are absent from production code; current SDK-native transport carries the needed behavior. | Still removed |
| Duplicate compaction archive and ARC conversation write | `transcript_trace.py`, `compact_memory.py`, and the `session_archives` ledger are absent. Compaction keeps raw message atoms, checkpoints materialization, and drops complete lane families when replacing them. | Still removed |
| Hand-coded capability contract | The obsolete route is absent; the live capability projection remains. | Still removed |

No recent `Revert` commit was found on clio-agent `develop`, and the scoped removed-symbol
sweep found no production reintroduction. Workflow `when_state` remains intentionally present
and is not the removed goal predicate.

## Live-verification campaign custody

The release branch preserves the September tool-presentation campaign under
`scripts/live_verification/tool_presentation/`:

- both exact workspace-local agent blueprints and all six expert definitions;
- both skill definitions used by the qualification workflow;
- replay prompts;
- resource-custody, file-lifecycle, quality-gate, and cross-file-triage fixtures; and
- a README that records provenance and known coverage gaps.

This archive is sufficient to replay the campaign. It does not itself prove the release
candidate live. A2UI, MCP Apps, compaction/restart, provider parity, and progressive session
hydration remain in the separate live gate.

## Gate verdict

For the two-week scope, all intended product features examined are either present in the
candidate tips or explicitly documented as superseded/dropped; all examined removals remain
removed. No recent product feature was found stranded in a dirty worktree or local-only branch.

Therefore the **feature-convergence gate passes**. The release is not yet release-ready: the
live-runtime gate and the version/package/publication gate remain open.
