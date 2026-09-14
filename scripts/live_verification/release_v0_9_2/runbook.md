# Campaign runbook

## 1. Prepare and freeze

1. Work from clean clio-agent `develop` with initialized submodules.
2. Run `uv run python scripts/live_verification/release_v0_9_2/scripts/preflight.py`.
3. Push the campaign commit and let GitHub run the comprehensive fleet.
4. Boot a fresh private CTE and backend from the recorded candidate.
5. Start the pinned GACT web application and open it in the Browser tool.

Do not run the full local pytest or frontend test suites.

## 2. Capture evidence

Run gates in this order so shared-runtime defects surface before expensive science:

1. Candidate identity and progressive reload.
2. Automatic compaction, two manual compactions, backend restart, transcript proof.
3. EarthScope Skills.
4. Factorio Flat and Daisy specialists.
5. Consolidated presentation matrix.
6. MCP App.
7. Memory and packaging gates.

For each visible scenario capture request, intermediate, final, and reloaded
screenshots. Record session IDs, exact tool results, A2UI/MCP App identifiers,
artifact paths, and milestone timestamps. A screenshot never substitutes for
runtime evidence, and runtime evidence never substitutes for a screenshot.

## 3. Failure handling

Stop publication on any functional failure. Diagnose it, add a failing focused
test where appropriate, make the smallest real fix, run only the focused tests,
repin the changed component, and rerun affected live gates:

- presentation-only: relevant rows and reload;
- A2UI: EarthScope and reload;
- MCP Apps: MCP App scenario;
- blueprint: that blueprint's science scenario;
- shared session/stream/ARC: reload, compaction/restart, and one science continuity check.

The runtime manifest keeps per-gate component SHAs. Unaffected evidence remains
valid only when none of its runtime inputs changed.

## 4. Release boundary

After every live gate passes, run the Codex/Luna three-session memory gate with a
180-second settle, package/import checks, and GitHub exact-tip CI verification.
Then release Marketplace v0.6.3, GACT v0.11.0, and clio-agent v0.9.2 in dependency
order. Attach the evidence bundle and verify all six installation experiences.

Claude parity and SPOTTER are post-release follow-ups.
