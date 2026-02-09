# Testing Patterns

**Analysis Date:** 2026-02-09

## Test Framework

**Runner:**
- pytest 7.4.0+
- Config: `pyproject.toml` lines 107-117

**Assertion Library:**
- pytest's built-in assert statements
- unittest.mock for mocking

**Run Commands:**
```bash
pytest tests/                          # Run all tests
pytest tests/ -v                       # Verbose output
pytest tests/ --cov=clio_agent         # Coverage report
pytest tests/ --cov=clio_agent --cov-report=html  # HTML coverage
pytest tests/ -k test_name             # Run specific test
pytest tests/ -x                       # Stop on first failure
```

**Coverage Configuration:**
- Auto-enabled in pyproject.toml `addopts`
- Generates term-missing and HTML reports
- Current coverage: ~35% (25 tests across modules)

## Test File Organization

**Location:** `tests/` directory at project root, mirrors `src/clio_agent/` structure

**Directory layout:**
```
tests/
├── __init__.py
├── test_core/              # Core agent tests
│   ├── __init__.py
│   ├── test_agent.py
│   ├── test_config.py
├── test_experts/           # Expert-specific tests
│   ├── __init__.py
│   ├── test_data_expert.py
├── test_arc/               # ARC memory layer tests
│   ├── __init__.py
│   ├── test_lsm.py
├── test_tools/             # Tool/MCP tests
│   ├── __init__.py
├── test_integration/       # Integration tests
│   └── __init__.py
```

**Naming:**
- Test files: `test_<module>.py` (e.g., `test_agent.py`, `test_data_expert.py`)
- Test classes: `Test<Feature>` (e.g., `TestClioAgent`, `TestLMStudioConfig`, `TestLSMTree`)
- Test functions: `test_<specific_behavior>` (e.g., `test_initialization()`, `test_basic_write_read()`)

## Test Structure

**Suite Organization:**

Test classes group related tests. All use pytest conventions.

```python
class TestClioAgent:
    """Test ClioAgent agent functionality."""

    def test_clio_agent_initialization(self):
        """Test ClioAgent agent can be initialized."""
        agent = ClioAgent()
        assert agent is not None
        assert hasattr(agent, 'forward')
```

**Fixtures (pytest style):**
```python
@pytest.fixture
def temp_dir(self):
    """Create temporary directory for tests."""
    temp_path = tempfile.mkdtemp()
    yield temp_path  # Provide resource to test
    shutil.rmtree(temp_path, ignore_errors=True)  # Cleanup

@pytest.fixture
def lsm(self, temp_dir):
    """Create LSMTree instance for testing."""
    lsm_tree = LSMTree(data_dir=temp_dir, memtable_size=10, compaction_threshold=3)
    yield lsm_tree
    lsm_tree.close()
```

**Setup/Teardown Pattern:**
```python
def setup_method(self):
    """Run before each test method."""
    self.test_data = []

def teardown_method(self):
    """Run after each test method."""
    self.test_data.clear()
```

**Fixture-based pattern (preferred):**
```python
@pytest.fixture
def resource(self):
    # Setup
    resource = create_resource()
    yield resource
    # Teardown
    resource.cleanup()
```

Example from `test_lsm.py` (lines 25-38):
```python
@pytest.fixture
def temp_dir(self):
    """Create temporary directory for tests."""
    temp_path = tempfile.mkdtemp()
    yield temp_path
    shutil.rmtree(temp_path, ignore_errors=True)

@pytest.fixture
def lsm(self, temp_dir):
    """Create LSMTree instance for testing."""
    lsm_tree = LSMTree(data_dir=temp_dir, memtable_size=10, compaction_threshold=3)
    yield lsm_tree
    lsm_tree.close()
```

## Mocking

**Framework:** `unittest.mock` (standard library)

**Import pattern:**
```python
from unittest.mock import Mock, patch, MagicMock
```

**Common mocking patterns:**

1. Mock simple objects:
```python
mock_arc = Mock()
expert = DataExpert(use_tools=True, arc_memory=mock_arc)
assert expert.arc_memory is mock_arc
```
From `test_data_expert.py` (lines 51-52)

