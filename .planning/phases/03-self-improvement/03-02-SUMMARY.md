---
phase: 03-self-improvement
plan: 02
subsystem: optimizer
tags: [dspy, simba, scipy, z-test, variants, optimization, rollback, deploy]

# Dependency graph
requires:
  - phase: 03-self-improvement
    plan: 01
    provides: "TrainingSetGenerator, clio_expert_metric, instrumented_forward, VariantRecord schema, ARC variant queries"
provides:
  - "SIMBARunner with full optimization pipeline (evaluate before/after, SIMBA compile, significance test, save variant)"
  - "VariantManager with save, load, deploy, rollback, compare, list_agents_with_variants"
  - "Two-proportion z-test for statistical significance (scipy.stats.norm)"
affects: [03-03, optimizer, cli-tune, experts]

# Tech tracking
tech-stack:
  added: [scipy]
  patterns: [lazy scipy import with clear error, native Python type casting from numpy, sequential variant IDs, module instance reuse for load]

key-files:
  created:
    - src/clio_agent/optimizer/runner.py
    - src/clio_agent/optimizer/variants.py
    - tests/test_core/test_runner.py
    - tests/test_core/test_variants.py
  modified:
    - src/clio_agent/optimizer/__init__.py

key-decisions:
  - "Cast scipy numpy types (np.True_, numpy.float64) to native Python types to prevent msgspec serialization errors"
  - "load_variant reuses existing module instance via module.load(path) to avoid MCPToolBridge constructor side effects"
  - "num_threads=1 for dspy.evaluate.Evaluate to prevent MCPToolBridge threading deadlocks"
  - "Sequential variant IDs: {agent_id}_v{N} with N from ARC record count"

patterns-established:
  - "VariantManager: disk JSON + ARC VariantRecord metadata for variant lifecycle"
  - "SIMBARunner: evaluate-before -> SIMBA.compile -> evaluate-after -> z-test -> save pipeline"
  - "Lazy scipy import with try/except and explicit install instructions"
  - "Native type casting pattern: float() and bool() on scipy/numpy return values"

# Metrics
duration: 6min
completed: 2026-02-11
---

# Phase 3 Plan 2: SIMBA Runner & Variant Management Summary

**SIMBA optimization runner with two-proportion z-test significance testing and variant lifecycle management (save/load/deploy/rollback)**

## Performance

- **Duration:** 6 min
- **Started:** 2026-02-11T09:52:35Z
- **Completed:** 2026-02-11T09:58:59Z
- **Tasks:** 2
- **Files modified:** 5

## Accomplishments
- VariantManager with full lifecycle: save to disk + ARC, load into existing module, deploy/rollback with exactly-one-active invariant, compare for CLI display
- SIMBARunner executing complete optimization pipeline: 20/80 split, before/after evaluation, SIMBA compile, statistical significance test, automatic variant save
- Two-proportion z-test using scipy.stats.norm with proper native Python type casting (no numpy leaks to msgspec)
- 24 new tests (16 variants + 8 runner) all passing, 293 total tests passing

## Task Commits

Each task was committed atomically:

1. **Task 1: VariantManager -- save, load, deploy, rollback, compare** - `e240cae` (feat)
2. **Task 2: SIMBARunner -- optimization pipeline with statistical testing** - `ab8e97f` (feat)

## Files Created/Modified
- `src/clio_agent/optimizer/variants.py` - VariantManager class with save/load/deploy/rollback/compare/list_agents_with_variants
- `src/clio_agent/optimizer/runner.py` - SIMBARunner class with run() pipeline and test_significance() z-test
- `src/clio_agent/optimizer/__init__.py` - Updated exports: added SIMBARunner and VariantManager
- `tests/test_core/test_variants.py` - 16 tests covering full variant lifecycle
- `tests/test_core/test_runner.py` - 8 tests covering significance testing and mocked pipeline

## Decisions Made
- Cast scipy return types (np.True_, numpy.float64) to native Python bool/float to prevent msgspec encoding errors when storing VariantRecord
- load_variant() reuses existing module instance via module.load(path=...) rather than creating new instance, avoiding MCPToolBridge constructor side effects (Pitfall 5 from research)
- num_threads=1 for dspy.evaluate.Evaluate to prevent MCPToolBridge threading deadlocks (Pitfall 6 from research)
- Sequential variant IDs ({agent_id}_v{N}) with N derived from ARC record count for that agent

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Installed scipy optional dependency**
- **Found during:** Task 2 (running test_significance tests)
- **Issue:** scipy not installed in venv despite being in pyproject.toml [optimizers] group
- **Fix:** Ran `uv pip install -e '.[optimizers]'` to install scipy>=1.11.0
- **Files modified:** None (runtime dependency install)
- **Verification:** All scipy-dependent tests pass
- **Committed in:** ab8e97f (Task 2 commit)

**2. [Rule 1 - Bug] Cast numpy types to native Python types**
- **Found during:** Task 2 (running full pipeline tests)
- **Issue:** scipy.stats.norm.cdf returns numpy.float64, and bool comparison returns np.True_/np.False_ -- these fail msgspec serialization and `is True` identity checks
- **Fix:** Wrapped returns with `float()` and `bool()` casts in test_significance()
- **Files modified:** src/clio_agent/optimizer/runner.py
- **Verification:** All tests pass, VariantRecord stores correctly in ARC
- **Committed in:** ab8e97f (Task 2 commit)

---

**Total deviations:** 2 auto-fixed (1 blocking, 1 bug)
**Impact on plan:** Both fixes necessary for correctness. No scope creep.

## Issues Encountered
None beyond the auto-fixed deviations above.

## User Setup Required
None - scipy installed automatically via `[optimizers]` optional dependency group.

## Next Phase Readiness
- SIMBARunner ready to optimize any expert module given sufficient training data (min 5 examples for split)
- VariantManager ready for CLI `tune` command to deploy/rollback variants (Plan 03-03)
- Statistical significance gate ensures only real improvements are flagged
- All optimizer package exports complete: instrumented_forward, MetricsAggregator, TrainingSetGenerator, clio_expert_metric, SIMBARunner, VariantManager

---
## Self-Check: PASSED

- All 5 files verified present on disk
- Both task commits (e240cae, ab8e97f) verified in git log
- 24/24 new tests passing, 293 total tests passing, ruff clean

*Phase: 03-self-improvement*
*Completed: 2026-02-11*
