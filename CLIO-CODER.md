# clio-agent

clio-agent is a Python project. CLIO Agent — Autonomous AI agent framework for scientific data management. Part of the IOWarp platform.

## Hard invariants

1. Never introduce a hidden class inside a function (`scripts/check_no_class_in_function.py`) – such a pattern is invisible to imports and breaks typing.
2. Never let a source file exceed its ratchet baseline (`scripts/check_file_size.py`); when reducing a file, also lower the baseline entry in the same commit.
3. Never allow silent‑fallback paths that omit a structured error reason (`scripts/check_silent_fallbacks.py`), otherwise the runtime will abort with a BLE001/E722 violation.
4. Never push a new ‘god‑file’ or let an existing baselined file grow past its recorded line count (`.github/workflows/ci.yml` comment on anti‑re‑accretion guardrails).
5. Never skip the marketplace test suite (`tests/_marketplace.py`) – CI marks the job a failure if the submodule is missing.
6. Never commit a change that breaks the GACT entry point (`src/clio_agent/gact/__main__.py`) – the guard ensures no early imports create cycles.
7. Each module under `src/clio_agent` that is not in the baseline must stay at or below `DEFAULT_MAX_LINES` (800 lines). A file listed in `RATCHET_BASELINE` in `scripts/check_file_size.py` must not grow past its recorded count. CI fails in either case. Put non-trivial new code in an owner module, not a god file. When a baselined file shrinks, lower or delete its entry in the same commit. A baseline never goes up.
8. Never define a class inside a function or method in `src/clio_agent`. `scripts/check_no_class_in_function.py` allows zero nested `ClassDef` nodes, at any depth, in files missing from its `RATCHET_BASELINE`, and no increase in files that are listed. Move the class to module scope and lower that file's count in the same commit.
9. Do not add a blind or swallowing except (`BLE001`, `S110`, `E722`) in `src/clio_agent`. `scripts/check_silent_fallbacks.py` has a baseline total of 0 and runs ruff isolated from the project config. `scripts/check_noqa_swallows.py` recounts with noqa ignored against a per-file JSON baseline, so a noqa comment still fails in a new file or in a file already at its count. Catch specific exceptions or raise a typed failure. Any sanctioned degradation must emit a structured reason, modeled on `_stream_fallback_reasons` in `src/clio_agent/gact/streaming.py`.
10. Never call `dspy.Tool(` directly in `src/clio_agent` outside the sanctioned list in `scripts/check_tool_instrumentation.py`. That list is `src/clio_agent/gact/agents/tool_instrumentation.py`, `src/clio_agent/tools/execution.py` and `src/clio_agent/gact/agents/clio_react.py`. Build tools with `native_tool`, `boundary_observed_tool` or `rebuilt_tool` so the tool observer sees them. The list may only shrink, so do not add your module to it. Tests are not scanned.

## Conventions

- `scripts/check_file_size.py` caps new source files at `DEFAULT_MAX_LINES` (≈500) unless listed in its `RATCHET_BASELINE` mapping; shrink the baseline when a file is reduced.
- `scripts/check_no_class_in_function.py` forbids any ``class`` definition nested inside a function; lift hidden classes to module scope before committing.
- Put new persistent state in an existing store. Do not add a fifth store (`.claude/CLAUDE.md`).
- Spawn depth is a computed runaway backstop: `MAX_SPAWN_DEPTH` = 8, refused as `spawn_depth_exceeded`. It is not a tier rule, and an expert may spawn only children it declares (`.claude/CLAUDE.md`).
- To make a DSPy output field droppable, give it a Pydantic field default, which is honored when a model omits the key. Do not repair the output after the fact (`.claude/CLAUDE.md`).
- Tests marked marketplace read shipped packs from the pinned submodule through `MARKETPLACE_ROOT` in `tests/_marketplace.py`. Never commit a copy of pack files into `tests/`.
- In `pyproject.toml`, the clio-core pin uses `~=2.3.2`, which follows patches within the tested minor. A new minor needs an explicit bump and compatibility validation. User-updatable provider components take a floor, never an upper bound.
- Server-built UI copy in `src/clio_agent` must not contain a middle dot (`U+00B7`), escaped spellings included. A literal that must keep it carries `# allow-middle-dot: <reason>` on its line (`scripts/check_no_middle_dot_copy.py`).

## Authored project rules

