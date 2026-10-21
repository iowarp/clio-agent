# MCP client unification — live verification RUNBOOK (#1286, C1-S6)

**Status:** PREPARED, not yet run live. Per the 2026-09-02 owner reorder recorded in
`docs/design/mcp-client-unification-2026-08.md`'s "Campaign 1 status" section:
the full live verification is prepared NOW on the campaign branch and runs on
**owner go-ahead**, before C1-S3..S5 land (not after). Nothing in this
package invokes a model — every script defaults to (or has) a
`--plumbing-only` mode; the live legs below spend real LM tokens only when
explicitly run by a human with the go-ahead. Legs B and C's `--plumbing-only`
mode HAS been run (zero LM spend) building this package's C1-S6 slice and
passes end-to-end through the testing-agent mechanism below -- see
"Testing-agent mechanism".

Study first if extending this package: `docs/design/
mcp-client-unification-2026-08.md` (plan of record), `gh issue view 1286`
(the gate spec this RUNBOOK maps to), `gh issue view 1301` (the bare-session
builtin-main defect legs B/C now route around, see below),
`scripts/live_gate_observe_1000.py` +
`scripts/live_gate_1031.py` (the house live-gate pattern these scripts
follow), `tests/test_tools/mcp_exerciser.py` (leg C's synthetic server),
`tests/test_real_cases/conftest.py` + `clio_sut.py` (the agent-test harness
legs A/D use), and `docs/PLUGINS_AND_FLOWS.md` (the plugin and agent model the
testing agents under `agents/` are built on).

## Order (cheapest signal first)

1. **Preflight** — no server, no model. Catches PATH/YAML/binary-presence
   gaps before anything else runs.
2. **Leg (ii) / B — web fetch smoke** — one server boot, one turn, driven
   through the `web-testing` testing agent (not a bare session --
   see "Testing-agent mechanism" below). The
   cheapest LIVE signal and the exact #1274 repro; if this is red, stop —
   legs C/A/D downstream would misattribute the same plumbing defect.
3. **Leg C — synthetic session** — one server boot, two turns (task=required
   + MRTR), driven through the `v2ex-testing` testing agent. Proves
   the generic client against the FULL v2 surface the exerciser forces, not
   just clio-kit's subset.
4. **Leg (i) / A — v1 fleet regression** — baseline diff (no model) + two to
   three live agent-test runs. Proves nothing regressed for the byte-identical
   v1 contract.
5. **Leg (iii)+(iv) / D — deep-researcher** — the most expensive, multi-
   session leg. Run last, only once B/C/A are green (a failure here with B
   red is not new information).

## One-command-per-leg table

| Leg | Command | Spends LM tokens? |
|---|---|---|
| Preflight | `uv run python scripts/live_verification/preflight.py` | No |
| B (plumbing) | `uv run python scripts/live_verification/leg_b_web_fetch.py --plumbing-only` | No |
| B (live) | `uv run python scripts/live_verification/leg_b_web_fetch.py --provider claude_code --model sonnet` | **Yes** (1 turn) |
| C (plumbing) | `uv run python scripts/live_verification/leg_c_synthetic_session.py --plumbing-only` | No |
| C (live) | `uv run python scripts/live_verification/leg_c_synthetic_session.py --provider claude_code --model sonnet` | **Yes** (2 turns) |
| A (baseline diff) | `uv run python scripts/mcp_v1_baseline.py --out out/live-verification/mcp_v1_baseline-c1s6.json` (then diff — see `leg_a_v1_fleet.md`) | No |
| A (live) | see `leg_a_v1_fleet.md` §2 | **Yes** (2-3 sessions) |
| D (live) | see `leg_d_deep_researcher.md` §1 | **Yes, most expensive** (multi-session) |

Every script also accepts `--port`/`--out`/`--ws-dir` (never a hardcoded
drive root — see each script's `--help`). Every verdict lands at
`out/live-verification/<leg>.json`; a script exits nonzero on failure.

## Go/no-go criteria, mapped to #1286's acceptance text

**Preflight** — no #1286 text maps directly; this is prep-only. Go = every
`required` check in `preflight.json` is `ok`.

**Leg (ii) — "clio-kit web MCP `task=required` fetch, TWO tiers, both
required."** This package builds the **E2E tier** only (`leg_b_web_fetch.py`
— boot via the `live_gate_observe_1000.py` run_server pattern; materialize +
install + trust + select the `web-testing` testing agent (see "Testing-agent
mechanism" for why a bare session + workdir `.clio/mcp.yaml` no longer
proves this -- #1301); handshake shows web ready; PRE-TURN READINESS GATE on
the resolved agent's tools; drive one headless turn calling `web_search`,
`web_fetch(to_file=true)` on a stable official PDF through a configured CLIO
Web Search deployment, and `web_fetch_events` on the returned conversion ID;
verdict JSON to out/). Go = `leg_b_web_fetch.json`: `web_ready` and
`web_tools_match` both true at handshake, `readiness_gate.ready` true, all
three web tool success flags true, `turn_status` terminal.

### Web-fetch qualification rule

A successful HTML or plain-text fetch does **not** qualify the asynchronous
document path. Those formats can complete inline before meaningful task
progress is visible. Leg B must use a PDF, Office document, XML document, or
image that CLIO Web Search converts, and the Web MCP must be launched with a
working `--remote-url` (or `WEB_REMOTE_URL`). The fetch must set
`to_file=true`, expose the conversion ID and progress while running, complete
with Markdown and metadata paths, and be followed by `web_fetch_events` for
the same conversion ID. If any of those observations is missing, preserve the
session as failed evidence and run a new visible qualification session; do not
substitute a fast HTML result.

This is the deployment gate, not a smoke-test preference. A qualifying
PDF/Office/XML/image run must prove all of the following from authoritative
structured results: `web_fetch(to_file=true)` accepted the document, returned
a conversion ID, emitted asynchronous progress, completed with saved Markdown
and metadata outputs, registered the returned files as artifacts, and
`web_fetch_events` returned the lifecycle for that same conversion ID. The
configured `web` MCP declaration must be ready and point at a service with the
document-conversion capacity needed by the selected input. Endpoint discovery
by network scanning and development/homelab addresses hard-coded into the
qualification are forbidden. An HTML fetch alone cannot pass this gate.

### Windows and CI validation policy

On Windows, run focused unit and integration suites first with repository-owned
isolated scratch directories and caches. Never disable, weaken, or add
exclusions to Microsoft Defender to make a suite pass. Host-policy or ACL
failures are reported as failures or explicitly blocked evidence; they are not
converted into skips or green claims. GitHub Actions owns the broad platform
and version matrix. Observe one authoritative CI run at the exact changed head
through completion. A failed, skipped, cancelled, pending, or unrun required
check is not acceptance.

**The WIRE tier** (#1286: "conformance test with a kit-web backend ...
client built through the GATEWAY construction path, assert `fetch` reaches
`tasks/get`; `_NoExtensionClient` negative control adjacent") is a
`tests/test_tools/` pytest addition, out of this package's scope
(`scripts/live_verification/` only) — file as a follow-up slice if not
already covered by `test_mcp_v2_conformance.py`.

**Leg (i) — v1 fleet regression.** See `leg_a_v1_fleet.md` in full. Go = zero
non-benign diffs in the baseline comparison AND earthscope/wildfire live runs
pass.

**Leg (iii)/(iv) — the marketplace deep-research expert works on clio-agent.**
See `leg_d_deep_researcher.md` in full. Go = `test_deep_researcher_web_synthesis`
passes all four matchers, hand-reviewed per GOAL.md.

**Plus (#1286): "exerciser full-surface green."** `leg_c_synthetic_session.py`
covers the task=required (`task_echo`) + MRTR (`guarded_input`) surface
through a REAL session driven through the `v2ex-testing` testing agent
— the piece no existing conformance test drives end-to-end (every existing
exerciser test drives it in-process). Go = `leg_c_synthetic_session.json`:
`v2ex_ready`/`v2ex_tools_match` true at handshake, `readiness_gate.ready`
true, `turn1.pass` true, `turn2.pass` true (or, if the question genuinely
never surfaces — not expected per this package's own tracing, see the
script's docstring — `turn2.requires_interactive` is `true` and that
specific sub-assert is the one accepted as not-yet-provable headlessly; every
other assertion still gates).

## Cost notes

- **Zero-cost**: preflight, both `--plumbing-only` runs, leg A's baseline
  diff.
- **Cheap live**: leg B (one turn), leg C (two turns, same session).
- **Moderate live**: leg A's earthscope/wildfire runs (one session each).
- **Most expensive**: leg D (an unbounded number of dynamically-spawned
  `researcher`/`critic` children).

## HITL / headless-answer finding (leg C, turn 2)

The headless answer route is `POST /v1/sessions/{sid}/questions/
{question_id}/answer` (`gact/routes/sessions.py::answer_user_question`) —
the SAME surface a native `ask_user` question uses. Traced (not assumed):
the exerciser's `guarded_input` task tool returns an `InputRequiredResult`;
the tasks-drive's `_answer_round` (`tools/mcp_tasks.py`) answers it via the
client's elicitation callback; every declared-server client (proxy AND the
capability-keyed direct route alike) is built with the handlers
`agent.py::_build_tool_gateway` captures from `elicitation_correlation.
make_correlated_handlers()`, which resolves to `gact/elicitation_bridge.py::
handle_elicitation` — mints a `UserQuestion` on the standard surface. So leg
C's turn 2 is implemented FULLY headless (poll `GET /v1/sessions/{sid}/
questions?status=pending`, answer via the route above, then wait for the
turn to complete) — no interactive-only gap was found. See `leg_c_synthetic_
session.py`'s module docstring for the full trace.

## Testing-agent mechanism (C1-S6, #1301) -- corrects a wrong assumption

**The wrong assumption this corrects:** legs B/C originally assumed a bare
gact session (`POST /v1/sessions` with no testing agent selected) plus a
workspace `.clio/mcp.yaml` declaration was enough for a real turn to reach a
declared MCP server's tools. Live investigation proved this false: the
bare-session builtin main's toolset is a **hardcoded 4-tool list**
(`agent.py`'s default gateway/catalog build for an unactivated session), so a
tool from a declared server -- however correctly `mcp.yaml` names it -- never
reaches the model's tool list. This is tracked as **#1301** and is now
**deferred to an upstream rework**: the Python builtin main is being
dissolved, so the fix is not "patch the hardcoded list" but "ride the path
that is staying." `GET /v1/mcp/handshake` still reports the server reachable
in this scenario (it probes the declared spec directly, independent of any
session's resolved toolset), which is exactly why the old leg B/C looked
green while the turn itself silently never called the declared tool -- a
false-green trap identical in shape to the cwd-threading gap below.

**The working path:** a testing agent built from plugins -- the same
mechanism every marketplace agent (e.g. `clio/deep-researcher`) uses
(`docs/PLUGINS_AND_FLOWS.md`). Each template under `agents/<name>/` is a small
native marketplace: `ai.iowarp.clio/marketplace.json`, one plugin
`plugins/<name>/` holding the agent's prompt fragment (and, when the leg needs
one, an `mcp.json` written at run time), and an agent class in
`agents/<module>.py` that selects that plugin plus shared plugins from the
default marketplace. A session pinned to that agent resolves its plugins' MCP
servers into the REAL per-turn tool gateway, so a declared server's tools
genuinely reach the model:

- `agents/web-testing/` -- `core` + the default marketplace's `web` plugin
  (`clio-kit mcp-server web`: `web_fetch`, `web_search`, `web_fetch_events`)
  + its own prompt plugin.
- `agents/v2ex-testing/` -- `core` + a plugin declaring the `v2ex` exerciser
  server (`v2ex_task_echo`, `v2ex_guarded_input`, ...).
- `agents/v2ex-avenues/`, `agents/v2-stress/` (also carries `web`),
  `agents/task-testing/` (`workspace`, `shell`) and
  `agents/task-subagent-testing/` (`workspace`, `shell`, `delegation`, plus the
  defined `worker` subagent) follow the same layout.

The agents exist to prove plumbing, not reasoning: their prompt fragments are
terse ("fetch/search exactly as instructed, report results verbatim" / "call
exactly the tool named, report exactly what it returned"). A top-level agent
has no tool allowlist, so it sees every tool its plugins bring; the readiness
gates check that the needed tools are present.

**Materialization (`_common.py::materialize_testing_marketplace`):** the
`v2ex` server's argv carries a per-run resolved script path (`EXERCISER_PATH`)
that must never be a hardcoded drive path committed to source. Each leg copies
its template into its own workspace dir at run time and writes the plugin's
`mcp.json` (`{"command": "python", "args": [<EXERCISER_PATH>]}`). Agent Plugins
1.0.0 requires a bare command name, so `_common.py::boot_server` puts this
interpreter's directory first on the server's `PATH`. JSON argv lists need no
shell quoting. `name=` and `agent_id=` materialize a renamed copy, for a leg
that needs two distinct agents from one template.

**Install, trust and select (`_common.py::install_testing_agent` and
`use_agent`):**

1. `POST /v1/plugins/marketplaces` with `{"source": <materialized root>,
   "name": "lv-<folder>"}`. Re-installing over the same name is idempotent.
2. `POST /v1/plugins/marketplaces/lv-<folder>/trust` -- a local marketplace's
   Python and MCP servers load only after the trust step, and an update whose
   `mcp.json` changed waits for it again, so the helper always trusts. The
   trust summary names the marketplace's one agent; the ref is
   `lv-<folder>/<agent id>`.
3. `PATCH /v1/sessions/{sid}/agent` with `{"ref": <ref>}` resolves the agent
   and pins it to the session.

**Resolved-tools readiness gate (`_common.py::resolved_agent_tools`):**
`GET /v1/sessions/{sid}/agent` returns the pinned resolution the turn path
builds from (its built-in tools by name and its plugins' MCP servers); the
servers' tools are listed by `GET /v1/mcp/handshake?session_id=<sid>` and
namespaced `<server>_<tool>`. The helper returns `{agent_id: [tools]}`. Legs
assert this BEFORE binding any provider (the readiness gate `--plumbing-only`
covers with zero LM spend):

- Leg B needs `{"web_fetch", "web_fetch_events", "web_search"}`.
- Leg C needs `{"v2ex_task_echo", "v2ex_guarded_input"}` (mind the
  `<namespace>_<tool>` naming).

**The workspace `.clio/mcp.yaml` decision:** dropped for legs B/C. The plugin
declares the server (that is the working path); a parallel workspace
`mcp.yaml` declaration would be redundant and would reintroduce the
cwd-threading gap documented below for no benefit.

## Constraint discovered: mcp.yaml command-string quoting on Windows

`tools/mcp_config.py`'s string-command form (`name: <command>`) parses via
`shlex.split(text)` — POSIX mode, even on Windows. An UNQUOTED absolute
Windows path (backslash-separated) is silently mangled: `shlex.split` treats
each backslash as an escape character and strips it, turning e.g.
`D:\Libraries\Documents\...\mcp_exerciser.py` into garbage no launcher can
resolve. Verified directly against `shlex.split` (the exact call `mcp_config.
py::_spec_from_string` makes). Fix: double-quote every token that may carry a
filesystem path (`"<python>" "<path>"`) — `shlex.split` preserves backslashes
inside double-quoted tokens correctly, confirmed on this box. `_common.py::
quoted_command` implements this; leg C uses it for the exerciser's
`<python> <script>` declaration. No wrapper `.cmd`/`.sh` script was needed.
The constraint applies only to `mcp.yaml` string commands; plugin servers are not affected: their `mcp.json` carries a JSON argv list,
which is never shell-split.

## Constraint discovered: workspace `.clio/mcp.yaml` cwd threading gap

`GET /v1/mcp/handshake` correctly threads a workspace's `root_path` as the
`cwd` used to discover its `.clio/mcp.yaml` (`gact/routes/mcp_specs.py::
declared_mcp_specs` -> `load_mcp_servers(cwd=...)`). The ACTUAL per-turn tool-
gateway build (`agent.py::_build_tool_gateway`) does not: it calls
`load_mcp_servers(plugin_servers=...)` with no `cwd=` at all, so
workspace/user `.clio/mcp.yaml` discovery silently falls back to
`Path.cwd()` — the gact SERVER PROCESS's OS working directory, not the HTTP
workspace's `root_path`. `scripts/live_gate_1031.py`'s P3/composed gates
already work around this (writing `.clio/mcp.yaml` at `<repo>/.clio/mcp.yaml`
and always booting with `cwd=str(REPO)`, so the two coincide); `_common.py`
generalizes the workaround (`boot_server(..., cwd=)` + `write_mcp_yaml`
into that SAME directory) so every leg here is immune. Left as a real,
reportable gap for the campaign (a leg that boots the server with one cwd and
writes `.clio/mcp.yaml` into a different directory would see the handshake
pass while the real turn's tool call fails to find the namespace — a
false-green trap) — not fixed here, since this package's scope is
`scripts/live_verification/` only.

**Update (C1-S6, #1301):** legs B and C no longer write a workspace
`.clio/mcp.yaml` at all (see "Testing-agent mechanism" above), so this gap
no longer applies to them by construction, not by convention: a plugin's
servers come from the session's pinned resolution and are passed to
`load_mcp_servers(plugin_servers=...)` already declared, and plugin stdio
servers start in the session workspace (unlike the bare `Path.cwd()` fallback
that only affects workspace/user `mcp.yaml` discovery). The gap itself is unfixed and still real for any future leg that
declares a workspace `mcp.yaml` directly — `write_mcp_yaml`/`boot_server(...,
cwd=)` remain in `_common.py` as the documented workaround for that case.
