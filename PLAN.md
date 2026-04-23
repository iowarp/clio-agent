# CLIO Agent Global Development Plan

Last updated: 2026-04-23

This file is the working source of truth for building CLIO Agent beyond the
v0.2.0 MVP into the full agent harness envisioned by the project.

If this file conflicts with older planning archives under `.planning/`, treat
the archive as historical context and this file as the active spec. If this
file conflicts with source code or tests, verify the source first, then update
this file in the same change.

## Mission

CLIO Agent is the IOWarp intelligence layer for scientific data management. It
is not a generic agent framework and not a demo chatbot. It should help HPC and
research users inspect, optimize, transform, profile, schedule, and reason about
real scientific data workflows using real tools connected to the user's real
environment.

The core product promise is:

> A locally deployable, memory-backed, self-improving scientific agent that routes
> work to specialized experts, uses real I/O and HPC tools, records what
> happened in ARC, and improves only when measured evidence says it improved.

## Current Baseline

The current codebase is a real alpha, not empty scaffolding.

Implemented and source-verified:

- CLIO harness contracts for validated route decisions and explicit tool traces:
  `src/clio_agent/harness.py`
- Main orchestrator: `src/clio_agent/agent.py`
- CLI: `src/clio_agent/ui/cli.py`
- REST API with health/query/experts/metrics/SSE: `src/clio_agent/ui/api.py`
- Multi-provider LM configuration: `src/clio_agent/config.py`
- ARC memory, schemas, cache, index, LSM, retrieval, context compilation:
  `src/clio_agent/arc/`
- HDF5 FastMCP server using `h5py`: `src/clio_agent/tools/servers/hdf5_server.py`
- Parquet FastMCP server using `pyarrow`: `src/clio_agent/tools/servers/parquet_server.py`
- FastMCP 3 gateway: `src/clio_agent/tools/gateway.py`
- Data and Analysis experts now use native typed CLIO request/result contracts
  with deterministic tool execution first and explicit tool provenance:
  `src/clio_agent/experts/data_expert.py`,
  `src/clio_agent/experts/analysis_expert.py`
- Native HDF5, Parquet, and CSV expert paths validate tool result shapes before
  answer construction, normalize tool errors, and store compact ARC-compatible
  tool summaries through `src/clio_agent/harness.py`
- Visualization expert remains a chart-focused DSPy ReAct module around local
  matplotlib tools: `src/clio_agent/experts/visualization_expert.py`
- Offline optimization support: `src/clio_agent/optimizer/`
- Container and CI artifacts: `Dockerfile`, `docker-compose.yml`,
  `singularity.def`, `.github/workflows/ci.yml`

Recent local verification:

- `uv sync --extra dev --extra api --extra optimizers`: upgraded FastMCP to
  `3.2.4` and switched from legacy `dspy-ai` to direct `dspy` dependency
  (2026-04-23)
- `uv run python -c "import dspy, fastmcp; ..."`: reported `dspy 3.1.3`,
  `fastmcp 3.2.4` (2026-04-23)
- Focused upgraded-harness smoke checks against generated HDF5, Parquet, and
  visualization outputs passed locally (2026-04-23)
- `uv run pytest tests/test_tools/test_gateway.py tests/test_tools/test_execution.py
  tests/test_core/test_api.py tests/test_core/test_cli_commands.py
  tests/test_integration/test_local_filesystem_smoke.py --no-cov -q`: 78 passed
  (2026-04-23)
- `uv run pytest tests/`: 598 passed, 84% coverage (2026-04-23)
- `uv run ruff check src/ tests/ scripts/create_demo_data.py`: passed (2026-04-23)
- `uv run ruff format <first-wave touched Python files> --check`: passed (2026-04-23)
- Live CLI and API smoke checks worked against generated HDF5 and Parquet files

Known baseline caveats:

