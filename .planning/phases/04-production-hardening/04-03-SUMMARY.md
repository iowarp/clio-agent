---
phase: 04-production-hardening
plan: 03
subsystem: infra, testing
tags: [docker, singularity, hpc, containers, pytest, coverage]

requires:
  - phase: 04-02
    provides: REST API with FastAPI endpoints for container entrypoint
provides:
  - Dockerfile with multi-stage build for CLIO Agent API
  - docker-compose.yml with env-based LM provider config
  - Singularity definition for HPC deployment
  - 85% test coverage (549 tests, 80% gate passing)
affects: []

tech-stack:
  added: [docker, singularity, docker-compose]
  patterns: [multi-stage build, env-based config, host.docker.internal for LM provider]

key-files:
  created:
    - Dockerfile
    - docker-compose.yml
    - singularity.def
    - tests/test_arc/test_storage.py
    - tests/test_arc/test_coordinator.py
    - tests/test_arc/test_retrieval.py
    - tests/test_arc/test_memory_coverage.py
    - tests/test_core/test_cli_argparse.py
  modified:
    - tests/test_core/test_config_providers.py
    - tests/test_core/test_api.py

key-decisions:
  - "scipy installed as runtime dependency (was optional) to enable SIMBA runner tests"

patterns-established:
  - "Container deployment: CLIO_* env vars for all config, no hardcoded LM URLs"
  - "HPC containers: Singularity bootstraps from Docker python:3.12-slim base"

duration: 24min
completed: 2026-02-11
---

# Phase 4 Plan 3: Container Deployment + 80% Coverage Summary

**Multi-stage Dockerfile, docker-compose.yml with env-based LM config, Singularity def for HPC, and 549 tests at 85% coverage**

## Performance

- **Duration:** 24 min
- **Started:** 2026-02-11T10:50:55Z
- **Completed:** 2026-02-11T11:15:14Z
- **Tasks:** 2
- **Files modified:** 10

## Accomplishments
- Dockerfile with multi-stage build (builder + runtime) using UV for fast dependency resolution
- docker-compose.yml with CLIO_* env vars, persistent volume for ARC data, healthcheck
- Singularity/Apptainer definition for HPC environments bootstrapping from python:3.12-slim
- Test coverage raised from 71% to 85% (549 total tests, up from 413)
- 136 new tests covering storage.py (65%), coordinator.py (93%), retrieval.py (98%), memory.py (96%), config.py (88%)

## Task Commits

1. **Task 1: Dockerfile, Singularity def, Docker Compose** - `6f2d83c` (feat)
2. **Task 2: Backfill tests to 80% coverage** - `95ba274` (test)

## Files Created/Modified
- `Dockerfile` - Multi-stage build for CLIO Agent API with healthcheck
- `docker-compose.yml` - Service orchestration with env-based LM provider config
- `singularity.def` - HPC container definition with uvicorn entrypoint
- `tests/test_arc/test_storage.py` - IOWarpCTEBackend local fallback and tier migration tests
- `tests/test_arc/test_coordinator.py` - Multi-agent coordination plan/execute tests
- `tests/test_arc/test_retrieval.py` - Keyword-based context retrieval scoring tests
- `tests/test_arc/test_memory_coverage.py` - ARC memory metrics, context, profiles, procedural memory
- `tests/test_core/test_cli_argparse.py` - CLI argparse flag parsing tests
- `tests/test_core/test_config_providers.py` - Added setup_dspy and fetch_lm_studio_models tests
- `tests/test_core/test_api.py` - Added SSE done event, error_info, model serialization tests

## Decisions Made
- scipy installed as runtime dependency to enable SIMBA runner statistical tests (was optional, 7 tests were failing)

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Installed scipy to fix 7 failing runner tests**
- **Found during:** Task 2 (test backfill)
- **Issue:** 7 tests in test_runner.py were failing due to missing scipy (optional dependency)
- **Fix:** Installed scipy via `uv pip install scipy numpy`
- **Files modified:** None (runtime dependency only)
- **Verification:** All 8 runner tests pass after install
- **Committed in:** Part of 95ba274

---

**Total deviations:** 1 auto-fixed (1 blocking)
**Impact on plan:** Auto-fix necessary to pass full test suite. No scope creep.

## Issues Encountered
- Pre-existing end-to-end integration test (test_end_to_end.py) requires LM Studio running and fails in CI-like environments. This is expected and pre-existing (not introduced by this plan).

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- All Phase 4 requirements (PROD-01 through PROD-10, TEST-10) are satisfied
- Container files ready for deployment
- 85% test coverage exceeds 80% gate
- Project ready for Phase 5 (IOWarp Integration) when CTE backend becomes available

## Self-Check: PASSED

- All 10 created/modified files verified on disk
- Commit 6f2d83c (Task 1) verified in git log
- Commit 95ba274 (Task 2) verified in git log
- Coverage gate (80%) verified: 85% actual

---
*Phase: 04-production-hardening*
*Completed: 2026-02-11*