- Use Desktop or Web for current CLIO. The legacy TUI is frozen and unsupported as of 0.9.5.1; do not install or run its old binaries, images or build instructions. Use `clio start` / `clio-agent serve` for headless services and `clio web` for Web. (source: CLAUDE.md)
- For new tools and changes to existing inputs, results or lifecycle, follow [Adding or changing a tool](docs/tool-result-presentation.md#adding-or-changing-a-tool). Include the readable input/output showcase in the implementation, using the shared presentation contract and GACT readers. A callable tool with only raw JSON or an acknowledgement in its human result is unfinished. (source: CLAUDE.md)
- Current CLIO uses **Desktop or Web**, with `clio start` / `clio-agent serve` for headless services. The legacy TUI is **frozen and unsupported as of 0.9.5.1**. Do not install, build, run, recommend or use it to verify current CLIO. Its [source and integration notes](../docs/tui/README.md) remain for a future upgrade. (source: .claude/CLAUDE.md)
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
- **DO NOT auto-generate** tools from OpenAPI specs or file system scans (source: .claude/CLAUDE.md)

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

## Tests

- CI's main leg runs `scripts/run_tests.py` over `tests/` with -m "not integration", in 6 shards (`tests/_sharding.py`) on Python 3.12, 3.13 and 3.14. Put a regression test in the matching domain folder (for example `tests/test_gact`, `tests/test_arc`). Integration-marked tests do not run there.
- Run locally with `uv run --frozen --no-sync python -B scripts/run_tests.py tests/ -m "not integration"`, not bare pytest. It keeps runtime and cache state inside the checkout (`tests/_test_runtime_isolation.py`) and refuses cleanup while child processes are still alive.
- `tests/test_native_services` runs in its own job on Ubuntu and Windows with only pytest, pytest-asyncio and pytest-timeout, no project install, src on PYTHONPATH and conftest discovery cut at that directory. Tests there cannot use `tests/conftest.py` fixtures or third-party project dependencies.
- Code must still run on 3.12 even though `AGENTS.md` targets 3.14. On the 3.14 leg `.github/workflows/ci.yml` turns `DeprecationWarning` into an error.
- A marketplace-marked test fails, rather than skips, when the submodule is empty, for example in a fresh worktree or a plain clone (`tests/_marketplace.py`). Fix it with: git submodule update --init `external/clio-agent-marketplace`
- Every test has a hard timeout enforced by `tests/_hang_guard.py`. A test that legitimately needs longer declares a per-test timeout marker with a comment saying why. Prefer making it faster.
- `scripts/check_no_elicitation_id_vocabulary.py` scans `tests` as well as `src/clio_agent`. A fixture must not reintroduce `elicitationId` or its `snake_case` spelling. Correlate by `UserQuestion.id` (`gact/elicitation_bridge.py`).

## Docs and prose

- Never mention DSPy in user-facing docs, APIs or error messages. Only `CLAUDE.md` and code comments are exempt (`.claude/CLAUDE.md`).
- Do not add behavioral handcuffs (EXACTLY ONCE, "use column X") to expert prompt `.md` files to suppress an observed failure. Fix the cause in code or data flow. Prompt edits are for grounding only (`.claude/CLAUDE.md`).
- Never dump raw conversation history into prompts, and never concatenate all ARC data into one string (`.claude/CLAUDE.md`).

## Git and release

- Commits use Conventional Commit style with a scope. A PR includes a `problem/solution` summary, linked issues, and the `test/lint` commands with their results (`AGENTS.md`).
- `.github/workflows/ci.yml` runs on PRs into main, develop, `campaign/**`, `codex/**` and `claude/**` only. `.github/workflows/desktop-bundle-smoke.yml` runs only when `install/`, `branding/`, `uv.lock` or the desktop scripts change.
- Release tags drive `.github/workflows/clio-bundles.yml`. Pre-releases must be `vX.Y.Z-beta.N`, and a release stays a draft until it is complete.

## Verification expectations

CI runs `uv run --no-project --with pytest --with pytest-asyncio --with pytest-timeout --managed-python --python 3.14 python -m pytest --confcutdir=tests/test_native_services tests/test_native_services -q -rs`, `exit 1`, `fi`, `uv run ruff check src/ tests/ scripts/create_demo_data.py scripts/mcp_mem_attribution.py`, `uv run ruff format --check --diff src/ tests/ scripts/`, `uv run python scripts/check_file_size.py`, `uv run python scripts/check_no_class_in_function.py`, `uv run python scripts/check_no_summaries.py`. These are CI checks, not authorization to expand the current task. Run `uv run pytest`. Choose checks relevant to the change within the user-authorized scope and authored project policy. `verify` can run declared checks; an authorized command it cannot run can be executed through `bash`.