2. Mock with return value:
```python
mock_obj = Mock()
mock_obj.method.return_value = {"key": "value"}
```

3. Patch function/class:
```python
@patch('module.function')
def test_something(self, mock_func):
    mock_func.return_value = "mocked"
    result = function_that_calls_mock_func()
    assert result == "mocked"
    mock_func.assert_called_once()
```

**What to Mock:**
- External dependencies (LM Studio, MCP servers)
- Network calls (requests.get, etc.)
- Expensive operations (LLM inference)
- File I/O for isolated unit tests

**What NOT to Mock:**
- Core business logic (ARCMemory operations, DataExpert reasoning)
- Internal method calls (only mock at boundary)
- File system operations in integration tests

Example from `test_agent.py` (lines 8, 18):
```python
from unittest.mock import Mock, patch, MagicMock
from clio_agent.agent import ClioAgent

# Test without mocking DSPy (integration-style)
agent = ClioAgent()
assert agent is not None
```

## Fixtures and Factories

**Test Data:**
Fixtures provide reusable test objects. No factory pattern observed yet, fixtures used directly.

Example from `test_lsm.py` (lines 40-52):
```python
def test_basic_write_read(self, lsm):
    """Test basic write and read operations."""
    timestamp = 1704800000.0
    metric = {"agent": "DataExpert", "latency_ms": 1500}

    lsm.write(timestamp, metric)
    result = lsm.read(timestamp)
    assert result is not None
    assert result["agent"] == "DataExpert"
```

**Location:**
- Fixtures: defined within test class as `@pytest.fixture` methods
- Test data: defined as class attributes or local variables in test methods
- No centralized fixture file (conftest.py) yet

## Coverage

**Requirements:**
- No enforced minimum yet
- Target by phase: Phase 1 (50%), Phase 2 (60%), Phase 3 (70%), Phase 4+ (80%)
- Current: ~35% coverage (from MEMORY.md)

**View Coverage:**
```bash
pytest tests/ --cov=clio_agent --cov-report=term-missing
pytest tests/ --cov=clio_agent --cov-report=html
# Open htmlcov/index.html in browser
```

**Coverage Gaps (from test files):**
- `test_agent.py` (lines 43-47): TODO for forward() method, routing logic, error handling with DSPy mocks
- `test_data_expert.py` (lines 58-62): TODO for forward() method, tool calling, ARC caching, error handling
- `test_config.py` (lines 40-43): TODO for configure_dspy_lm_studio(), setup_dspy(), fetch_lm_studio_models()

## Test Types

**Unit Tests:**
- Scope: Single module/class in isolation
- Location: `tests/test_<module>/`
- Mock dependencies and external systems
- Examples:
  - `test_config.py`: Tests LMStudioConfig dataclass (no network)
  - `test_lsm.py`: Tests LSMTree storage operations (mocked filesystem via temp_dir)
  - `test_agent.py`: Tests agent initialization (no LM inference)

**Integration Tests:**
- Scope: Multiple modules working together
- Location: `tests/test_integration/` (folder exists but empty)
- May use real resources (temp files, in-memory structures)
- Test agent with DataExpert, ARC memory together
- Not yet implemented

**E2E Tests:**
- Scope: Full user workflows (CLI interaction, API calls)
- Location: `tests/test_integration/` (folder exists but empty)
- Framework: None implemented yet (could use pytest + requests for API, subprocess for CLI)
- Not yet implemented

## Test Patterns and Examples

**Pattern: Class-based tests with fixtures**
```python
class TestLSMTree:
    """Test suite for LSMTree class."""

    @pytest.fixture
    def temp_dir(self):
        temp_path = tempfile.mkdtemp()
        yield temp_path
        shutil.rmtree(temp_path, ignore_errors=True)

    @pytest.fixture
    def lsm(self, temp_dir):
        lsm_tree = LSMTree(data_dir=temp_dir, memtable_size=10, compaction_threshold=3)
        yield lsm_tree
        lsm_tree.close()

    def test_basic_write_read(self, lsm):
        timestamp = 1704800000.0
        metric = {"agent": "DataExpert", "latency_ms": 1500}
        lsm.write(timestamp, metric)
        result = lsm.read(timestamp)
        assert result is not None
        assert result["agent"] == "DataExpert"
```

