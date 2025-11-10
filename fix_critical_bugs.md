# CRITICAL BUG FIX PLAN

## Bugs to Fix (Priority Order)

### 1. Timestamp Type Inconsistencies
- coordinator.py:554-555 - String timestamps in invocation
- coordinator.py:507-508 - Mixed float/string timestamps
- memory.py:669-672 - Unsafe fallback to time.time()

### 2. DSPy Version Mismatches
- 7 files still using dspy-ai>=2.6.0
- Should be: >=3.0.3

### 3. Python Version Mismatches
- 7 files still using >=3.11
- Should be: >=3.12

### 4. FastMCP Version Outdated
- mcp_wrapper.py using >=0.1.0
- Should be: >=2.13.0

### 5. Missing requests dependency in pyproject.toml

## Fix Strategy

1. Fix ALL script headers in parallel
2. Fix timestamp bugs in coordinator
3. Fix memory.py timestamp fallback
4. Add requests to pyproject.toml
5. Test with UV after each fix batch

