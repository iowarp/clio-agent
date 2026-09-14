# clio-agent

> Historical local context snapshot preserved during the v0.9.2 worktree cleanup.
> This generated document is review evidence, not the current repository contract.

clio-agent is a Python project. CLIO Agent — Autonomous AI agent framework for scientific data management. Part of the IOWarp platform.

## Conventions

- Commit subjects are imperative, lowercase, conventional, at most 72 characters, and end without a period.
- **No silent fallback.** Every degradation, downgrade, or alternate path must
- Custom async/sync bridge code (use native DSPy/FastMCP instead)
- `PLAN.md` at the repo root is a superseded pointer file — do not treat its
- The GACT server, main agent, and CLI must always work
- Smoke-check before committing: `uv run src/clio_agent/ui/cli.py`

## Context retrieval

Start orientation with these indexed entry points: `src/clio_agent/arc/index.py`, `src/clio_agent/gact/__main__.py`, `src/clio_agent/ui/cli.py`, `src/clio_agent/__init__.py`, `src/clio_agent/gact/types.py`, `src/clio_agent/gact/runtime/globals.py`, `src/clio_agent/runtime/__init__.py`, `src/clio_agent/gact/events.py`. Use `code_nav` (modes: symbol, path, entries, outline, deps, dependents, wiki) before broad reads when the task is navigational.

## Agent context interop

Bootstrap scanned 7 sibling context sources: `.claude/CLAUDE.md`, `AGENTS.md`, `.claude/skills/codex-review/SKILL.md`, `.claude/skills/grind-clio-case/GOAL_TEMPLATE.md`, `.claude/skills/grind-clio-case/parallel-clio.md`, `.claude/skills/grind-clio-case/SKILL.md`, `.claude/skills/release-clio/SKILL.md`. Run `clio-coder context init --adopt` to refresh the managed provenance section when those sources change.

## Verification expectations

Run `pytest` before handoff.
