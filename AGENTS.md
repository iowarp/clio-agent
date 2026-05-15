# Repository Guidelines

## Project Structure & Module Organization
`src/clio_agent/` contains production code, organized by responsibility:
- `ui/` for CLI and FastAPI entry points
- `experts/`, `registry/`, and `signatures/` for routing and expert behavior
- `arc/` for memory/indexing/storage components
- `tools/servers/` for FastMCP-backed tool servers
- `optimizer/` for tuning and instrumentation workflows
- `runtime/` for doctor and integration status reporting

Tests mirror the runtime layout in `tests/` (`test_core/`, `test_arc/`, `test_experts/`, `test_tools/`, `test_integration/`). Architecture and design docs live in `docs/`, and reference material is in `ai-docs/`.
Helper scripts for local demos and homelab setup live in `scripts/`.

## Build, Test, and Development Commands
Use `uv` for environment and command execution:
- `uv sync --extra dev --extra optimizers` installs contributor dependencies.
- `uv run ruff check src/ tests/ scripts/create_demo_data.py` runs lint checks.
- `uv run ruff format src/ tests/ scripts/create_demo_data.py` formats code.
- `uv run pytest tests/` runs the full test suite with coverage output.
- `uv run src/clio_agent/ui/cli.py doctor` reports runtime truth for LM, tools, file policy, API, and `clio-core`.
- `uv run src/clio_agent/ui/cli.py` starts the interactive CLI.
- `uv run src/clio_agent/ui/api.py --port 8000` starts the REST API locally.

## Coding Style & Naming Conventions
Target Python 3.12 with 4-space indentation and type hints on public interfaces. Keep lines readable within the configured `line-length = 100`. Follow existing naming:
- modules/functions: `snake_case`
- classes: `PascalCase`
- constants: `UPPER_SNAKE_CASE`

Ruff is the authoritative linter/formatter (`ruff`, `ruff-format` in pre-commit). Run lint/format before opening a PR.

## Testing Guidelines
Pytest is the test framework (`test_*.py`, `Test*`, `test_*`). Coverage is enabled by default via `pyproject.toml` (`--cov=clio_agent --cov-report=term-missing --cov-report=html`). Add tests in the matching domain folder and prefer deterministic fixtures from `tests/conftest.py`. Integration tests that need LM Studio are already guarded with skip conditions.

## Commit & Pull Request Guidelines
The history follows Conventional Commit style, e.g. `feat(api): ...`, `fix(ci): ...`, `test(core): ...`, `docs(04-03): ...`. Keep subject lines imperative and scoped.

PRs should include:
- a short problem/solution summary
- linked issue(s) when applicable
- test/lint evidence (commands + result)
- API/CLI example output when behavior changes
