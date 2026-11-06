# Branch cleanup, 6 November 2026

A checklist only: this PR was opened and closed so the list has a home. Nothing here is
meant to merge. Checked against `develop` and `claude/tool-calling-long-horizon` (PTC) at
18:00 UTC. "Nothing left" means a trial merge into `develop` (or PTC) changes no file.
Full map: https://claude.ai/artifact/X3db8723GXvBcAer2yJYQH

Left alone on purpose: `claude/epic-hopper-6s2vhn` (SPOTTER team), `develop`, `main`,
open PRs (`claude/tool-calling-long-horizon`, `claude/direct-summoning`), and the
"startup fixes" stack tips under review (`store-relocate`, `residual-gaps`,
`recall-index`, `migrate-fast`, `restart-hang`, `fresh-first-start`, `office-arm64`,
`desktop-combined-test`).

## Delete now (nothing left)

### iowarp/clio-agent — https://github.com/iowarp/clio-agent/branches/all
- [ ] `chore/branch-cleanup-2026-11-06` (this PR's own branch)
- [ ] `fix/1839-1840-1841` (in develop)
- [ ] `tutorial/remote-deployment` (in develop)
- [ ] `installation-setup-optimization` (in develop except one doc line)
- [ ] `claude/b3-mcp-prepare` (contained in the startup-fixes tips)
- [ ] `claude/crash-and-budgets` (contained in the tips)
- [ ] `claude/drain-bound` (contained in the tips)
- [ ] `claude/r2-followups` (contained in the tips)
- [ ] `claude/r2-migrate` (contained in the tips)
- [ ] `claude/r3r4-followups` (contained in the tips)
- [ ] `claude/r3r4-paging` (contained in the tips)
- [ ] `claude/r5-task-lane` (contained in the tips)
- [ ] `claude/r5-tree-reads` (contained in the tips)
- [ ] `claude/r6-cache-owner` (contained in the tips)

### iowarp/gact-tui — https://github.com/iowarp/gact-tui/branches/all
- [ ] `claude/clever-fermi-y5tw6h` (in develop)
- [ ] `claude/delegation-fork-science` (in develop)
- [ ] `claude/install-and-startup` (in develop)
- [ ] `claude/launcher-watches-desktop` (in develop)
- [ ] `claude/remote-agent-choices` (in develop)
- [ ] `claude/ui-waits-for-start` (in develop)
- [ ] `claude/b4-boot-lean` (landed; the few differing lines were edited again later)
- [ ] `claude/copy-sweep` (same)
- [ ] `claude/frontend-size` (same)
- [ ] `claude/b3-mcp-ui` (same)
- [ ] `claude/no-middle-dot` (same)
- [ ] `claude/r3r4-followups` (same)
- [ ] `claude/r3r4-paging` (same)

### iowarp/clio-agent-marketplace — https://github.com/iowarp/clio-agent-marketplace/branches/all
- [ ] `claude/clever-fermi-y5tw6h` (in develop)
- [ ] `claude/delegation-fork-science` (in develop)
- [ ] `codex/release-develop-ci` (in develop)

### iowarp/clio-schemas — https://github.com/iowarp/clio-schemas/branches/all
- [ ] `claude/clever-fermi-y5tw6h` (in develop)
- [ ] `ccr-f1dc34ec-h6ucm1` (in develop)

## Delete after the PTC PRs merge (everything in them is inside PTC)

PTC PRs: iowarp/clio-agent#1924, iowarp/gact-tui#713, iowarp/clio-agent-marketplace#122,
iowarp/clio-schemas#48.

- clio-agent: `claude/gracious-goldberg-gagoo4`, `claude/compaction-handoff`,
  `claude/relaxed-albattani-np48nf`, `claude/vibrant-johnson-gskxcf`,
  `claude/session-scratch-folder`, `claude/tool-settings-permissions`
- gact-tui: `claude/busy-turing-r40z0q`, `claude/keen-dirac-hf724m`,
  `claude/bold-cori-inzcdd`, `claude/gracious-goldberg-gagoo4`,
  `claude/vibrant-johnson-gskxcf`, `claude/tool-settings-permissions`
- marketplace: `claude/busy-turing-r40z0q`, `claude/keen-dirac-hf724m`,
  `claude/gracious-goldberg-gagoo4`
- clio-schemas: `claude/keen-dirac-hf724m`
- all four: `claude/tool-calling-long-horizon` itself

## Needs a decision (unmerged work, no PR)

- clio-agent `claude/remote-agents-setup` (21 Oct, ~2,000 lines: remote agents plan, plan flow)
- clio-agent `claude/installation-changes` (5 Nov, Windows installer, runtime compression)
- clio-agent `claude/gracious-archimedes-uu7gbm` (one review doc)
- clio-agent `chore/provider-sdk-bump` (dependency bump)
- clio-agent `ccr-f3f98215-lvwhes` (pushed today, OPAL qualification notes)
- clio-agent `feat/distributable-expert-runtime` (June, unrelated history)
- the eight startup-fixes tips listed above (under review)
