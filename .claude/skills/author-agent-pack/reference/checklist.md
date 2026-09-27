# Pack review checklist

## Level honesty
- [ ] The pack's name and description claim the level it has been **tested** at (at least 2 instances), no higher.
- [ ] The `AGENT.md` body states the contract: format id, version range, what "any instance" covers.
- [ ] Every skill has a `level:Lx` keyword, and its content matches that level.
- [ ] No L3 literals in the pack: `.lint-l3` and `lint-denylist.txt` are present, and `scripts/check_skill_literals.py` passes.
- [ ] Skills describe what *can* exist and tell the agent to discover what *does* exist.
- [ ] Nothing in the pack would have to change for a new instance of the same source.

## Knowledge placement
- [ ] General habits are in L0 skills; domain methods in L1; format and instrument knowledge in L2.
- [ ] Instance facts live only in experiment cards stored with the data.
- [ ] Lessons are written as checks. Promoted checks have evidence from 2 or more instances, or the format docs.
- [ ] No format-specific MCP. Deterministic helpers are bundled skill scripts (PEP 723, `uv run --no-project`).
- [ ] Existing packs' skills were checked for overlap first (e.g. `data-semantics`) and reused or referenced.

## Behaviour
- [ ] The first session writes the card, the loader and the validated views. The second session reuses the card.
- [ ] If a child agent audits, the parent re-runs the loader and compares hashes.
- [ ] Missing semantics (units, what treatment codes mean) are asked about, not assumed.
- [ ] Out-of-contract input (e.g. an unknown format version) is refused clearly.
- [ ] Claims are tagged as stated, checked or inferred.

## UI
- [ ] Only builtin, generic components are used. A pack catalog, if any, contains presets or aliases only.
- [ ] Any new visual capability is proposed as core work (clio-schemas + gact-tui), not as a pack catalog.

## Evals
- [ ] Two or more real instances, plus mutated variants, plus out-of-contract cases.
- [ ] Held-out status is stated honestly (was the instance described anywhere the pack or model could see?).
- [ ] Trap-catch, card-reuse, determinism, refusal and per-skill question cases are all present.
- [ ] Marketplace CI passes: unittest, `check_model_pins.py`, `check_skill_literals.py`, blueprint validation.

## Process
- [ ] Work is on feature branches (off `develop`, or `main` where no `develop` exists), in worktrees, not merged until the owner says so.
- [ ] A separate critical reviewer agent has looked at the design, and its findings were addressed or explicitly declined.
