# clio-agent

clio-agent is a Python project. CLIO Agent — Autonomous AI agent framework for scientific data management. Part of the IOWarp platform.

## Hard invariants

1. Never introduce a hidden class inside a function (`scripts/check_no_class_in_function.py`) – such a pattern is invisible to imports and breaks typing.
2. Never let a source file exceed its ratchet baseline (`scripts/check_file_size.py`); when reducing a file, also lower the baseline entry in the same commit.
3. Never allow silent‑fallback paths that omit a structured error reason (`scripts/check_silent_fallbacks.py`), otherwise the runtime will abort with a BLE001/E722 violation.
4. Never push a new ‘god‑file’ or let an existing baselined file grow past its recorded line count (`.github/workflows/ci.yml` comment on anti‑re‑accretion guardrails).
5. Never skip the marketplace test suite (`tests/_marketplace.py`) – CI marks the job a failure if the submodule is missing.
6. Never commit a change that breaks the GACT entry point (`src/clio_agent/gact/__main__.py`) – the guard ensures no early imports create cycles.

## Conventions

- `scripts/check_file_size.py` caps new source files at `DEFAULT_MAX_LINES` (≈500) unless listed in its `RATCHET_BASELINE` mapping; shrink the baseline when a file is reduced.
- `scripts/check_no_class_in_function.py` forbids any ``class`` definition nested inside a function; lift hidden classes to module scope before committing.

## Authored project rules