From `test_lsm.py` (lines 22-52).

**Pattern: Testing initialization**
```python
def test_expert_initialization(self):
    """Test expert can be initialized."""
    expert = DataExpert()
    assert expert is not None
    assert hasattr(expert, 'forward')
    assert hasattr(expert, 'agent')
```

From `test_data_expert.py` (lines 22-28).

**Pattern: Testing with mock dependencies**
```python
def test_expert_react_mode_with_arc(self):
    """Test expert with ARC memory integration."""
    mock_arc = Mock()
    expert = DataExpert(use_tools=True, arc_memory=mock_arc)
    assert expert is not None
    assert expert.arc_memory is mock_arc
```

From `test_data_expert.py` (lines 49-56).

**Pattern: Capability/metadata testing**
```python
def test_capabilities(self):
    """Test expert capabilities metadata."""
    caps = DataExpert.get_capabilities()
    assert caps['name'] == "Data Expert"
    assert 'hdf5' in caps['keywords']
```

From `test_data_expert.py` (lines 13-20).

## Async Testing

**Framework:** pytest-asyncio>=0.21.0

**Pattern (not yet used in codebase):**
```python
@pytest.mark.asyncio
async def test_async_function():
    result = await async_operation()
    assert result is not None
```

Will be needed for MCP server testing in Phase 2+.

## Error/Exception Testing

**Pattern for testing exceptions:**
```python
def test_error_condition(self):
    """Test that error is raised for invalid input."""
    with pytest.raises(ValueError, match="expected error message"):
        function_that_raises()
```

Not yet extensively used in codebase (Phase 4 requirement).

## Test Strategy by Phase

**Phase 1 (Current):**
- Focus: Agent initialization, config, basic expert functionality
- Coverage target: 50%
- Use mocks for DSPy, external services
- Test data structures (Conversation, Invocation, Message)

**Phase 2:**
- Add: Multi-expert routing tests, ARC memory integration tests
- Coverage target: 60%
- Begin integration tests with multiple modules
- Test MCP tool connectivity (mocked)

**Phase 3:**
- Add: Optimizer tests, learning/self-improvement tests
- Coverage target: 70%
- Test metric collection via LSM tree

**Phase 4+:**
- Add: E2E tests, API tests, performance tests
- Coverage target: 80%+
- Test CLI interaction
- Performance benchmarks (cache hit rate, latency targets)

## Common Test Issues and Gaps

**Known Issues:**

1. **DSPy mocking:** Main challenge is mocking DSPy.ReAct predictions. Requires:
   - Mock `dspy.Predict` or `dspy.ChainOfThought`
   - Mock response prediction objects with typed fields
   - Example TODO: `test_agent.py` lines 43-47

2. **MCP tool testing:** Will require:
   - In-memory MCP server testing via `Client(server)` (FastMCP 3.x pattern)
   - Currently using mock tool functions (hdf5_analyze, hdf5_optimize)
   - Example TODO: `test_data_expert.py` lines 58-62

3. **LLM testing:** Tests currently avoid LM inference:
   - Use Mock() for arc_memory parameter
   - Skip tests that require actual model output
   - Phase 4: implement LLM response mocking

**Gaps to Fill:**

- No conftest.py for shared fixtures
- No async tests (needed for FastMCP)
- No E2E tests for CLI
- No performance/load tests
- Limited error handling tests
- No tests for context compilation (retrieval.py)
- No tests for coordinator (coordinator.py)
- No tests for registry (registry.py)

## Test Execution

**Before Commit:**
```bash
pytest tests/                          # Run all tests
ruff check src/                        # Lint check
```

Both must pass before committing.

**GitHub Actions/CI:**
- Not yet configured (Phase 4)
- Should run pytest + coverage report
- Should fail if coverage drops

---

*Testing analysis: 2026-02-09*
