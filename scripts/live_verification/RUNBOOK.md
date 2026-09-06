# MCP client unification — live verification RUNBOOK (#1286, C1-S6)

**Status:** PREPARED, not yet run live. Per the 2026-09-02 owner reorder recorded in
`docs/design/mcp-client-unification-2026-08.md`'s "Campaign 1 status" section:
the full live verification is prepared NOW on the campaign branch and runs on
**owner go-ahead**, before C1-S3..S5 land (not after). Nothing in this
package invokes a model — every script defaults to (or has) a
`--plumbing-only` mode; the live legs below spend real LM tokens only when
explicitly run by a human with the go-ahead. Legs B and C's `--plumbing-only`
mode HAS been run (zero LM spend) building this package's C1-S6 slice and
passes end-to-end through the testing-agent pack mechanism below -- see
"Testing-agent pack mechanism" for both verdicts.

Study first if extending this package: `docs/design/
mcp-client-unification-2026-08.md` (plan of record), `gh issue view 1286`
(the gate spec this RUNBOOK maps to), `gh issue view 1301` (the bare-session
builtin-main defect legs B/C now route around, see below),
`scripts/live_gate_observe_1000.py` +
`scripts/live_gate_1031.py` (the house live-gate pattern these scripts
follow), `tests/test_tools/mcp_exerciser.py` (leg C's synthetic server),
`tests/test_real_cases/conftest.py` + `clio_sut.py` (the agent-test harness
legs A/D use, and the install+activate call shapes legs B/C's testing-agent
path now mirrors), `external/clio-agent-marketplace/deep-researcher/` (the
real marketplace pack whose `AGENT.md` shape `agents/web-testing/` and
`agents/v2ex-testing/` mirror).

## Order (cheapest signal first)

1. **Preflight** — no server, no model. Catches PATH/YAML/binary-presence
   gaps before anything else runs.
2. **Leg (ii) / B — web fetch smoke** — one server boot, one turn, driven
   through the `web-testing` Agent Blueprint pack (not a bare session --
   see "Testing-agent pack mechanism" below). The
   cheapest LIVE signal and the exact #1274 repro; if this is red, stop —
   legs C/A/D downstream would misattribute the same plumbing defect.
3. **Leg C — synthetic session** — one server boot, two turns (task=required
   + MRTR), driven through the `v2ex-testing` Agent Blueprint pack. Proves
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
install + activate the `web-testing` Agent Blueprint pack (see "Testing-agent
pack mechanism" for why a bare session + workdir `.clio/mcp.yaml` no longer
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
through a REAL session driven through the `v2ex-testing` Agent Blueprint pack
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

## Testing-agent pack mechanism (C1-S6, #1301) -- corrects a wrong assumption

**The wrong assumption this corrects:** legs B/C originally assumed a bare
gact session (`POST /v1/sessions` with no Agent Blueprint activated) plus a
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

**The working path:** an Agent Blueprint pack -- the same mechanism every
real marketplace pack (e.g. `deep-researcher`) uses. Its `AGENT.md`
frontmatter declares `mcp_servers:`, and each expert `.md` under `experts/`
declares `tools:` naming the exact `<namespace>_<tool>` names the declared
server(s) expose. Activated on a session, `agent.py::_build_tool_gateway`
resolves the blueprint's `mcp_servers` into the REAL per-turn tool gateway
(`_discover_pack_servers(blueprint_id, cwd=cwd)` -> `load_mcp_servers(
pack_servers=...)`) -- a declared server's tools genuinely reach the model.
Legs B and C now ride this path via two minimal, purpose-built single-expert
packs kept in this directory:

- `agents/web-testing/` -- declares `mcp_servers: {web: clio-kit mcp-server
  web}` and a `main` expert with `tools: [web_fetch, web_search,
  web_fetch_events]`.
- `agents/v2ex-testing/` -- declares a `v2ex` server (placeholder command in
  the committed template) and a `main` expert with `tools: [v2ex_task_echo,
  v2ex_guarded_input]`.

Both packs exist to prove plumbing, not reasoning: their `main` expert's
system prompt is terse ("fetch/search exactly as instructed, report results
verbatim" / "call exactly the tool named, report exactly what it returned").

**Materialization (`_common.py::materialize_testing_pack`):** `AGENT.md`
frontmatter is a static file, but the `v2ex-testing` pack's server command
carries a per-run resolved absolute interpreter + script path
(`sys.executable` + `EXERCISER_PATH`) that must never be a hardcoded drive
path committed to source. Each leg copies its pack template into its own
workspace dir at run time and rewrites ONLY the `mcp_servers` block (parsed
and re-emitted via `yaml.safe_load`/`yaml.safe_dump`, matching `write_mcp_
yaml`'s approach) before installing it. `web-testing`'s committed
`mcp_servers.web` is already a real, portable command (`clio-kit` resolves on
PATH), so its materialization is a no-op rewrite -- routed through the same
function anyway, so there is ONE mechanism instead of two.

**Install + activate (route shapes verified against `gact/routes/
blueprints.py` and mirrored from `tests/test_real_cases/clio_sut.py`):**

1. `POST /v1/agent-blueprints/install` with `{"source": <local pack root
   path>, "scope": "workspace", "workspace_id": <wsid>}` -> `201` with
   `{"installed": [{"id": <blueprint_id>, ...}], "skipped": [...]}`. The
   installer overwrites an existing install at the same id/scope, so
   re-running a leg needs no manual uninstall first.
2. `POST /v1/sessions/{sid}/agent-blueprint` with `{"blueprint_id": <id>}` --
   note the verb is **POST, not PUT** (this RUNBOOK's build brief named PUT;
   the actual route in `gact/routes/blueprints.py::set_session_agent_
   blueprint`, and the exact call `clio_sut.py` makes, is POST).

**Resolved-tools readiness gate (`_common.py::resolved_agent_tools`):**
`GET /v1/agents?session_id=<sid>&workspace_id=<wsid>` -> `{"agents": [{"id":
"main", "tools": [...], ...}]}`. This is the SAME seam `gact/agents/
resolution.py::_runtime_active_agent_blueprint_rows` shares with the real
turn path (#770 C1: the route and the executing agent can never disagree on
what an agent resolves to), so it is genuine pre-turn PROOF a testing-agent
pack's declared `tools:` reached the session's active agent -- not merely
that its frontmatter parsed. Both legs assert this BEFORE binding any
provider (the readiness gate `--plumbing-only` now covers with zero LM
spend):

- Leg B needs `{"web_fetch"}` in `main`'s resolved tools.
- Leg C needs `{"v2ex_task_echo", "v2ex_guarded_input"}` (mind the
  `<namespace>_<tool>` naming).

**The workspace `.clio/mcp.yaml` decision:** dropped for legs B/C. The pack
frontmatter already declares the server (that is the working path); a
parallel workspace `mcp.yaml` declaration would be redundant and would
reintroduce the cwd-threading gap documented below for no benefit. Leg B now
proves "pack-declared server + task=required fetch" -- the `deep-researcher`
shape, the #1274 user's actual shape.

**Both `--plumbing-only` verdicts, run live building this slice (zero LM
spend):**

```
leg_b_web_fetch.json: handshake {web_ready: true, web_tools_match: true},
  readiness_gate {needed_tools: ["web_fetch"],
                   main_tools: ["web_fetch", "web_fetch_events", "web_search"],
                   ready: true},
  pass: true

leg_c_synthetic_session.json: handshake {v2ex_ready: true, v2ex_tools_match: true},
  readiness_gate {needed_tools: ["v2ex_guarded_input", "v2ex_task_echo"],
                   main_tools: ["v2ex_guarded_input", "v2ex_task_echo"],
                   ready: true},
  pass: true
```

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
The SAME constraint governs an Agent Blueprint's `mcp_servers` frontmatter
value (`agent_blueprints.py`'s `mcp_servers` mapping feeds the identical
`load_mcp_servers` -> `_spec_from_string` -> `shlex.split` call) — leg C's
`v2ex-testing` pack materialization reuses `quoted_command` for exactly this
reason.

## Constraint discovered: workspace `.clio/mcp.yaml` cwd threading gap

`GET /v1/mcp/handshake` correctly threads a workspace's `root_path` as the
`cwd` used to discover its `.clio/mcp.yaml` (`gact/routes/mcp_specs.py::
declared_mcp_specs` -> `load_mcp_servers(cwd=...)`). The ACTUAL per-turn tool-
gateway build (`agent.py::_build_tool_gateway`) does not: it calls
`load_mcp_servers(pack_servers=pack_servers)` with no `cwd=` at all, so
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
`.clio/mcp.yaml` at all (see "Testing-agent pack mechanism" above), so this
gap no longer applies to them by construction, not by convention: a
blueprint's `mcp_servers` resolves through `_discover_pack_servers(
blueprint_id, cwd=cwd)` into an ALREADY-RESOLVED spec dict BEFORE it ever
reaches the `load_mcp_servers(pack_servers=...)` call this section flags, and
that `cwd` is correctly threaded from the per-workspace gateway build (unlike
the bare `Path.cwd()` fallback that only affects workspace/user `mcp.yaml`
discovery). The gap itself is unfixed and still real for any future leg that
declares a workspace `mcp.yaml` directly — `write_mcp_yaml`/`boot_server(...,
cwd=)` remain in `_common.py` as the documented workaround for that case.