- These were learned the hard way grinding the model-agnostic marketplace across model families (qwopus/LM Studio, gpt-oss/gemma/nemotron/ALCF). When older rules below conflict with these, **these win.** Much of the "Locked-Down Stack" / numbers below is stale or aspirational — do not treat it as ground truth. (source: .claude/CLAUDE.md)
- **Allowed vs forbidden in core.** clio MAY *surface/gate on reality* — file/path exists, HITL block, auth gate, **schema-validate**, and **format-only error correction** with no semantic change (the lenient constructor-repr→JSON adapter; a declared Pydantic field default). clio must NOT *silently fix / reroute / scrub / decide* via case-logic or keyword heuristics. (source: .claude/CLAUDE.md)
- **Fix the root in code/data-flow, don't bolt constraints onto prompts.** Do not add `EXACTLY ONCE` / `no &&` / "use column X" prose to expert `.md`s to suppress an observed failure — that overfits to tested cases, bloats context (which *causes* small-model failures), and forecloses edge cases. Fix the cause: lean tool outputs, non-hanging shell, forward the discovered columns, pin staging dirs as infra. Prompt editing IS a valid lever for *understanding/grounding* (telling the model what state means), just not for hard behavioral handcuffs. (source: .claude/CLAUDE.md)
- **Be the trace-driven driver (the loop).** (1) run the target test, (2) read the FULL trace — `~/.config/clio-agent/messages/sess_*.json` + `run.extra.workflow_state`, not the summary; add instrumentation where understanding is thin, (3) hypothesize ONE cause, (4) verify cheaply (~30s probe) before any multi-minute rerun, (5) fix the root, (6) one change per rerun, regression-check, advance. Don't run blindly; don't declare blocked; dig. Deterministic code is only ever the *last* error-correction barrier. (source: .claude/CLAUDE.md)
- **No silent fallback.** Every degradation, downgrade, or alternate path must emit a structured reason that reaches the trace/API — or the path gets deleted. The model is the `stream_fallback` catalog in `src/clio_agent/gact/streaming.py` (`_stream_fallback_payload` / `_stream_fallback_reasons`): a typed reason, recorded per session, queryable after the fact. A bare `except: pass` or an unexplained quality downgrade is a bug even when the output looks fine. (source: .claude/CLAUDE.md)
- **No accretion.** Fixes that add more than a trivial amount of code go in an owner module, not appended to a god file — patch-driven development is how `turn.py` / `agent.py` / `app.py` grew to 3–4k lines. The CI guards (`scripts/check_file_size.py`, `scripts/check_no_class_in_function.py`) are now **enforcing** via per-file ratchet baselines (#774): a new god-file or a baselined file that regrows past its recorded count fails CI; baselines only ratchet down. Do not add to files they flag. (source: .claude/CLAUDE.md)
- **DO NOT add** without approval: LangChain, CrewAI, AutoGen as core dependencies (external integration only via A2A); Heavy ML frameworks (TensorFlow, PyTorch); Database ORMs (SQLAlchemy); Custom async/sync bridge code (use native DSPy/FastMCP instead) (source: .claude/CLAUDE.md)
- `PLAN.md` at the repo root is a superseded pointer file — do not treat its historical phases as tasks (source: .claude/CLAUDE.md)
- The GACT server, main agent, and CLI must always work (source: .claude/CLAUDE.md)
- **DO NOT expose** DSPy in user-facing docs, APIs, or error messages (source: .claude/CLAUDE.md)
- **Exception**: CLAUDE.md and code comments only (source: .claude/CLAUDE.md)
- Rule of conduct: do NOT add a fifth store. New persistent state goes in an existing store, and new code should not deepen the duplication #737 removes (source: .claude/CLAUDE.md)
- **DO NOT auto-generate** tools from OpenAPI specs or file system scans (source: .claude/CLAUDE.md)
- Never dump raw conversation history into prompts (source: .claude/CLAUDE.md)
- **DO NOT concatenate** all ARC data into a single string (source: .claude/CLAUDE.md)
- **DON'T**: mix responsibilities, or let an expert spawn a child it did not DECLARE (the parent→child edge is enforced). Spawn depth is a COMPUTED runaway backstop (`depth > MAX_SPAWN_DEPTH` = 8 refused, typed `spawn_depth_exceeded`), NOT a 3-tier rule — `tier` is semantic weight, not depth, so deep declared chains are legitimate (source: .claude/CLAUDE.md)

## Change recipes

- Ensure its line count is below `DEFAULT_MAX_LINES` (see `scripts/check_file_size.py`).
- Add the file path to `RATCHET_BASELINE` with the same count if it is expected to be large; otherwise omit it.
- Run `uv run ruff check src/` locally to satisfy lint.
- Commit **both** the modified source file and the updated entry in `scripts/check_no_class_in_function.py`’s `RATCHET_BASELINE` (if the file was previously listed).
- Edit `scripts/check_tool_instrumentation.py` and add the new module path to the `sanctioned` frozenset.
- Update any import statements to use the wrapper functions from `clio_agent.gact.agents.tool_instrumentation`.
- Run the CI locally (`uv run python scripts/check_tool_instrumentation.py`) to confirm no violations.
- After shrinking a file, edit `scripts/check_file_size.py` where `RATCHET_BASELINE` is defined.

## Gotchas

- The CI workflow (`.github/workflows/ci.yml`) treats a file‑size regression as a hard error; do not rely on lint warnings to catch it.
- The `check_noqa_swallows.py` script (present but with no checks listed) currently ensures that adding a `# noqa` does not hide new violations; adding a blanket `# noqa: *` may trigger a future failure.
- The desktop bundle smoke test (`.github/workflows/desktop-bundle-smoke.yml`) only runs when files under `install/**`, `branding/**`, `uv.lock`, or the desktop scripts change; changes to core runtime code do not trigger it.
- The `scripts/check_bundle_matches_lock.py` enforces that the bundled runtime extras equal `BUNDLE_EXTRAS`; mismatched extras cause a build‑time failure.

## Agent context interop

Bootstrap scanned 9 sibling context sources: `.claude/CLAUDE.md`, `AGENTS.md`, `.claude/skills/a2ui-component-design/SKILL.md`, `.claude/skills/codex-review/SKILL.md`, `.claude/skills/grind-clio-case/GOAL_TEMPLATE.md`, `.claude/skills/grind-clio-case/parallel-clio.md`, `.claude/skills/grind-clio-case/SKILL.md`, `.claude/skills/release-clio/SKILL.md`, .... Run `clio-coder context init --adopt` to refresh the managed provenance section when those sources change.

## Verification expectations

CI runs `exit 1`, `fi`, `uv run ruff check src/ tests/ scripts/create_demo_data.py scripts/validate_marketplace_blueprints.py scripts/mcp_mem_attribution.py`, `uv run ruff format --check --diff src/ tests/ scripts/`, `uv run python scripts/check_file_size.py`, `uv run python scripts/check_no_class_in_function.py`, `uv run python scripts/check_no_summaries.py`, `uv run python scripts/check_no_settle_vocabulary.py`. These are CI checks, not authorization to expand the current task. Run `uv run pytest`. Choose checks relevant to the change within the user-authorized scope and authored project policy. `verify` can run declared checks; an authorized command it cannot run can be executed through `bash`.