- The product path for explicit HDF5, Parquet, and CSV file tasks is
  deterministic harness routing plus native expert tool execution with ARC tool
  traces. LLM routing and DSPy synthesis are reasoning extensions, not the core
  safety path.
- FastMCP gateway is now validated on FastMCP 3.2.4 with stable namespaced tool
  names.
- `MCPToolBridge` is retained as a compatibility shim over the explicit sync
  MCP executor. New tool execution paths should use the sync or async executor
  interfaces directly.
- IOWarp CTE support is an adapter-shaped local fallback, not proven production
  CTE integration.
- ADIOS, Darshan, SLURM/PBS, compression benchmarking, workflow execution, A2A,
  and nanoagent execution are not yet real product capabilities.
- Existing planning docs contain stale statements from earlier phases. Use them
  for design intent, not current-state truth.

## Product Boundaries

In scope:

- Scientific data I/O inspection and optimization
- HDF5, Parquet, ADIOS2/BP, Darshan, compression, format conversion
- HPC scheduler and job context integration, starting with SLURM unless the
  deployment target requires PBS first
- ARC memory with durable local mode and real IOWarp CTE mode
- CLI, REST API, library use, and future agent-to-agent access
- Tool-backed answers with traceable evidence
- Offline optimization and gated variant deployment
- Local-first deployment with LM Studio or Ollama, plus cloud provider support

Out of scope unless explicitly promoted:

- A general-purpose chatbot
- A framework for arbitrary agent creation
- Unbounded autonomous job submission without policy and approval gates
- Production online learning that can deploy changes without evaluation gates
- Replacing IOWarp runtime components that CLIO should integrate with

## Architectural Commitments

1. Source and tests outrank docs.
2. CLIO is a hierarchical agent: orchestrator, experts, optional workers.
3. All durable runtime context flows through ARC.
4. Tool servers perform real operations or clearly report that the real backend
   is unavailable.
5. The agent never invents file-specific or environment-specific facts without
   a tool result, a stored ARC record, or an explicit user-provided fact.
6. Context is compiled with budgets. Raw history concatenation is not allowed
   for production paths.
7. Tool sets are curated. Each expert should see the smallest useful set of
   high-level tools, normally 5 to 7.
8. Deterministic routing, typed expert contracts, and validated tool execution
   protect the product path. LLM routing and DSPy synthesis are optional
   reasoning layers, not the execution owner.
9. Optimization is evidence-gated. Variants can be saved freely, but deployment
   requires before/after evaluation and rollback.
10. External integration failures degrade clearly. Users should know which
    backend is live, degraded, skipped, or unavailable.

## Runtime Modes

CLIO must support these deployment modes:

| Mode | Target | Required behavior |
| --- | --- | --- |
| Standalone local | Developer workstation | LM Studio/Ollama plus local ARC and in-process tools |
| API service | Server or container | FastAPI with health, query, experts, metrics, structured errors |
| HPC login node | Cluster environment | CLI/API with scheduler, Darshan, ADIOS, and filesystem-safe policies |
| IOWarp integrated | Full IOWarp stack | ARC uses CTE, tools use CAE/PPI where available |
| Agent sidekick | External coding/science agents | A2A or HTTP contract exposing CLIO capabilities safely |

## Integration Model

CLIO should integrate with the IOWarp stack as follows:

| IOWarp layer | CLIO responsibility |
| --- | --- |
| CEI | CLIO orchestrator, context compilation, expert routing, final answer synthesis |
| CAE/PPI | FastMCP tool gateway and scientific tool servers |
| CTE | ARC durable storage, tier policy, prefetch/promotion hints |
| Storage/runtime | HDF5, Parquet, ADIOS2, Darshan logs, scheduler state, real filesystems |

The local fallback is part of the product, but it must be explicitly reported as
fallback mode. A successful local fallback is not proof that IOWarp integration
works.

## Capability Matrix

