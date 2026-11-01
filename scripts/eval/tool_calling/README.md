# Tool-calling evaluation harness

The instrument for [the tool-calling plan](../../../docs/design/sdk/tool-calling-plan.md)'s
"Evaluation" section and Phase 0 (ledger decisions TD22, TD23 and TD33). It runs
plain-request tasks against a running GACT server over REST, records model
steps, tokens and timing per run, grades answers mechanically where an answer
key exists, and renders the ledger's Evaluation tables. It imports nothing from
`src/`; the only coupling is the REST API.

| Module | Does |
| --- | --- |
| `fixtures/generate.py` | Writes every fixture, the `clio-eval` marketplace and `manifest.json` |
| `servers/` | The synthetic MCP servers the marketplace's plugins start |
| `tasks.py` | The thirteen task prompts and their session setup |
| `runner.py` | `setup`, `plan` and `run` against a server |
| `session_run.py` | One run: workdir, workspace, session, wait, collect |
| `collect.py` | Session export to the normalized run shape |
| `metrics.py` | Per-run metrics |
| `grade.py` | Answer-key grading (no LLM judge) |
| `claude_code_ref.py` | The Claude Code reference on P1 to P4 |
| `summarize.py` | Median and range per task and configuration, as Markdown tables |

Every command below runs from the repository root. Runtime state stays in
`.cloud-run/`.

## 1. Generate the fixtures

```sh
uv run --frozen --no-sync python -m scripts.eval.tool_calling.fixtures.generate
```

