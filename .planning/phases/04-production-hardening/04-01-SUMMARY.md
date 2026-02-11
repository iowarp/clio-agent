---
phase: 04-production-hardening
plan: 01
subsystem: config, errors
tags: [multi-provider, env-config, structured-errors, degradation, dspy]

requires:
  - phase: 03-self-improvement
    provides: Working agent with optimizer pipeline and CLI
provides:
  - LMProviderConfig with 4-provider support (lm_studio, ollama, openai, anthropic)
  - Environment-based config via CLIO_* env vars
  - create_lm() and create_router_lm() for provider-agnostic LM creation
  - Structured error hierarchy (ClioError, ProviderError, RoutingError, ExpertError, ToolError, ConfigError)
  - format_error_response() that never exposes raw tracebacks
  - with_degradation() for primary/fallback chains
  - error_info field on Prediction return
affects: [04-02 REST API, 04-03 containers/CI]

tech-stack:
  added: []
  patterns: [provider-agnostic config, structured error hierarchy, graceful degradation chain, env-based settings]

key-files:
  created:
    - src/clio_agent/errors.py
    - tests/test_core/test_config_providers.py
    - tests/test_core/test_errors.py
  modified:
    - src/clio_agent/config.py
    - src/clio_agent/agent.py
    - tests/test_core/test_agent_dispatch.py

key-decisions:
  - "ClioError.error_type defaults to 'clio_error' for base class compatibility with with_degradation()"
  - "Subclasses set error_type in super().__init__ call, not constructor arg"
  - "agent.py uses load_config_from_env + create_router_lm instead of LM-Studio-specific functions"
  - "Only lm_studio provider triggers fetch_lm_studio_models; other providers use static config"
  - "User-facing error messages are friendly ('encountered an issue'), structured details in error_info"

patterns-established:
  - "Provider-agnostic config: load_config_from_env() -> LMProviderConfig -> create_lm()"
  - "Structured errors: ClioError subclass -> to_dict() -> JSON-safe response"
  - "Degradation chain: with_degradation(primary, fallback, error_cls)"
  - "Error wrapping in forward(): ExpertError with error_info on Prediction"

duration: 8min
completed: 2026-02-11
---

# Phase 4 Plan 1: Config + Errors Summary

**Multi-provider LM config (4 providers, env-based) with structured error hierarchy and graceful degradation**

## Performance

- **Duration:** 8 min
- **Started:** 2026-02-11T10:33:50Z
- **Completed:** 2026-02-11T10:42:11Z
- **Tasks:** 2
- **Files modified:** 6

## Accomplishments
- LMProviderConfig supporting lm_studio, ollama, openai, anthropic with CLIO_* env vars
- 5 structured error subclasses with JSON-serializable to_dict() and format_error_response()
- Agent uses provider-agnostic config path; no raw tracebacks in user output
- 51 new tests (31 config + 20 errors), 398 total tests passing

## Task Commits

Each task was committed atomically:

1. **Task 1: Multi-provider LM config with environment-based settings** - `04c94b4` (feat)
2. **Task 2: Structured error handling and agent error wrapping** - `690bd84` (feat)

## Files Created/Modified
- `src/clio_agent/config.py` - Added LMProviderConfig, load_config_from_env, create_lm, create_router_lm
- `src/clio_agent/errors.py` - New: ClioError hierarchy, format_error_response, with_degradation
- `src/clio_agent/agent.py` - Uses provider-agnostic config, wraps errors in ExpertError
- `tests/test_core/test_config_providers.py` - New: 31 tests for multi-provider config
- `tests/test_core/test_errors.py` - New: 20 tests for error hierarchy and degradation
- `tests/test_core/test_agent_dispatch.py` - Updated: error assertion matches structured format

## Decisions Made
- ClioError.error_type defaults to 'clio_error' so with_degradation can construct base class without explicit type
- Only lm_studio provider triggers dynamic model fetching; ollama/openai/anthropic use static config
- User-facing error messages are friendly; structured details go in Prediction.error_info

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed with_degradation calling convention for ClioError**
- **Found during:** Task 2 (error tests)
- **Issue:** ClioError.__init__(message, error_type, details) required error_type positionally, but with_degradation called error_cls(message, details=...) which missed error_type
- **Fix:** Made error_type default to "clio_error" in base class
- **Files modified:** src/clio_agent/errors.py
- **Verification:** All 20 error tests pass
- **Committed in:** 690bd84 (Task 2 commit)

**2. [Rule 1 - Bug] Updated test_expert_failure_logs_status for new error format**
- **Found during:** Task 2 (full regression test)
- **Issue:** Existing test asserted "error" in answer.lower() but new message says "issue" not "error"
- **Fix:** Updated assertion to check "issue" in answer + structured error_info field
- **Files modified:** tests/test_core/test_agent_dispatch.py
- **Verification:** 398 tests pass
- **Committed in:** 690bd84 (Task 2 commit)

---

**Total deviations:** 2 auto-fixed (2 bugs)
**Impact on plan:** Both fixes necessary for correctness. No scope creep.

## Issues Encountered
None beyond the auto-fixed deviations above.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Config and error subsystems ready for REST API (plan 04-02) and containers (plan 04-03)
- REST API can use format_error_response() for all endpoint error handling
- Container deployments can use CLIO_* env vars for provider configuration

---
*Phase: 04-production-hardening*
*Completed: 2026-02-11*