| Capability | Current status | Target status |
| --- | --- | --- |
| HDF5 inspection | Real local server | Production tool with safe paths, chunk/compression planning, optional rewrite plan |
| Parquet inspection | Real local server | Production schema, stats, quality profile, partition/row-group advice |
| Visualization | Real local matplotlib tools | Stable chart artifacts, file registry, optional API artifact retrieval |
| ARC local memory | Real | Retention, locking, repair, export, privacy controls |
| ARC CTE backend | Partial adapter | Real CTE contract with namespace, tier migration, stats, failure modes |
| Offline optimization | Real but underused | Evidence-gated workflow with curated datasets and model/provider matrix |
| REST API | Real | Auth, streaming lifecycle, cancellation, artifact endpoints |
| CI/container | Present | Verified build/test matrix plus live-integration gates when credentials exist |
| ADIOS2/BP | Missing | Real ADIOS2 server and conversion/profile workflow |
| Darshan | Missing | Real log parser/profiler server and HPC I/O recommendations |
| SLURM/PBS | Missing | Real scheduler server with read-only default and guarded submit/cancel |
| Compression benchmarking | Missing | Real sampling benchmark with limits, provenance, and reproducible output |
| Multi-expert coordination | Partial | Plan-execute-verify workflows with dependency passing and trace storage |
| A2A/external agents | Missing | Authenticated protocol with agent card and bounded delegation |
| Nanoagents/parallel workers | Placeholder concept | Only implement after real workflows need parallel fan-out |

## Version Roadmap

### v0.3: Integration-Ready Harness

Goal: make the existing alpha trustworthy as a base for real environments.

Deliverables:

- Replace deprecated FastMCP `prefix=` gateway mounts with current namespace API.
- Add a runtime capability probe command, preferably `clio-agent doctor`.
- Add an integration registry that describes every configured backend:
  provider, endpoint, auth mode, health, capability set, and fallback state.
- Add explicit file access policy: allowed roots, read/write mode, max file size,
  symlink behavior, and unsafe-path rejection.
- Add tool parameter and result validation around every tool call.
- Replace or isolate `MCPToolBridge` behind an interface that supports both sync
  CLI calls and async API service calls without hidden global thread behavior.
- Add artifact registry for generated plots and future reports.
- Reconcile stale docs that claim planned features are current.

Acceptance gates:

- Full test suite passes.
- `clio-agent doctor` reports LM, ARC, gateway, HDF5, Parquet, API readiness.
- A missing LM, missing IOWarp runtime, or missing tool backend produces a
  structured degraded status, not a crash.
- Gateway namespace migration is covered by tests.
- No production path depends on README-only claims.

### v0.4: Real Scientific Tool Integrations

Goal: make CLIO useful against real scientific/HPC artifacts, not only HDF5 and
Parquet sample files.

Deliverables:

- ADIOS2/BP FastMCP server:
  - inspect variables, attributes, timesteps, shapes, engines, file metadata
  - sample variable data within configured limits
  - recommend conversion or layout strategy
  - optionally convert BP to HDF5/Parquet through a guarded write workflow
- Darshan FastMCP server:
  - parse real Darshan logs
  - summarize POSIX/MPI-IO/HDF5 behavior
  - detect common I/O bottlenecks
  - produce actionable recommendations tied to counters
- Scheduler server, starting with SLURM unless changed:
  - read-only default: partition info, queue status, job status, accounting
  - guarded mutating operations: submit, cancel, hold, release
  - explicit user approval for mutating operations in CLI/API
- Compression benchmark server:
  - bounded sample extraction
  - candidate codec tests where libraries are available
  - speed, ratio, CPU, and memory reporting
  - provenance for benchmark inputs and limits
- Tool packaging model:
  - in-process for tests
  - stdio or HTTP/SSE for real deployments
  - config-driven selection

Acceptance gates:

- Each new integration has tests with deterministic fixtures.
- Each new integration has at least one live/manual verification recipe.
- Real backend unavailable states are distinguishable from code failures.
- Mutating scheduler and conversion operations require an approval policy.

