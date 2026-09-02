# Leg (iii)/(iv) runbook: the marketplace deep-research expert on clio-agent

**#1286 text:** "new benchmark case (GOAL.md scaffolding per grind-clio-case
conventions) + `tests/test_real_cases/test_deep_researcher_case.py` using
`marketplace_source` (local-path install of `external/clio-agent-marketplace/
deep-researcher`, no network). Matchers: at least one successful task-backed
`web_fetch` + one `web_search` in tool_calls, markdown report artifact in the
registry, the critic's independent fetch (the pack's own validity condition).
RED today by construction — it IS the honest #1274 repro."

This leg's harness is already built and merged
(`benchmark/case14-deep-researcher-web/` + `tests/test_real_cases/
test_deep_researcher_case.py`, per `docs/design/
mcp-client-unification-2026-08.md`'s "Campaign 1 status" note and the
issue-#1286 comment: "Leg (iii) scaffolding MERGED ... The live run stays
parked for this gate, after C1-S2..S5"). This file is the precise runbook,
not a new script.

## 0. Preflight + leg (ii) first

This leg's whole point is proving the deep-researcher pack's `researcher`/
`critic` children reach `web_fetch`/`web_search` through the SAME generic
plumbing leg (ii) proves directly. Run preflight and leg (ii) green FIRST —
a leg (ii) failure here would misattribute a plumbing defect to the pack.

```
uv run python scripts/live_verification/preflight.py
uv run python scripts/live_verification/leg_b_web_fetch.py --provider claude_code --model sonnet
```

## 1. The live run

```bash
CLIO_RUN_LIVE=1 uv run pytest \
    tests/test_real_cases/test_deep_researcher_case.py \
    --provider claude_code --model sonnet -o addopts="" -p no:cacheprovider -q
```

Guardrail cell is `claude_code`/`sonnet` by default in the test itself (NOT
the NDP-case default `argonne_metis`) — per GOAL.md's "Case-specific
deviations" and the live-tests-use-claude/codex convention; override via
`CLIO_AGENTTEST_CELLS` if genuinely needed.

`CLIO_DEEP_RESEARCHER_TIMEOUT_S` (default `1800`) is the hard ceiling; the
progress watchdog governs otherwise (dynamic fan-out + an independent critic
pass can run long — see the test module docstring).

## 2. Where evidence lands

- **Trace**: `.grind/traces/<slugified node id>/` (the `gact_server` fixture's
  `CLIO_SEMANTIC_TRACE_PATH`) plus the SUT's own per-run JSONL at
  `benchmark/case14-deep-researcher-web/runs/acceptance-<provider>-<model>.jsonl`
  (`clio_sut.ClioAgent._resolve_trace_path`).
- **Server log**: `.grind/logs/<slug>.gact.log`.
- **Report artifact**: written inside the test's isolated `tmp_path` workdir
  (never the repo — the test's own hygiene assertion enforces this), and
  registered in the artifact registry (`GET /v1/sessions/{id}/artifacts`).

## 3. Green means (per GOAL.md's Done criteria, gate bar)

1. `test_deep_researcher_web_synthesis` passes: the coordinator actually
   delegated to >=1 child (`run.extra["child_sessions"]` non-empty).
2. `web_fetch_succeeded` — >=1 SUCCESSFUL `web_fetch` anywhere across the
   coordinator + every direct child session.
3. `web_search_succeeded` — >=1 SUCCESSFUL `web_search`.
4. `markdown_report_artifact` — a real `.md` file (>256 bytes) landed in the
   registry, inside the isolated workdir.
5. `critic_independent_evidence` — the CRITIC child session's own tool calls
   (attributed via the child `Session`'s `agent.id` field, not a proxy count)
   include >=1 successful `web_search`/`web_fetch` — the pack's own stated
   validity condition for its critic pass.

**Hand-review after a green run** (per GOAL.md, "no human in the loop for
acceptance, but the agent reads the trace"): read the trace and confirm the
critic's fetch is GENUINELY independent (not the same source the researcher
already fetched, re-served from cache) and the report's citations actually
map to what was fetched — a passing matcher grid is necessary, not
sufficient, per GOAL.md's own "no fake data, no mocks" method note.

## 4. If RED

Per GOAL.md's own framing, this case is "RED today by construction" pre-S6 —
a failure here before C1-S3..S5 land is the expected, honest state, not a
surprise. Once C1-S3..S5 have landed on this branch, a failure is real
signal: read the trace (per CLAUDE.md superseding principle #4 — read the
FULL trace, not the summary), hypothesize ONE cause, and determine whether it
is a genuine clio-agent plumbing defect (fix here) or a pack defect (file
upstream in the marketplace repo — GOAL.md's branching section is explicit
that a pack defect never gets patched from this side).

## Cost note

This leg spends real LM tokens across MULTIPLE sessions (the coordinator plus
however many `researcher`/`critic` children it dynamically spawns) — the
most expensive leg in this package. Run it last.
