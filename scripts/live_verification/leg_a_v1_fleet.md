# Leg (i) runbook: v1 fleet regression

**#1286 text:** "agent-test harness (`tests/test_real_cases/`, `gact_server`
fixture :17960) on earthscope + wildfire packs (+ data-semantics for
hdf5/parquet breadth); canonical fleet list = `scripts/provenance_qualification/
Dockerfile:13-16` (geo ndp pandas plot hdf5 parquet web); handshake-row diff vs
the develop baseline. Preflights (`runtime/mcp_launcher.py` probes) so
PATH/YAML issues never score as protocol failures."

This leg already has its harness (`tests/test_real_cases/` + `scripts/
mcp_v1_baseline.py`); this file is the precise runbook, not a new script.

## 0. Preflight first

```
uv run python scripts/live_verification/preflight.py
```

Green here means PATH/YAML issues can never masquerade as a protocol
failure below (the whole point of running it first).

## 1. Baseline diff (schema/handshake breadth across the WHOLE fleet)

The fleet (`geo ndp pandas plot hdf5 parquet web`) covers hdf5/parquet
breadth at the schema + handshake level directly — this is the fastest,
cheapest signal and needs no live model turn.

```
uv run python scripts/mcp_v1_baseline.py \
  --out out/live-verification/mcp_v1_baseline-c1s6.json
```

Then diff against the pre-campaign baseline:

```
git diff --no-index \
  tests/fixtures/mcp_v1_baseline/baseline-develop-96d5bdcc.json \
  out/live-verification/mcp_v1_baseline-c1s6.json
```

**Expected benign diffs (green even if these differ):**
- `meta.commit`, `meta.captured_at` — always differ (this run's own stamp).
- `meta.clio_kit` — differs ONLY if the installed clio-kit version moved
  since the develop baseline was captured; the version string itself is the
  explanation, not a defect.
- `declared_tools.*` schema digests — MUST be byte-identical for every v1
  server (`geo ndp pandas plot hdf5 parquet`) unless `meta.clio_kit` moved
  (a clio-kit version bump can legitimately change a tool's own schema). Any
  digest change with `meta.clio_kit` UNCHANGED is a real regression — C1-S1's
  capability-keyed routing must be byte-identical for v1 servers.
- `handshake.*` rows — `reachable`/`tools`/`tools_count`/`transport`/
  `protocol_version` must match exactly for the v1 servers. `web`'s row is
  informational here (it is v2, not part of the byte-identical contract) —
  its own conformance is leg (ii).

Any non-benign diff is a FAIL — do not proceed to the live agent-test runs
below until it is understood.

## 2. Live agent-test runs (earthscope, wildfire, data-semantics)

All three follow the same `gact_server` fixture pattern
(`tests/test_real_cases/conftest.py`, dedicated port `:17960`, one CLEAN
server per cell). Guardrail cell for a subscription-safe run: `claude_code` /
`sonnet` (matches case14's own convention, and CLAUDE.md's live-tests-use-
claude/codex rule — never point `--matrix`/local `lm_studio` cells at this
box unattended).

```bash
# earthscope
CLIO_RUN_LIVE=1 uv run pytest \
  tests/test_real_cases/test_earthscope_case.py \
  --provider claude_code --model sonnet -o addopts="" -p no:cacheprovider -q

# wildfire
CLIO_RUN_LIVE=1 uv run pytest \
  tests/test_real_cases/test_wildfire_case.py \
  --provider claude_code --model sonnet -o addopts="" -p no:cacheprovider -q
```

**data-semantics (hdf5/parquet breadth, agent-driven):** no committed
`tests/test_real_cases/test_data_semantics_*.py` file exists yet (confirmed
by search — only earthscope/wildfire/case13/case14 have real-case test
files). The marketplace pack itself IS present and installable
(`external/clio-agent-marketplace/data-semantics`, declares
`hdf5`/`parquet`/`pandas`/`plot` exactly like the canonical fleet). Two
options, in order of preference:

1. **Preferred — author a thin real-case test** the same shape as
   `test_deep_researcher_case.py` (blueprint_id=`data-semantics`,
   `marketplace_source=<repo>/external/clio-agent-marketplace/data-semantics`,
   a prompt that exercises an hdf5 inspect + a parquet read), gated
   `CLIO_RUN_LIVE`/`live`/`real_case` exactly like its siblings. This is
   scoped OUTSIDE this prep slice (touches `tests/test_real_cases/` +
   `benchmark/`, not `scripts/live_verification/`) — file it as a follow-up
   if the owner wants it before C1-S6, or accept the schema/handshake breadth
   from step 1 as sufficient for "breadth" (the #1286 text pairs
   hdf5/parquet with "breadth", not "agent behavior").
2. **Ad hoc** — drive it through the SAME `gact_server` fixture pattern by
   hand (a throwaway pytest function or a REPL session against a locally
   booted server) using `clio_sut.ClioAgent` directly:
   `agent.run({"task": ..., "blueprint_id": "data-semantics",
   "marketplace_source": "<repo>/external/clio-agent-marketplace/
   data-semantics", "workdir": <tmp dir>})`.

## 3. Green means

- Step 1's diff has zero non-benign changes.
- Steps 2's earthscope/wildfire tests pass (all matchers).
- data-semantics breadth is either agent-proven (option 1) or accepted via
  step 1's schema/handshake coverage (option 2).

## Cost note

Steps 2 spend real LM tokens on a subscription provider (claude_code/sonnet
by default) — one live session per pack. Step 1 spends none.