### v0.5: Agent Harness and Objective-Driven Workflows

Goal: make CLIO perform multi-step work reliably with planning, verification,
and traceability.

Deliverables:

- Introduce a `TaskSpec` model stored in ARC:
  - user objective
  - files/resources involved
  - constraints and safety policy
  - selected experts/tools
  - expected outputs
  - validation criteria
- Implement plan-execute-verify orchestration:
  - deterministic objective parsing for known scientific tasks
  - expert selection through registry plus capability matcher
  - explicit tool call plan
  - result validation before final answer
  - retry/fallback rules
- Upgrade `MultiAgentCoordinator`:
  - dependency-aware sequential execution
  - safe parallel execution where dependencies permit
  - cancellation and timeout propagation
  - coordination trace in ARC
- Add first-class workflows:
  - HDF5 optimization assessment
  - Parquet data-quality profile
  - ADIOS/Darshan I/O diagnosis
  - scheduler-aware run analysis
  - storage-to-insight pipeline across data, analysis, visualization

Acceptance gates:

- Workflows pass golden-task tests with fixed data/log fixtures.
- Every final answer can cite tool outputs or ARC records used.
- Failed validation produces a partial result plus next action, not false
  success.
- API supports request cancellation and long-running task status.

### v0.6: ARC + IOWarp CTE Production Integration

Goal: make ARC a real IOWarp-backed memory layer while preserving local mode.

Deliverables:

- Define and implement the real CTE client contract:
  - namespace registration
  - read/write/delete/list
  - tier selection
  - metadata and stats
  - migration and prefetch hints if the runtime exposes them
- Add ARC storage modes:
  - `local`
  - `cte`
  - `auto` with explicit fallback reporting
- Add retention and maintenance:
  - invocation and metrics retention policy
  - LSM compaction safety and repair
  - index rebuild
  - export/import
- Add privacy and security:
  - optional no-persist mode
  - sensitive field redaction
  - encryption-at-rest option if required for deployments
- Add concurrency controls for API service mode.

Acceptance gates:

- Local mode and CTE mode pass the same ARC contract tests.
- CTE failures degrade to local mode only when policy permits fallback.
- Tier stats shown by CLI/API are real in CTE mode and clearly labeled in
  fallback mode.
- ARC survives restart with conversation, invocation, profile, metric, and
  variant records intact.

### v0.7: Evaluation and Self-Improvement as Product

Goal: make "gets better with use" measurable and safe.

Deliverables:

- Golden evaluation suite:
  - HDF5 fixtures
  - Parquet fixtures
  - ADIOS fixtures
  - Darshan logs
  - scheduler transcripts or live read-only cluster checks
- Scorecards:
  - routing accuracy
  - tool-call correctness
  - answer groundedness
  - latency
  - cost/token use
  - regression rate
- Training data quality controls:
  - only successful and validated invocations become candidates
  - deduplication and leakage checks
  - manual curation path for high-value examples
- Optimization deployment:
  - shadow evaluation first
  - statistical significance gates
  - human approval for production activation
  - rollback and audit trail

Acceptance gates:

- Optimizer cannot deploy a variant without evaluation evidence.
- `/metrics`, `/compare`, and API metrics report evaluation-backed scores.
- CI can run offline golden evaluations.
- Live evaluations are separately gated and skipped cleanly when backends are
  unavailable.

### v0.8: External Agent and Team Integration

Goal: expose CLIO safely to other agents, services, and team workflows.

Deliverables:

- Agent card or service descriptor with capabilities, limits, and auth.
- A2A or HTTP delegation endpoint:
  - query
  - task submission
  - task status
  - artifact retrieval
  - trace retrieval
- Authentication and authorization:
  - API keys or bearer tokens
  - role/policy checks for mutating operations
  - per-tool allow/deny rules
