# Contributor Quickstart

Desktop and Web are the supported CLIO interfaces. The legacy TUI is frozen and
unsupported as of 0.9.5.1; its source remains for a future upgrade. Do not use it
for current installation or release qualification.

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

Run full test suite (including integration tests) when changing plugins, agents, flows, ARC, or API behavior:

```bash
uv run pytest tests/
```

When changing a plugin, a pack or an agent class, run the install resolver on it:

```bash
uv run clio-agent plugin check external/clio-agent-marketplace
```

## 3) Run the app locally

```bash
source scripts/homelab-env.sh
clio_homelab_use dynamo-lms
export CLIO_ALLOWED_ROOTS=/home/akougkas/iowarp/clio-agent:/tmp
uv run src/clio_agent/ui/cli.py doctor
uv run scripts/create_demo_data.py --output-dir /tmp/clio-agent-demo
uv run src/clio_agent/ui/cli.py
uv run clio-agent serve --host 0.0.0.0 --port 8100
```

## 4) Where to put code

- Runtime host: `src/clio_agent/agent.py`
- Agent SDK (`Plugin`, `Pack`, `Agent`, `Subagent`, `Flow`): `src/clio_agent/sdk/`
- Plugin loader, importer, store, resolver and `plugin check`: `src/clio_agent/plugins/`
- Flow runtime (`ctx`, journal, outcomes): `src/clio_agent/flows/`
- Built-in `core` and `orchestration` plugins: `src/clio_agent/plugins/builtin/`
- Memory and indexing: `src/clio_agent/arc/`
- Tools and servers: `src/clio_agent/tools/`
- Interfaces: `src/clio_agent/ui/`
- Tests: mirror code domains under `tests/`

For a new or changed tool, follow the
[tool authoring guide](tool-result-presentation.md#adding-or-changing-a-tool).
The change includes its readable request/result presentation and representative
browser showcases, as well as the callable implementation. Use the existing
producer contract and shared frontend readers rather than another tool viewer.

## 5) Submodules

clio-agent ships two pinned git submodules:

- `external/gact-tui` — Web and Desktop, plus the frozen legacy TUI source.
- `external/clio-agent-marketplace` — the default marketplace: plugins, packs and
  agent classes ([PLUGINS_AND_FLOWS.md](PLUGINS_AND_FLOWS.md)).

**What the pins mean.** Both submodules are pinned at a **released tag**, not a
moving branch. The release skill (`.claude/skills/release-clio/SKILL.md`, steps
1–2) tags the marketplace first, then checks out the aligned `gact-tui` release
tag, and commits those exact SHAs into clio-agent. A given clio-agent commit
therefore always reproduces against a fixed frontend + plugin set. Do **not**
bump a submodule pointer casually in a feature PR — pin changes belong to the
release flow.

**Sibling dev checkouts.** When you need to develop clio-agent and a submodule
in tandem, check the submodule out on its own branch and point clio-agent at it
locally:

```bash
git -C external/gact-tui fetch origin
git -C external/gact-tui checkout <your-branch>   # detached pin is expected otherwise
```

Land the submodule change in its own repo first; clio-agent re-pins to the new
released tag at release time. A detached-HEAD submodule is the normal state
between releases — that is the pin, not a mistake.

**Clone size.** The submodules carry history (`.git/modules/external` is large —
hundreds of MB). New clones do not need that history to build. `.gitmodules`
marks both submodules `shallow = true`, so a `--recursive` clone fetches them
shallow automatically. To be explicit:

```bash
git clone --recurse-submodules --shallow-submodules https://github.com/iowarp/clio-agent
# or, on an existing clone:
git submodule update --init --recursive --depth 1
```

Existing full clones keep their large `.git/modules/external` until you re-clone
— shrinking it is non-destructive and optional (no history is rewritten).

## 6) Commits and PRs

Use Conventional Commit style:

- `feat(api): add ...`
- `fix(arc): handle ...`
- `test(core): cover ...`

PR checklist:

- Explain problem and solution clearly.
- Link issue(s) when relevant.
- Include lint/test command output in PR description.
- Add or update tests for behavior changes.
