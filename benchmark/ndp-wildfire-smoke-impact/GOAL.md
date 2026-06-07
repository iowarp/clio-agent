# Goal: Wildfire Downwind Smoke & Air-Quality CLIO Case

## Objective

Turn the **grounded** wildfire downwind-impact question into a working CLIO
Agent Blueprint case that runs through the normal orchestrator against the live
NDP catalog and a live provider (ALCF Sophia), produces a layered map artifact
via the new `geo` MCP, and is accepted only after trace + artifact + provenance
review. The question, datasets, and a hand-built solution already exist in this
folder (`README.md`, `manual-solution/`, `manual-solution/DATASETS.md`) — this
goal is the build-and-prove phase, not a re-grounding phase.

The case prompt is natural and names no expert, tool, or schema:
> see `prompt.txt`.

## Non-negotiable method (do not regress on these)

- **No gates, no fake data, no mocks in `src/`.** A passing JSONL counter or a
  single happy-path unit test is not a pass. Acceptance = a real live session
  whose trace, per-branch evidence, artifact, and provenance are inspected by
  hand and match the prompt intent.
- **Routing is DSPy-typed, not string-matching.** Expert handoffs come from
  typed structured blueprint outputs / workflow state, never from
  `when_request_contains: "<city>"` or forced mandatory branches. The case must
  generalize across cities/regions and across "active smoke" vs "contained, no
  impact" without benchmark-specific string contracts (see issues #646/#648).
- **Difficulty is intrinsic.** The fire is selected by *downwind impact*
  (smoke footprint overlapping populated AQI monitors), not by perimeter size.
  The contained / no-smoke outcome is a correct, reachable answer — not a fail.