- Team collaboration:
  - documented work-package ownership
  - integration test environments
  - release checklists
  - evidence templates for PRs

Acceptance gates:

- External callers can discover CLIO capabilities without reading source.
- External calls are bounded by policy and logged in ARC.
- A mutating external request cannot run without authorization and approval.

### v1.0: Production Release

Goal: CLIO is deployable in a real IOWarp/HPC environment with a stable contract.

Release criteria:

- At least four real scientific/HPC integrations are production-ready:
  HDF5, Parquet, plus two of ADIOS2, Darshan, SLURM/PBS, compression.
- ARC local and CTE modes are contract-tested.
- CLI and API are both documented and stable.
- Security policy exists for file access, command execution, and job mutation.
- Golden evaluations pass at agreed thresholds.
- Container and HPC deployment paths are verified.
- Known limitations are explicit and user-visible.

## Work Packages for Coding Agents

Use these work packages to split work across team members or coding agents.
Each package should produce source changes, tests, and evidence.

### WP-01: Planning Truth Reconciliation

Scope:

- Make this `PLAN.md` the active global spec.
- Mark older `.planning` docs as archive where they conflict.
- Update stale docs that claim missing capabilities are implemented.

Files likely touched:

- `PLAN.md`
- `.planning/STATE.md`
- `.planning/PROJECT.md`
- selected docs under `docs/`

Done when:

- A new contributor can read one plan and understand current vs target state.
- No active doc claims ADIOS, Darshan, SLURM, A2A, nanoagents, or CTE are fully
  delivered unless source and tests prove it.

### WP-02: Runtime Doctor and Integration Registry

Scope:

- Add capability registry for LM, ARC, gateway, tools, CTE, scheduler.
- Add CLI/API health details.
- Add `clio-agent doctor` or equivalent.

Done when:

- The runtime reports `ready`, `degraded`, `unavailable`, or `misconfigured`
  per integration.
- Doctor output includes exact config source and next action.

### WP-03: Gateway Modernization and Tool Contracts

Scope:

- Move FastMCP mounts to current namespace API.
- Add schema validation for parameters and results.
- Add file access policy enforcement.
- Add tool timeout and cancellation behavior.

Done when:

- HDF5 and Parquet tests pass through the modern gateway.
- Unsafe paths and invalid arguments are rejected before tool execution.

### WP-04: Async Harness Refactor

Scope:

- Define sync and async execution boundaries.
- Hide or replace `MCPToolBridge`.
- Support CLI sync calls, FastAPI async calls, and optimizer evaluation without
  deadlocks or leaked threads.
- Keep expert tool execution CLIO-native: typed request/result objects,
  deterministic tools first, explicit provenance, and DSPy only for synthesis
  or planning where it adds value.

Done when:

- Expert tools run through a single execution abstraction.
- API can serve concurrent requests without creating unbounded background
  threads.

### WP-05: Real CTE Adapter

Scope:

- Identify real IOWarp CTE API or SDK.
- Implement ARC storage contract against it.
- Preserve local fallback mode.

Done when:

- Contract tests pass in local and CTE modes.
- CTE mode reports real namespace and tier stats.

### WP-06: ADIOS2 Server

Scope:

- Add ADIOS2/BP inspection and optional conversion tools.
- Add deterministic fixtures and live verification recipe.

Done when:

- CLIO can inspect BP variables/timesteps and recommend a concrete next action.

### WP-07: Darshan Server

Scope:

- Add Darshan log parsing and I/O diagnosis.
- Map counters to recommendations with traceable evidence.

Done when:

- CLIO can turn a real Darshan log into bottleneck findings and next actions.

### WP-08: Scheduler Server

Scope:

- Add SLURM first unless project leadership chooses PBS first.
- Read-only operations first, mutating operations behind approval policy.

Done when:

- CLIO can inspect queue/job/accounting state.
- Submit/cancel cannot run without explicit approval.

### WP-09: Workflow Orchestrator

Scope:

