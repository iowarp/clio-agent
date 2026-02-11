# Roadmap: CLIO Agent

## Overview

Transform CLIO Agent from a working-but-stub-heavy CoT agent into a production-ready multi-expert ReAct pipeline with real MCP tool servers, self-improvement via SIMBA optimization, and containerized deployment. Four phases: reset the foundation with real tools, expand to three experts with shared memory, add self-improvement, then harden for production.

## Phases

- [x] **Phase 1: Foundation Reset** - Replace stubs with real DSPy 3.x/FastMCP 3.x patterns and working HDF5 tools
- [x] **Phase 2: Multi-Expert Pipeline** - Three experts (Data, Analysis, Visualization) with shared context and compiled memory
- [ ] **Phase 3: Self-Improvement** - SIMBA optimization, training data collection, variant management with rollback
- [ ] **Phase 4: Production Hardening** - REST API, CI/CD, containers, multi-provider LM, 80% test coverage

## Phase Details

### Phase 1: Foundation Reset
**Goal**: User can ask DataExpert to analyze a real HDF5 file and get tool-backed answers through the CLI
**Depends on**: Nothing (first phase)
**Requirements**: INFRA-01, INFRA-02, INFRA-03, INFRA-04, INFRA-05, TOOL-01, TOOL-02, TOOL-03, AGENT-01, AGENT-02, AGENT-03, AGENT-04, AGENT-05, AGENT-06, AGENT-12, AGENT-13, TEST-01, TEST-02, TEST-03, TEST-04, TEST-05, TEST-07
**Success Criteria** (what must be TRUE):
  1. User types a query about an HDF5 file in the CLI and receives an answer derived from actual h5py tool execution (not hallucinated)
  2. Router correctly sends "what datasets are in this file?" to DataExpert and "hello" to Chat Agent
  3. `pytest tests/` passes with >= 50% coverage and all MCP server tests use in-memory Client(server) pattern
  4. mcp_connector.py and all dead stub files are gone; `ruff check src/` is clean
  5. CLI interactive session works end-to-end: start, query, get tool-backed response, exit
**Plans:** 3 plans

Plans:
- [x] 01-01-PLAN.md -- Delete dead code, update deps, fix imports, configure ChatAdapter
- [x] 01-02-PLAN.md -- Build HDF5 MCP server, gateway, connect DataExpert via ReAct
- [x] 01-03-PLAN.md -- Router + Chat Agent, CLI wiring, comprehensive tests (50% coverage)

### Phase 2: Multi-Expert Pipeline
**Goal**: User can ask questions spanning data, analysis, and visualization -- each routed to the right expert with shared context
**Depends on**: Phase 1
**Requirements**: TOOL-04, TOOL-05, TOOL-06, AGENT-07, AGENT-08, AGENT-09, AGENT-10, AGENT-11, MEM-01, MEM-02, MEM-03, MEM-04, MEM-05, TEST-06, TEST-08
**Success Criteria** (what must be TRUE):
  1. User asks "analyze the schema of data.parquet" and AnalysisExpert returns real pyarrow-backed statistics
  2. User asks "plot the distribution of column X" and VisualizationExpert writes a PNG/SVG file to disk
  3. Router Literal correctly routes to all five targets: chat, data, analysis, visualization, none
  4. DataExpert stores a dataset profile in ARC, and AnalysisExpert can read it in the same session (shared context works)
  5. `pytest tests/` passes with >= 60% coverage including end-to-end workflow test
**Plans:** 3 plans

Plans:
- [x] 02-01-PLAN.md -- Parquet MCP server (pyarrow) + AnalysisExpert with ReAct
- [x] 02-02-PLAN.md -- VisualizationExpert (matplotlib) + ARC shared context layer
- [x] 02-03-PLAN.md -- Router 5-way dispatch + context compilation + 60% coverage

### Phase 3: Self-Improvement
**Goal**: System measurably improves expert performance through offline optimization and exposes metrics to the user
**Depends on**: Phase 2
**Requirements**: OPT-01, OPT-02, OPT-03, OPT-04, OPT-05, OPT-06, OPT-07, OPT-08, TEST-09
**Success Criteria** (what must be TRUE):
  1. User runs `--tune` and sees SIMBA optimization run with before/after success rate comparison
  2. User runs `/metrics` in CLI and sees per-expert success_rate, avg_latency, cache_hit_rate
  3. User runs `/compare` in CLI and sees variant A vs B performance side-by-side
  4. After deploying a bad variant, user can rollback to the previous variant and performance recovers
  5. `pytest tests/` passes with >= 70% coverage
**Plans:** 3 plans

Plans:
- [ ] 03-01-PLAN.md -- Instrumentation decorator, ARC extensions, training set generator
- [ ] 03-02-PLAN.md -- SIMBA runner, variant manager, statistical significance testing
- [ ] 03-03-PLAN.md -- CLI wiring (--tune, /metrics, /compare, /rollback) + 70% coverage

### Phase 4: Production Hardening
**Goal**: CLIO Agent runs as a containerized service with REST API, CI/CD, and multi-provider LM support
**Depends on**: Phase 3
**Requirements**: PROD-01, PROD-02, PROD-03, PROD-04, PROD-05, PROD-06, PROD-07, PROD-08, PROD-09, PROD-10, TEST-10
**Success Criteria** (what must be TRUE):
  1. `curl POST /query` returns a streamed expert response via SSE; `GET /health` returns ok
  2. `docker compose up` starts CLIO Agent + LM Studio and processes queries end-to-end
  3. GitHub Actions CI passes: ruff, mypy, pytest with 80% coverage gate
  4. Switching LM provider from LM Studio to Ollama requires only config change, no code change
  5. Raw Python tracebacks never reach the user -- all errors are structured JSON with degradation fallback
**Plans**: TBD

Plans:
- [ ] 04-01: TBD
- [ ] 04-02: TBD
- [ ] 04-03: TBD

## Progress

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Foundation Reset | 3/3 | ✓ Complete | 2026-02-10 |
| 2. Multi-Expert Pipeline | 3/3 | ✓ Complete | 2026-02-10 |
| 3. Self-Improvement | 0/3 | Planned | - |
| 4. Production Hardening | 0/3 | Not started | - |
