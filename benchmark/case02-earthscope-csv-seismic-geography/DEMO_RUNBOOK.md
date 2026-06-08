# EarthScope GNSS — Demo Runbook (CLIO composable agent)

_Prepared overnight 2026-06-08. Honest status + how to run the demo._

## The story (what this demonstrates)

CLIO is now a **composable** agent framework: the EarthScope case lives **entirely** in a
marketplace pack (markdown experts with typed DSPy signatures) over **clio-kit MCP servers**,
with **zero EarthScope/NDP code in clio-agent core**. The same core runs any case.

What got done this session:
- **Deleted the regex inference layer** (`_infer_ndp_*`, ~2,780 lines) that used to backfill
  workflow state — the case is now driven by the agent emitting **typed `workflow_state`**.
- **Fixed the continuation mechanism** so nested child state bubbles to the parent's
  continuation contracts (generic, unit-tested).
- **Domain tools moved to clio-kit MCPs** (`ndp` pure data access, `geo` incl. `geocode`,
  `sac`, `seismic`, `terrain`, `plot` timeseries, `pandas` profile) — PR #287.
- **Grounding guards** (generic): drop fabricated state sections; rewrite/neutralize any
  answer path not on disk. _(Up for review — see Honest status.)_

## Run the demo (live)

The gact server is on `http://127.0.0.1:17960` (argonne_metis / gpt-oss-120b; Globus token present).
Restart if needed:
```
cd /home/jcernuda/clio-agent
CLIO_ALLOWED_ROOTS="$PWD:/tmp" nohup uv run uvicorn clio_agent.gact.app:app --host 127.0.0.1 --port 17960 > /tmp/gact_demo.log 2>&1 &
# wait for /v1/health
```

**Demo prompt** (blueprint `earthscope-gnss-region`):
> "Explore recent seismic/geodetic activity around the San Diego area. Resolve the requested
> geography, find a public EarthScope/NDP GNSS station time-series, analyze it, and produce a plot."

**Expected (a clean grounded pass):** route `geospatial → data → analysis → visualization → synthesis`;
it resolves San Diego, ranks the real EarthScope station catalog by distance, stages the nearest
station's **real ~50 MB GNSS CSV** (e.g. `P475` @9.5 km), profiles it, renders a **real PNG**, and
synthesizes — citing only the real staged station/paths.

**Honest-negative demo (impressive + honest):** ask the same about **"Chicago, IL"** → the agent
correctly reports **no EarthScope GNSS station within the region** (nearest is ~2,400 km away in the
western US) instead of fabricating one. This is the "grounded, not hallucinated" story.

Quick CLI smoke (no TUI):
```
cd /home/jcernuda/clio-agent && CLIO_RUN_LIVE=1 ES_REGION="San Diego area" uv run python tmp/es_sampler.py
```

## Honest status (tell the audience / for morning review)

- **Works + grounded:** on covered regions it stages **real** station data and plots it; honest
  no-coverage on Chicago/Sahara.
- **No fabrication.** The one residual fabrication mode (a path-doubling citation, ~1/12 in the
  overnight 12-run sample) is now **closed** by a generic grounding guard (collapses a malformed
  cited path to the real embedded artifact); confirmed live + unit-tested. Measured clean rate
  before the fix was **6/12 ≈ 0.50** (the rest honest incompletes); the path-doubling case is now a
  clean pass, so practical clean-rate is a bit higher.
- **Reliability is ~0.5–0.6 on gpt-oss-120b**, NOT the ≥0.8 Done bar. Remaining failures are
  **honest incompletes** (model sometimes stages then stalls before profile/plot) — small-model
  variance, not fabrication. **If a live run stalls, just re-run** (it's stochastic).
- **Backup artifacts** (real, from clean runs) if a live run stalls during the demo:
  - `~/.clio/artifacts/ndp-staging/P473.PW.LY_.00_plot.png` (151 KB)
  - `~/.clio/artifacts/ndp-staging/SEAT.PW.LY_.00_plot.png` (154 KB)
  - `~/.clio/artifacts/ndp-staging/PKRD.CI.LY_.20_plot.png` (137 KB)
  - matching ~50 MB CSVs under `~/.clio/artifacts/ndp-staging/`
- **For review:** the two core grounding-guards (`_drop_fabricated_workflow_state_sections`,
  `_ground_fabricated_local_artifact_paths`) are framework-side correction of small-model
  fabrication — keep / generalize the `*_selection` denylist / or trade for pure agent-reliability.

## Second case: wildfire — WORKING (second demoable case)
The `wildfire-smoke-impact-review` pack runs on the same composable core and now **passes its
acceptance test** (`tests/test_real_cases/test_wildfire_case.py` → pass): it selects a live fire
(WFIGS), resolves the fire perimeter into a region, fuses smoke + air-quality, computes the
downwind population overlap, makes a **grounded impact decision** (present/honest-null), and renders
an **impact map PNG** — all via typed `workflow_state` (region/fire/impact emitted by the agent, not
runtime logic). Same fix pattern as EarthScope (typed-state emission), **pack-only** (zero new core).
Run it:
```
cd /home/jcernuda/clio-agent && CLIO_RUN_LIVE=1 uv run pytest tests/test_real_cases/test_wildfire_case.py --provider argonne_metis -o addopts="" -p no:cacheprovider -q
```
Reliability not statistically measured (like EarthScope, ~stochastic on gpt-oss-120b); a single
clean pass is confirmed. **Demo as the second domain** — same composable agent, different science case.

## Two-case story for the demo
Same clean core + clio-kit MCPs + typed-state experts, two real science domains:
1. **EarthScope GNSS** — geography → NDP discovery → stage real station CSV → profile → plot (+ honest no-coverage).
2. **Wildfire smoke impact** — fire discovery → region → smoke/AQ fusion → downwind overlap → impact decision → map.
Neither has any case-specific code in clio-agent core.

## Tip for the most reliable live demo
Use **San Diego** or **Los Angeles** (densest EarthScope coverage). Have the Chicago honest-negative
ready as the second beat. If a covered run stalls (honest incomplete), re-run once.