- Add `TaskSpec`, plan-execute-verify, and workflow traces.
- Upgrade `MultiAgentCoordinator`.

Done when:

- Multi-step HDF5/Parquet/Darshan workflows pass golden-task tests.

### WP-10: Evaluation Harness

Scope:

- Add golden datasets/logs or generators.
- Add scoring and regression gates.
- Wire offline evaluation to CI where feasible.

Done when:

- A release candidate can be judged by scorecards, not ad hoc demos.

## Integration Definition of Done

Every real integration must include:

- Config keys and environment variables
- Health probe
- Clear unavailable/degraded behavior
- Tool API with typed inputs and outputs
- Parameter validation
- Timeout limits
- Safe error messages
- Unit tests with deterministic fixtures
- Integration tests that can be skipped when the backend is unavailable
- Manual live verification steps
- ARC trace fields for tool calls and results
- Documentation of any mutating or security-sensitive behavior

Mocks are allowed for unit tests. They are not acceptable as proof that a
capability is delivered.

## Agent and Team Collaboration Rules

All human and coding-agent contributors should follow these rules:

1. Read `AGENTS.md`, this `PLAN.md`, and the relevant source files before
   changing code.
2. Start by checking `git status --short`.
3. Do not rewrite unrelated dirty files.
4. Work in scoped packages with clear file ownership.
5. Prefer source and tests over docs when resolving conflicts.
6. Add tests proportional to blast radius.
7. For external integrations, include both offline tests and live verification
   instructions.
8. Never claim a backend is integrated because a fallback path works.
9. Do not expose DSPy concepts in user-facing interfaces unless the interface is
   explicitly for developers.
10. PRs must include commands run, results, skipped checks, and required human
    verification.

## Required Evidence for PRs

Every PR should include:

- Problem statement
- Implementation summary
- Files changed by ownership area
- Test commands and results
- Integration evidence:
  - backend used
  - endpoint or environment
  - sample input
  - sample output
  - skipped live checks with reason
- Rollback or failure mode if the change affects runtime behavior

For planner/spec changes, include:

- Which older docs were reconciled
- Which source files were checked
- Which decisions remain open

## Open Decisions

These require project-owner input before the related work can be considered
complete. Defaults are listed so coding can proceed without blocking where safe.

| Decision | Default until changed | Needed for |
| --- | --- | --- |
| Real CTE API/SDK and endpoint | Keep local fallback and design a small adapter interface | v0.6 |
| First scheduler | SLURM | v0.4 scheduler server |
| Mutating job policy | Deny by default, require explicit approval | Scheduler and workflow tools |
| Canonical live environment | Developer local plus optional HPC login node | Live verification |
| Canonical model set | LM Studio/Ollama local first, OpenAI/Anthropic optional | Evaluation matrix |
| Real fixture sources | Generated fixtures until project data is provided | Evaluation harness |
| A2A protocol flavor | HTTP task API first, formal A2A after capabilities stabilize | v0.8 |

## Immediate Next Milestone

The next milestone should be v0.3: Integration-Ready Harness.

Source-verified v0.3 items already present:

- `doctor` runtime integration status models and CLI/API health detail.
- FastMCP gateway namespace compatibility with stable tool names.
- File access policy and basic tool parameter validation for current HDF5,
  Parquet, CSV, and visualization paths.

Recommended next tasks:

1. Finish tool result validation contracts for every HDF5, Parquet, CSV, and
   visualization response shape.
2. Migrate future direct API tool-use paths to `AsyncMCPToolExecutor` instead of
   sync executor calls when API handlers can stay fully async end-to-end.
3. Define the CTE adapter interface and identify the real IOWarp runtime
   contract with the project owner.
4. Add artifact registry support for generated charts and future reports.

Do not start ADIOS, Darshan, or scheduler implementation until the v0.3
integration harness stays green and every current backend reports clear ready,
degraded, unavailable, or misconfigured status.