- **Work in the session workspace** (artifacts under the workspace, not stray
  `.local/` paths — issue #645).

## Testing harness (agent-test)

Use Jaime's `~/agent-test` pytest plugin as the run+acceptance harness. Division
of labor: **I (the agent) monitor semantics — read traces, discover failures,
make design decisions; agent-test monitors the data pathways — assertions on
tools, trajectory, artifacts, routing; I iterate to improve both** (agent
prompts, the geo tool, and the matchers). Every failure I find by reading a
trace becomes a new matcher; the green grid never substitutes for the trace read.

Sequencing (Jaime, 2026-06-06): **first iterate the case to productive** on a
single cell with trace review. DEFER agent-test's model-matrix and prompt-search
features until the case works and the harness is in place — those are the payoff
phase, not the setup phase.

CLIO live-run recipe (from `scripts/run_demo_benchmark.py`; the SUT `invoke`
reuses it): start the gact server (`uvicorn clio_agent.gact.app:app`), then
`GET /v1/health` → `GET/POST /v1/workspaces` → `POST /v1/agent-blueprints/install`
{source, scope:workspace, workspace_id} → `POST /v1/sessions` →
`POST /v1/sessions/{id}/agent-blueprint` {blueprint_id} → post the prompt turn →
read session messages + children as the trace → normalize into agent-test `Run`.

## Priority order

1. **Wire the `geo` tool into clio-agent.** Register the clio-kit `geo` MCP
   (`render_feature_map`) in the clio-agent tool catalog/gateway so a blueprint
   expert can call it. Add focused tests for tool registration and a render
   smoke test. (Acquisition tool `ndp_query_arcgis_features` already exists.)
2. **Author the marketplace pack** `wildfire-smoke-impact-review` (template:
   `seismic-waveform-review`). Use the **domain-grouped topology** — the one
   EarthScope converged on (final pushed-registry r111 is domain-grouped; the
   depth/linear variant was iterated r1–r63 and was where the deterministic /
   forced-handoff pain lived). NOT a linear chain.

   ```
   main
   ├─ geography                 resolve region -> bbox + provenance
   ├─ data        (acquisition domain; breadth sub-experts, no forced order)
   │   ├─ fire_discovery        active perimeters
   │   ├─ smoke_forecast        smoke polygons over region
   │   └─ air_quality           AirNow monitors over region
   ├─ analysis    (impact domain)
   │   └─ downwind_impact       smoke∩monitors overlap -> pick impactful fire,
   │                            rank affected communities
   ├─ visualization             render_feature_map -> map
   └─ synthesis                 brief + caveats (incl. honest no-impact)
   ```

   `main` re-decides after each domain returns, routing on typed workflow state
   (like EarthScope's `data x6 / main x5 / analysis x5` re-entrant pattern) —
   not on path position. The **impact selector lives in `analysis`'s reasoning
   over typed acquisition state, never as a routing string**, and the
   contained/no-smoke path is just `analysis` returning null-impact -> `main` ->
   `synthesis`. Each expert gets a real domain prompt (500+ words), a typed
   signature, and 5–7 curated tools. Pin/install into the default registry the
   same way EarthScope is.
3. **Stand up the agent-test harness + first live run.** Write a CLIO `SUT`
   adapter (the recipe above → normalized `Run`) and a wildfire acceptance test
   (canonical matchers for tools/trajectory/artifact + `extra` matchers for
   impact-selector-not-size and no-forced-routing). Validate the wiring on the
   fake cell (fast), then do ONE live ALCF Sophia + live NDP run. Capture the
   trace to `runs/`, render the map, and review by hand: did it select an
   impactful fire, fuse three live sources, and brief the worst-affected
   communities with caveats? Single cell only — no matrix/sampling yet.
4. **Grind to acceptance.** Iterate (r1…rN, traces in `runs/`) on the blueprint
   prompts/state until a reviewed run is benchmark-clean under mutated geography
   (different cities/regions) and under the contained/no-smoke case. Encode each
   regression found during review as an agent-test matcher / unit test
   (state-space, not one happy path): selector edge cases, empty smoke, empty
   monitors, malformed feature responses, oversized responses, missing artifact
   path. Only once productive: turn on agent-test's model-matrix and prompt
   search (the payoff phase).
5. **Finalize the case spec.** Update this folder's `README.md` to the accepted
   contract; keep all run logs in `runs/`, never in the spec.

## Branching

- clio-kit: `feat/geo-mcp-server` (the new geo MCP — already built locally;
  land it).
- clio-agent: `feat/wildfire-geo-tool-wiring`, then
  `feat/wildfire-smoke-impact-case`.
- marketplace: `feat/wildfire-smoke-impact-pack`.
- Rebase `develop`/`main` into open branches after each merge.

## Done criteria

- `geo` MCP merged in clio-kit with passing tests.
- `wildfire-smoke-impact-review` pack in the marketplace, loadable through the
  normal registry/blueprint path (no privileged native experts).
- At least one reviewed live ALCF Sophia + live NDP run that:
  selects an impact-driven fire, fuses fire + smoke + AirNow, renders a verified
  non-empty map whose layers match the briefed claims, and produces an audited
  synthesis with caveats.
- At least one reviewed run of the contained / no-significant-impact path that
  correctly returns a null-impact answer instead of forcing a map.
- At least one mutated-geography run (different city/region) passing the same
  review bar, proving no benchmark-string dependence.
- Regression tests cover the selector and feature-handling state space.
- This `README.md` reflects the accepted contract; `runs/` holds the evidence.

## Status

- **Phase 0 complete**: question grounded, datasets verified live, `geo` MCP
  built + tested + live-proven (`manual-solution/geo_tool_live_render.png`).
- **Priority 1 complete**: `geo` tool wired into clio-agent. Added
  `clio_agent/tools/clio_kit_bridge.py` (shared clio-kit stdio bridge) and a
  `geospatial_render_feature_map` tool (proxies to the clio-kit `geo` MCP, so
  heavy geo deps stay out of CLIO core); catalog entry visible to
  `visualization`/`analysis`. 28 unit tests + 1 real-proxy integration test
  pass; ruff clean. The visualization expert can now call
  `geospatial_render_feature_map`.
- **Priority 2 complete (approach C).** Authored marketplace pack
  `wildfire-smoke-impact-review` (10 experts, domain-grouped) with
  typed/structural routing only — `when_child_completed` + `structured_outputs`,
  no free-text contracts (runtime now rejects `when_output_contains`). The
  visualization expert calls `geospatial_render_feature_map`. Pack validates
  CLEAN through the agent-blueprint loader (0 errors/warnings, all experts
  enabled). Committed on marketplace `feat/wildfire-smoke-impact-pack`.
  Branch note: `feat/wildfire-smoke-impact-case` now also carries the geo-tool
  wiring merge (case + wiring coexist); `develop` left untouched pending review.
- **Priority 3 in progress — run-ready.** ALCF auth confirmed working (Globus
  token present, `get_access_token` succeeds). Pack installed into the workspace
  discovery root `.clio/agent-blueprints/wildfire-smoke-impact-review`; CLIO
  discovers it (18 blueprints, alongside `earthscope-gnss-region`). Marketplace
  source: authored in `~/clio-agent-marketplace` (feat/wildfire-smoke-impact-pack);
  the live marketplace is the `external/clio-agent-marketplace` submodule (on
  `main`) — pack must also land there/in the submodule for a committed run.
  Remaining for the first live run: invoke a CLIO session with the wildfire
  blueprint against ALCF Sophia + live NDP and capture a trace to `runs/`. NOTE:
  `scripts/run_demo_benchmark.py` is heavily EarthScope-specific
  (`_is_earthscope_gnss_blueprint_id` gating) — the first wildfire run likely
  needs a generic invocation path or a runner generalization, not the EarthScope
  harness as-is.
