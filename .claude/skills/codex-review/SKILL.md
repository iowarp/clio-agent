---
name: codex-review
description: Merge-gate review method for Codex-produced branches across the CLIO repos (clio-agent, gact-tui, marketplace, clio-kit) — the process, the per-repo checklists, and the catalog of Codex's known failure signatures. Load when asked to review Codex work, a codex/* branch, or a PR produced by the Codex executor, before any merge verdict.
---

# Reviewing Codex work (CLIO repos)

Distilled from the 2026-08 A2UI/v0.3 landing campaign (clio-agent #1255, gact-tui #380): an
adversarially-verified review of ~30k changed lines that confirmed 92 real defects, refuted ~60
plausible-but-wrong claims, and found Codex's claimed blocker fixes 13/15 genuinely fixed.

## Ground rules of the review itself

1. **Exact-head discipline.** Review a named SHA; every verdict names it. Claims about "the branch"
   without a SHA are not evidence. Re-review after any push.
2. **Three-dot scope.** `git diff origin/<target>...HEAD` is the branch's own content; develop-syncs
   absorbed into the branch are already-reviewed material — do not re-review them, do check the
   merge resolutions (gitlink pins especially).
3. **Adversarial verification before any fix.** Every reviewer finding gets an independent verifier
   prompted to REFUTE it from the code at head. Expect a ~40% refutation rate; a finding that
   skipped verification is not a finding. Cost tiers: reviewers/verifiers on opus, orchestration
   only on the top model.
4. **Blocker lane is trust-but-verify.** For every "fixed as requested" claim: the named test must
   exist, assert the actual contract (read the body — renamed lookalikes and weaker assertions count
   as PARTIAL), and the fix must cover the SIBLING paths, not just the named instance.
5. **CI green is an input, not a verdict.** Never report what CI already proves (style, types).
   Hunt semantics: wrong behavior, data loss, races, fail-open, contract violations.
6. **One tree, one writer.** Reviewers are read-only; a second repo gets a detached worktree.
   Fix agents run SEQUENTIALLY per tree, with failing-first tests and a dispute valve (a wrong
   finding gets disputed with evidence, never silently "fixed").
7. **The verdict is an artifact**: an explicit reviewer comment on the PR at the exact head —
   blockers, majors (fix in-campaign), minors, disputed items. No comment, no approval.
8. **Qualification is validation, not discovery.** Live cases run ONCE cleanly at the exact heads;
   judge sensibly. Re-grinding many session versions over semantic nitpicks is expressly wrong
   (owner ruling 2026-08-31).
9. **Companion-repo sweep BEFORE scoping.** Codex handoff summaries understate scope: the 2026-09
   composer wave was handed off as "all UI, one PR" and actually spanned FIVE repos (an unmentioned
   +12k-line clio-agent server PR the UI's contract ledger depended on, a web-search PR, and
   marketplace/kit branches). Before scoping any review: (a) list the local worktree dirs for
   same-named siblings (`ls -d /d/Libraries/Documents/projects/*<branch-suffix>*`), (b) check every
   CLIO repo for a same-named `codex/*` branch and open PRs (`gh pr list` + `git branch -r`), (c)
   verify the handoff's claims against CI at the exact head yourself — "checks running
   asynchronously" has meant "already red", "verified locally" has meant "the suite was never run",
   and headline features have shipped with no implementation behind them. Review the WAVE, order
   the merges by contract dependency (server before the client that grades its events
   `implemented`), and pin gitlinks only to merged-branch SHAs.

## Codex failure signatures (what to hunt, in priority order)

1. **Pattern applied once, not carried to siblings.** The single most reliable defect: the fix
   lands exactly where the review pointed and misses the sibling paths (preserve-on-compact fixed;
   preserve-on-clear-context and on-message-delete missed). For every fix, enumerate the sibling
   call sites yourself and check each.
2. **Overbuild.** Dead typed vocabulary (an SSE event no consumer reads; an unreachable typed
   degradation branch), speculative config/hooks, duplicated mechanisms (a hand-rolled twin of an
   existing normalizer that then diverges), no-op shims left alive after a deletion. Flag for
   deletion, not wiring-up.
3. **Prose/keyword heuristics.** Classification by error-message substrings (watch for captured
   debugging literals frozen into code, e.g. a specific `pool_id 512.0`), client regex over server
   prose, scraping `status`/`state` keys out of arbitrary payloads and overriding the server's own
   state. Banned in clio-agent core (ground rule) and in gact-tui (server owns semantics).
4. **Fail-open and sticky state.** Safety barriers that pass when their watcher is missing/dead;
   health latches set on a transient failure and never cleared by recovery; optimistic-green
   fallthroughs in state mappings (`default: 'completed'`).
5. **Test theater.** Renamed lookalikes of a demanded test; self-confirming fixtures that validate
   the heuristic that generated them; observation before settlement in async flows; a "bounding"
   test whose fixture cannot trigger the overflow.
6. **Record drift.** Campaign-doc/PR claims that are false at HEAD (a pin recorded at the wrong
   SHA, local test counts presented as CI counts, "done" for work whose tests were deleted).
   Correct the record as part of the review.
7. **Hardcoded operational tunables.** Byte/char bounds, retention caps, timeouts, poll intervals,
   retry budgets sprinkled as literals. These belong in config (see below); protocol/schema-owned
   invariants stay constants. Also catch the same semantic bound hardcoded in two places.
8. **Two-lane violations.** Any trace/UI-lane fact derived from a bounded model-lane string;
   any bound applied before escaping (re-escaping after slicing overruns the cap).
9. **Session-grinding in qualification records.** Many re-run versions of a live session over
   semantic nitpicks signals process failure, not diligence — read the FIRST clean run's evidence.

## Per-repo checklists

### clio-agent (backend)
- Ground rules: no silent fallback (typed reasons per the `stream_fallback` catalog); no ratchet
  raises AND the checker scripts themselves not weakened; failing-first tests incl. error paths and
  concurrency; no prose heuristics deciding routing/completion (format-only repair is the limit);
  no fifth store (new persistence goes in an existing store); negotiation-not-timing (per-exchange
  progress deadlines that reset on activity are the only sanctioned clock); two-lane observability.
- Run: `uv run ruff check src/`, `scripts/check_file_size.py`, `scripts/check_silent_fallbacks.py`,
  route-count guardrail, `scripts/gen_env_reference.py --check`. Focused pytest with
  `CLIO_ALLOWED_ROOTS="$TMPDIR:$PWD"` for tool-server tests.
- Submodule gitlinks must point at MERGED main/develop commits (never feature-branch-only SHAs
  that dangle on branch deletion) — verify with `branch -r --contains`.
- Config: every operational tunable through `conf.resolve` + `config.defaults.yaml` (categorized,
  commented: what it does, unit, when to change) + regenerated env references.

### gact-tui (frontend)
- Server owns semantics: any surviving client heuristic goes through `reportPresentationOverride`
  + the registry with a REAL filed server issue; `scripts/check_presentation_overrides.mjs` stays
  green; its cap rises only with named issues.
- The render contract (`web/CONVERSATION-RENDER-CONTRACT.md`) is owner-locked: components must
  match it; fixes conform to it, never amend it.
- Typed degradation everywhere: `.catch()` on every enum; per-frame safeParse containment on EVERY
  stream (child/secondary streams too, the main one is not enough); honest state mappings with
  never-guards; a known-type block failing strict parse degrades per-block, never per-transcript.
- Separators are layout (no middot/em-dash glyph joins). File-size <800, component-reuse check,
  oxlint --deny-warnings.
- CI truth: media policy; tag-gated release jobs syntactically live; the native WebView proof must
  assert what its name claims under EVERY flag combination; generated descriptors (brand) must be
  regenerated in the pipeline AND drift-gated, never trusted as committed.
- Deletion inventory: grep configs/workflows/Docker/docs for references to deleted trees.

### marketplace
- No `default_model:` pins without an adjacent justification comment (policy lint runs in CI);
  session-model inheritance everywhere. Pack frontmatter parses (fences intact).

### clio-kit
- File-size ratchet; typed skip/degradation events (no fabricated zeros — absent measurements are
  `None`); lock-file audits across all committed locks.

## Fix-phase rules (when the review leads to fixes)

- Sequential slices per tree, grouped by owner area; each slice: failing-first test → fix →
  focused tests + lint + guards → conventional commit (no attribution footers).
- Minimal fixes, no new abstractions; ratchet pressure is solved by extraction to owner modules,
  never baseline raises.
- Disputes and skips are first-class outcomes: a skip states precisely what larger work is needed
  and becomes a filed issue; it is never silent.
- After fixes: push, full CI at the new exact head, THEN the verdict comment, THEN qualification,
  THEN merge. Stop at develop unless a release is explicitly authorized.
