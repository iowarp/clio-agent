# Contributor Quickstart

This is the shortest path to make safe, reviewable changes.

## 1) Set up local environment

```bash
uv sync --extra dev --extra optimizers
```

## 2) Run quality checks before every PR

```bash
uv run ruff check src/ tests/ scripts/create_demo_data.py
uv run ruff format src/ tests/ scripts/create_demo_data.py
bash -n scripts/homelab-env.sh
uv run mypy src/
uv run pytest tests/ -m "not integration" --cov-fail-under=80
```

Run full test suite (including integration tests) when changing routing, experts, ARC, or API behavior:

```bash
uv run pytest tests/
```

## 3) Run the app locally

```bash
source scripts/homelab-env.sh
clio_homelab_use dynamo-lms
export CLIO_ALLOWED_ROOTS=/home/akougkas/iowarp/clio-agent:/tmp
uv run src/clio_agent/ui/cli.py doctor
uv run scripts/create_demo_data.py --output-dir /tmp/clio-agent-demo
uv run src/clio_agent/ui/cli.py
uv run clio-agent-gact --host 0.0.0.0 --port 8100
```

## 4) Where to put code

- Core orchestration: `src/clio_agent/agent.py`, `src/clio_agent/registry/`
- Memory and indexing: `src/clio_agent/arc/`
- Expert logic: `src/clio_agent/experts/`
- Tools and servers: `src/clio_agent/tools/`
- Interfaces: `src/clio_agent/ui/`
- Tests: mirror code domains under `tests/`

## 5) Commits and PRs

Use Conventional Commit style:

- `feat(api): add ...`
- `fix(arc): handle ...`
- `test(core): cover ...`

PR checklist:
- Explain problem and solution clearly.
- Link issue(s) when relevant.
- Include lint/test command output in PR description.
- Add or update tests for behavior changes.
