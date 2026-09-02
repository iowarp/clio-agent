# MCP client unification — live verification RUNBOOK (#1286, C1-S6)

**Status:** PREPARED, not yet run. Per the 2026-09-02 owner reorder recorded in
`docs/design/mcp-client-unification-2026-08.md`'s "Campaign 1 status" section:
the full live verification is prepared NOW on the campaign branch and runs on
**owner go-ahead**, before C1-S3..S5 land (not after). Nothing in this
package invokes a model — every script defaults to (or has) a
`--plumbing-only` mode; the live legs below spend real LM tokens only when
explicitly run by a human with the go-ahead.

Study first if extending this package: `docs/design/
mcp-client-unification-2026-08.md` (plan of record), `gh issue view 1286`
(the gate spec this RUNBOOK maps to), `scripts/live_gate_observe_1000.py` +
`scripts/live_gate_1031.py` (the house live-gate pattern these scripts
follow), `tests/test_tools/mcp_exerciser.py` (leg C's synthetic server),
`tests/test_real_cases/conftest.py` + `clio_sut.py` (the agent-test harness
legs A/D use).

## Order (cheapest signal first)

1. **Preflight** — no server, no model. Catches PATH/YAML/binary-presence
   gaps before anything else runs.
2. **Leg (ii) / B — web fetch smoke** — one server boot, one turn. The
   cheapest LIVE signal and the exact #1274 repro; if this is red, stop —
   legs C/A/D downstream would misattribute the same plumbing defect.
3. **Leg C — synthetic session** — one server boot, two turns (task=required
   + MRTR). Proves the generic client against the FULL v2 surface the
   exerciser forces, not just clio-kit's subset.
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
— "write workdir `.clio/mcp.yaml` ... boot via the `live_gate_observe_1000.py`
run_server pattern, handshake shows web ready, drive one headless turn
calling `web_fetch` on a stable URL, verdict JSON to out/"). Go =
`leg_b_web_fetch.json`: `web_ready` and `web_tools_match` both true at
handshake, `web_fetch_succeeded` true, `turn_status` terminal.
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
through a REAL session — the piece no existing conformance test drives
end-to-end (every existing exerciser test drives it in-process). Go =
`leg_c_synthetic_session.json`: `v2ex_ready`/`v2ex_tools_match` true at
handshake, `turn1.pass` true, `turn2.pass` true (or, if the question genuinely
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
