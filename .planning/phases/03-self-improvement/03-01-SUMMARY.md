---
phase: 03-self-improvement
plan: 01
subsystem: optimizer
tags: [dspy, simba, instrumentation, metrics, training-data, msgspec, arc]

# Dependency graph
requires:
  - phase: 02-multi-expert-pipeline
    provides: "3 experts (data, analysis, visualization) with ARC memory + invocation storage"
provides:
  - "instrumented_forward decorator for expert call logging"
  - "MetricsAggregator for per-expert success_rate/latency computation"
  - "VariantRecord schema for tracking optimization variants"
  - "get_invocations_by_agent ARC query for training data extraction"
  - "TrainingSetGenerator converting ARC invocations to dspy.Example lists"
  - "clio_expert_metric multi-signal scoring function for SIMBA"
affects: [03-02, 03-03, optimizer, variants, cli-tune]

# Tech tracking
tech-stack:
  added: []
  patterns: [instrumented_forward decorator, multi-signal metric function, invocation-to-example conversion]

key-files:
  created:
    - src/clio_agent/optimizer/__init__.py
    - src/clio_agent/optimizer/instrumentation.py
    - src/clio_agent/optimizer/trainer.py
    - tests/test_core/test_instrumentation.py
    - tests/test_core/test_trainer.py
  modified:
    - src/clio_agent/arc/schema.py
    - src/clio_agent/arc/memory.py
    - src/clio_agent/agent.py

key-decisions:
  - "Inline instrumentation in agent.py dispatch instead of decorator on expert.forward() (avoids MCPToolBridge constructor side effects)"
  - "Truncate output fields to 500 chars in invocations to prevent ARC bloat"
  - "Error keywords as frozenset for O(1) membership testing in metric function"
  - "VisualizationExpert field mapping: visualization_description -> analysis weight, file_path -> recommendations weight"

patterns-established:
  - "instrumented_forward: decorator pattern for logging expert calls to ARC"
  - "_extract_output: safe extraction of string fields from dspy.Prediction"
  - "clio_expert_metric: 3-signal weighted metric with boolean gate for trace mode"
  - "get_invocations_by_agent: disk scan with agent_id + status filter"

# Metrics
duration: 6min
completed: 2026-02-11
---

# Phase 3 Plan 1: Optimization Data Pipeline Summary

**Instrumented expert calls logging to ARC, TrainingSetGenerator converting invocations to dspy.Examples, and 3-signal metric function for SIMBA optimization**

## Performance

- **Duration:** 6 min
- **Started:** 2026-02-11T09:43:11Z
- **Completed:** 2026-02-11T09:49:55Z
- **Tasks:** 2
- **Files modified:** 8

## Accomplishments
- optimizer/ package with instrumentation.py and trainer.py providing complete data pipeline for SIMBA
- ARC memory extended with get_invocations_by_agent, store_variant_record, get_variant_records
- VariantRecord msgspec schema for tracking optimization variant lifecycle
- agent.py stores tier-2 expert Invocations with full input/output on every dispatch
- 24 tests covering instrumentation, metrics, schema, training generation, and metric scoring

## Task Commits

Each task was committed atomically:

1. **Task 1: Instrumentation decorator + ARC extensions + VariantRecord schema** - `dfe0681` (feat)
2. **Task 2: Training set generator + metric function** - `e846423` (feat)

## Files Created/Modified
- `src/clio_agent/optimizer/__init__.py` - Package init with public exports
- `src/clio_agent/optimizer/instrumentation.py` - instrumented_forward decorator + MetricsAggregator
- `src/clio_agent/optimizer/trainer.py` - TrainingSetGenerator + clio_expert_metric
- `src/clio_agent/arc/schema.py` - Added VariantRecord struct + encode/decode
- `src/clio_agent/arc/memory.py` - Added get_invocations_by_agent, store_variant_record, get_variant_records, _variants_dir
- `src/clio_agent/agent.py` - Added _store_expert_invocation for tier-2 logging on dispatch
- `tests/test_core/test_instrumentation.py` - 11 tests for decorator, metrics, schema, ARC queries
- `tests/test_core/test_trainer.py` - 13 tests for generation, validation, metric scoring

## Decisions Made
- Used inline instrumentation in agent.py dispatch section rather than decorating expert.forward() to avoid MCPToolBridge constructor side effects during decoration
- Output field values truncated to 500 chars to prevent ARC storage bloat
- VisualizationExpert outputs mapped to same metric weights: visualization_description -> analysis signal (0.4), file_path existence -> recommendations signal (0.3)
- Error keywords stored as frozenset for O(1) membership testing in metric function

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Created stub trainer.py before Task 2 to unblock imports**
- **Found during:** Task 1 (running tests)
- **Issue:** optimizer/__init__.py imports from trainer.py which didn't exist yet, causing ModuleNotFoundError
- **Fix:** Created trainer.py stub with NotImplementedError methods, replaced with full implementation in Task 2
- **Files modified:** src/clio_agent/optimizer/trainer.py
- **Verification:** Import succeeds, tests collect
- **Committed in:** dfe0681 (Task 1 commit)

---

**Total deviations:** 1 auto-fixed (1 blocking)
**Impact on plan:** Necessary to unblock Task 1 test execution. Stub replaced by full implementation in Task 2.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Optimization data pipeline complete: every expert call now logs tier-2 Invocation to ARC
- TrainingSetGenerator ready to feed dspy.Examples to SIMBA optimizer (Plan 03-02)
- clio_expert_metric ready for use as SIMBA metric function
- VariantRecord schema ready for variant storage/comparison (Plan 03-03)

---
## Self-Check: PASSED

- All 8 files verified present on disk
- Both task commits (dfe0681, e846423) verified in git log
- 24/24 tests passing, ruff clean

*Phase: 03-self-improvement*
*Completed: 2026-02-11*