Options: `--seed` (default 7), `--base` (default `32d7cbc6`, the plan's base),
`--out` (default `.cloud-run/eval-fixtures/`). It replaces the output folder.
The same seed and base always give a byte-identical `manifest.json`, which
holds a sha256 per file (git internals excepted; repository fixtures record
their commit SHAs instead) and one answer key per task.

| Folder | Tasks | Contents |
| --- | --- | --- |
| `tree/` | P1, P4 | `git archive <base> src/clio_agent` |
| `p2/` | P2 | `station_registry.py`: 5,000 lines, the fact near line 4,300, two decoys |
| `p3/tools/` | P3 | `src/clio_agent/tools` with five seeded bugs, one per file |
| `home/` | P5 to P10 | A README; the data lives behind the synthetic servers |
| `h1/` | H1, H3 | `ledgerlite`, a git repo whose fourth commit breaks the report tests |
| `h2/` | H2 | `recstream`, a git repo whose fourth commit makes reads reopen the file per record |
| `marketplace/` | P5 to P10 | The `clio-eval` plugins |

Answer keys: P1 a uniquely named function with its definition and every call
site (found with `ast`); P2 the line and value; P3 file, line range, category
(`off-by-one`, `swallowed-exception`, `wrong-default`, `unclosed-file`,
`inverted-condition`), function and before/after text of each bug; P4 four
module names and up to two entry points for each of `arc`, `plugins` and
`providers`; P5 to P7 the negative record ids and their count; P8 and P9 the
tool and arguments; H1 to H3 the root-cause file, function and introducing
commit. Generation checks that the H1 tests pass before the regression and
fail at HEAD, and that the H2 tests pass at HEAD.

## 2. The synthetic servers and plugins

The marketplace (`ai.iowarp.clio/marketplace.json`, `plugins/<id>/plugin.json`,
`plugins/<id>/mcp.json`, `plugins/<id>/server/*.py`) holds:

| Plugin | Servers | Tools |
| --- | --- | --- |
| `eval-home` | `synthetic-home`, `synthetic-calendar`, `synthetic-inventory` | 95, 15, 15 |
| `eval-records-20`, `-200`, `-2000` | `records` | `list_records(page=1, page_size=50)`, `get_record(record_id)` |

Server names use `[a-z0-9-]` because CLIO derives a tool's namespace by
splitting its name on the first `_`. Results come from seeded, read-only state:
a write reports what it would change but never persists, so every run sees the
same home. `list_records` returns ids without readings (at most 100 per page);
only `get_record` has the reading, and about 8% are negative.

Every call is appended to a JSONL log: `EVAL_CALL_LOG`, which `mcp.json` sets
to `${CLIO_EVAL_CALL_LOG:-${PLUGIN_DATA}/calls.jsonl}`. The servers are shared
by every session with the same plugins, so the runner slices the log by the
run's time window; grading uses each session's own tool records instead.

The plugins start servers with the bare command `python` (a plugin rule: no
absolute paths), which must have FastMCP. `scripts/cloud/stack.sh` serves with
`uv run`, which puts `.venv/bin` first on `PATH`. Use `--python python3` at
generation time if the server's `PATH` differs.

No agent is added. A session runs `clio/standard` with a plugin delta in its
metadata: `{"agent_ref": "clio/standard", "agent_delta": {"add":
["clio-eval/eval-home"], "remove": []}}`. To see what that resolves to:

```sh
uv run --frozen --no-sync clio-agent plugin resolve clio/standard --add clio-eval/eval-home
```

## 3. Start the stack with the logs the harness reads

```sh
export CLIO_EVAL_CALL_LOG=$PWD/.cloud-run/eval-calls.jsonl
export CLIO_STREAM_AUDIT_LOG=$PWD/.cloud-run/stream-audit.jsonl
# The plan's evaluation stacks use the marketplace checkout on the plan's branch:
export CLIO_DEFAULT_MARKETPLACE=$PWD/external/clio-agent-marketplace
bash scripts/cloud/stack.sh restart
```

The server must start after these exports so it inherits them. The runner reads
the same two variables as its `--call-log` and `--stream-audit-log` defaults.
Without the stream audit, prefix resets are reported as unknown, not zero.

A server on another host needs `CLIO_GACT_BEARER_TOKEN` in the runner's
environment (`--token-env` names a different variable); loopback needs none.

## 4. Install the eval plugins

```sh
uv run --frozen --no-sync python -m scripts.eval.tool_calling.runner setup
```

This installs (or, when present, updates) the `clio-eval` marketplace from
`.cloud-run/eval-fixtures/marketplace` with `POST /v1/plugins/marketplaces`,
trusts it (its plugins launch servers, so they need trust) and checks that
`clio/standard` resolves with each plugin. Rerun it after regenerating the
fixtures.

## 5. Smoke run

```sh
uv run --frozen --no-sync python -m scripts.eval.tool_calling.runner plan --tasks P10 --n 1
uv run --frozen --no-sync python -m scripts.eval.tool_calling.runner run --config smoke --tasks P10 --n 1 --serial
uv run --frozen --no-sync python -m scripts.eval.tool_calling.metrics .cloud-run/eval-results/smoke/P10/run-1.json
uv run --frozen --no-sync python -m scripts.eval.tool_calling.grade .cloud-run/eval-results/smoke/P10/run-1.json
uv run --frozen --no-sync python -m scripts.eval.tool_calling.summarize
```

## 6. Measure a configuration

```sh
# Timing pass: one run at a time.
uv run --frozen --no-sync python -m scripts.eval.tool_calling.runner run --config base --tasks all --n 3 --serial
# Other passes may run three at a time (wall time changes, steps and tokens do not).
uv run --frozen --no-sync python -m scripts.eval.tool_calling.runner run --config phase1 --tasks all --n 3 --concurrency 3
# The Claude Code reference on P1 to P4 (see the note on CLAUDE.md below).
uv run --frozen --no-sync python -m scripts.eval.tool_calling.claude_code_ref run --n 3
uv run --frozen --no-sync python -m scripts.eval.tool_calling.summarize --out .cloud-run/eval-summary.md
```

`--config` is a label (`base`, `phase1`, an ablation name); the server under
test decides what the configuration is. Defaults follow TD22: provider
`claude_code`, model `claude-sonnet-5-5`, set per session. The fleet `explore`
role (`claude-haiku-5-5`) is a server setting; each result records
`GET /v1/settings/fleet` as `fleet_settings`. Sessions use approval mode
`bypass` (`--approval-mode`) so no run waits on a person; a run that still
waits for a question or permission longer than `--interaction-timeout` is
cancelled and fails the completion guard. `--session-metadata '{...}'` merges
extra session metadata, for ablations. H3 sets
`context_preferences.autocompact_pct` to `--h3-autocompact-pct` (default 0.25)
so it compacts at least once.

**Usage guard.** Before each run the runner reads `GET /v1/fleet`
(`accounts[].usage.windows[]`: `name`, `used_percent` from 0 to 100,
`resets_at`, `window_minutes`) and stops scheduling when any 7-day window of
`--account` (default `claude_code`) is at or above `--stop-at` (default 0.93),
printing the window, its level and its reset time, and exits with status 3.
Usage is captured passively from each call, so a fresh server has no reading
yet; the guard then starts the run and says so. Existing results are kept, so
the same command resumes after the window resets (`--force` reruns).

**Claude Code reference.** `claude -p --output-format stream-json --verbose
--include-partial-messages --model <model> --permission-mode bypassPermissions`
in a copy of the fixture. Claude Code loads `CLAUDE.md` from the work folder's
ancestors, so a work folder under this checkout inherits the repository's
instructions; each result lists them under `ancestor_instructions`. Pass
`--work-root` outside the checkout for a clean reference. Its timing comes from
when stream lines arrive (`timing_source: client_receive`); its usage-window
shares come from the stream's rate-limit events.

## Results

`<results>/<config>/<task>/run-<n>.json` (`--keep-export` also saves the raw
export as `run-<n>.export.json`; the reference saves `run-<n>.stream.jsonl`).
A result holds the prompt, session handles (root and children), the workdir,
`wall_s`, the completion outcome, `sessions` (per session: `lm_calls` with
usage and timing, `tool_calls` with start and end, `steps`, event counts),
`fleet_before`/`fleet_after` with the per-window deltas, the run's
`provider.stateful` stream-audit rows and its synthetic-server calls.

How each metric is computed is in `metrics.py`'s docstring; what each task's
grade checks is in `grade.py`'s. Correctness is recorded, not gated (TD23); a
run counts only if it ended with a final answer.

## REST routes used

| Route | For |
| --- | --- |
| `GET /v1/health` | The server answers |
| `GET /v1/plugins/marketplaces`, `POST /v1/plugins/marketplaces`, `POST .../{name}/update`, `POST .../{name}/trust`, `GET /v1/agents/resolve` | `setup` |
| `GET /v1/workspaces`, `POST /v1/workspaces` | One workspace per run, pinned to its fresh workdir |
| `POST /v1/sessions` | Agent ref and plugin delta in `metadata`, model, approval mode |
| `POST /v1/sessions/{sid}/messages` | The prompt (`delivery: start`) |
| `GET /v1/sessions/{sid}`, `GET /v1/sessions/{sid}/messages?limit=5` | Polled every `--poll` seconds until the turn has ended and stayed ended for `--settle` seconds |
| `POST /v1/sessions/{sid}/cancel` | Timeout or an unanswered question |
| `GET /v1/sessions/{sid}/export` | Messages, full semantic events (`lm.call`, `tool.call.*`, `react.step.completed`, `compaction.*`, `goal.redrive`) and every descendant session |
| `GET /v1/fleet`, `GET /v1/settings/fleet` | Usage windows before and after; fleet roles |

While the server boots it answers `503 task_runtime_starting`; the client waits
that out for up to three minutes.

## Tests

```sh
uv run --frozen --no-sync python -B scripts/run_tests.py tests/test_eval
```
