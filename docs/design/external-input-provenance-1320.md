# External input provenance (#1320)

Status: implemented locally; focused backend, component, and contained Chromium
verification passed.

Successful calls to the qualified built-in filesystem reader and CLIO Kit
Geo/Pandas/Plot consumers retain external file arguments as durable `used`
edges. Each edge records the resolved locator, readable basename, originating
schema argument, `schema-arg` evidence, and `external_input_not_hashed`. CLIO
does not open, hash, copy, import, download, or mint an artifact for that source.

The observer uses exact gateway-visible tool names and explicit consuming
arguments. Output arguments, NDP URLs/catalog operations, directory/listing
operations, inline text, and unknown tools do not become used edges. A plausible
external path on an unknown contract is retained as a durable
`external_input_contract_unknown` warning instead of a fabricated use.
`fs_propose_edit` records an external use only when the target already exists
and its implementation reads the old bytes; a missing target remains an
edge-free new-file proposal. Slash-containing prose and queries do not trigger a
path warning.

An output-free read remains queryable through its activity record and
`GET /v1/transforms/{call_id}/lineage`. The tool-result part carries external
input rows for the transcript UI. Activity graphs enforce the existing 500-node
cap, deduplicate edges, and omit edges whose endpoint was clipped. A provenance
recording failure does not change the tool result; a successful tool call emits
the durable, SSE-visible `artifact.provenance.incomplete` diagnostic.
When an explicitly non-writing consumer echoes the same external input under an
output-shaped key such as `local_path`, designation suppresses that verified
outside-workspace input echo before containment checks. Contained paths still
follow normal minting and reconciliation, and write-capable tools still treat a
matching result path as an output. A distinct out-of-root output, including one
from a write-capable tool, emits the existing `containment_rejected` warning and
is never minted.

## Verification

- `uv run --no-sync python -B scripts/run_tests.py tests/test_gact/test_hpc_external_inputs.py`
  passed 18 tests after the graph-cap, conditional-read, unknown-text, and
  read-only/write-capable echoed-path containment regressions were added.
- The affected artifact designation, minting, transform, pagination, export,
  grounding, and external-input suites passed 298 tests in 106.74 seconds.
- The installed-candidate Agent-boundary acceptance passed 1 integration test
  in 68.49 seconds with this explicit local invocation:

  ```powershell
  $env:CLIO_TEST_KIT_LAUNCHER = 'D:\Libraries\Documents\projects\clio_develop_workspace\qualification-runtimes\shared-runtime-20260904-a\bin\clio-kit.exe'
  uv run --no-sync python -B scripts/run_tests.py -m integration tests/test_gact/test_hpc_mcp_integration.py
  ```

  The integration test uses production bounded discovery to mount the installed
  NDP, Geo, Pandas, and Plot namespaces, then runs the production synchronous
  Agent MCP execution boundary and real tool observer. A real
  `pandas_profile_csv` and `plot_line_plot` consume an external CSV. The test
  validates successful structured MCP results, PNG magic, registered output,
  and reloaded HTTP transform/lineage graphs leading to the external source.
  `CLIO_TEST_KIT_LAUNCHER` is required by this integration-only test; the
  repository contains no machine-specific launcher path and ordinary unit gates
  exclude it.
- Focused Ruff checks passed.
- Focused mypy (`--follow-imports=skip`) passed for the five changed backend
  source files.
- `pnpm --dir external/gact-tui/apps/web typecheck` passed after
  `pnpm --dir external/gact-tui/apps/web install --frozen-lockfile` reused 334
  cached packages and downloaded none.
- `pnpm --dir external/gact-tui/apps/web test -- tests/unit/tool-result-ladder.test.tsx`
  passed 91 tests, including external-source and incomplete-warning render cases.
- `pnpm --dir external/gact-tui/apps/web test:e2e -- external-provenance.spec.ts`
  passed in Chromium after a production build. It rendered the readable source
  filename and locator, rendered the provenance-incomplete warning, reloaded the
  application, reopened the persisted session fixture, and verified both states
  remained visible.

This browser gate is a contained component/API-fixture qualification against the
exact changed UI checkout. The fixture uses the production `ToolPart` and the
same result-part payloads captured by the real backend observer test. It is not
an installed full-stack CLIO/CTE qualification: the active contained generation
belongs to another campaign and was left untouched.

No full Agent suite, live HPC measurement, or installed end-to-end CLIO
qualification is claimed.
